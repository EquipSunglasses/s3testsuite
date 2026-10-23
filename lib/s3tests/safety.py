"""Run safety: crash-safe bucket journal, abort control and automatic recovery.

Escape plan, in order of severity:

1. A test exceeds test_timeout      -> stopped at its next request (ERROR), buckets removed.
2. A test stops making progress     -> the watchdog abandons it after test_timeout +
   (stuck inside one request)          hang_grace, marks it ERROR, removes its buckets from
                                       another thread and starts a replacement worker.
3. The endpoint stops answering     -> circuit breaker: after `breaker` consecutive
                                       connection errors the run aborts, queued tests
                                       are reported as not run, everything is cleaned.
4. Ctrl-C, SIGTERM, SIGHUP,         -> run aborts the same way; a second signal exits at
   --max-runtime reached               once, leaving the journal for recovery.
5. kill -9, crash, power loss,      -> the journal on disk still lists every bucket the
   laptop asleep                       run created; the next run (or --recover) finds
                                       journals whose process is gone and removes them.

The journal lives in [run] state_dir (default ~/.local/state/s3tests), one
JSON-lines file per run. It records bucket names and which configured user
created them, never credentials.
"""

import errno
import glob
import json
import os
import socket
import ssl
import threading
import time
import http.client

STATE_ENV = 'S3TESTS_STATE_DIR'


class RunAborted(Exception):
    """Raised by clients once the run has been aborted."""


def default_state_dir():
    if os.environ.get(STATE_ENV):
        return os.environ[STATE_ENV]
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(base, 's3tests')


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def is_connection_error(exc):
    return isinstance(exc, (ConnectionError, socket.timeout, socket.gaierror, ssl.SSLError,
                            http.client.HTTPException, TimeoutError)) or (
        isinstance(exc, OSError) and exc.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
                                                   errno.ETIMEDOUT, errno.ECONNRESET, errno.EPIPE))


class Journal(object):
    """Append-only record of buckets created and removed by one run."""

    def __init__(self, state_dir, cfg, enabled=True):
        self.enabled = enabled
        self.path = None
        self._lock = threading.Lock()
        self._live = {}          # bucket -> user, still to be removed (kept in memory even
        self._all = {}           # when the file cannot be written); every bucket this run made
        if not enabled:
            return
        try:
            if not os.path.isdir(state_dir):
                os.makedirs(state_dir, 0o700)
            run_id = time.strftime('%Y%m%dT%H%M%S') + '-%d' % os.getpid()
            self.path = os.path.join(state_dir, 'run-%s.jsonl' % run_id)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._fh = os.fdopen(fd, 'a')
            self._write({'op': 'start', 'pid': os.getpid(), 'host': socket.gethostname(),
                         'endpoint': cfg.endpoint, 'prefix': cfg.bucket_prefix, 'time': time.time()})
        except OSError as e:
            self.enabled = False
            self.error = str(e)

    def _write(self, rec):
        self._fh.write(json.dumps(rec) + '\n')
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def created(self, bucket, user):
        with self._lock:
            self._live[bucket] = user
            self._all[bucket] = user
            if self.enabled:
                self._write({'op': 'create', 'bucket': bucket, 'user': user, 'time': time.time()})

    def removed(self, bucket):
        with self._lock:
            if self._live.pop(bucket, None) is not None and self.enabled:
                self._write({'op': 'remove', 'bucket': bucket, 'time': time.time()})

    def forget(self, bucket):
        """The server refused to create it: it was never ours."""
        with self._lock:
            self._all.pop(bucket, None)
            if self._live.pop(bucket, None) is not None and self.enabled:
                self._write({'op': 'remove', 'bucket': bucket, 'time': time.time()})

    def live(self):
        with self._lock:
            return dict(self._live)

    def all_created(self):
        with self._lock:
            return dict(self._all)

    def close(self):
        """Finish the journal; delete it when nothing is left to recover."""
        if not self.enabled:
            return
        with self._lock:
            self._write({'op': 'end', 'time': time.time(), 'remaining': sorted(self._live)})
            self._fh.close()
            if not self._live:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass


def read_journal(path):
    meta, live, ended = {}, {}, False
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # torn last line after a crash
            op = rec.get('op')
            if op == 'start':
                meta = rec
            elif op == 'create':
                live[rec['bucket']] = rec.get('user', 'main')
            elif op == 'remove':
                live.pop(rec['bucket'], None)
            elif op == 'end':
                ended = True
    return meta, live, ended


def stale_journals(state_dir, endpoint):
    """Journals for this endpoint whose run is no longer alive. Returns [(path, meta, live)]."""
    out = []
    host = socket.gethostname()
    for path in sorted(glob.glob(os.path.join(state_dir, 'run-*.jsonl'))):
        try:
            meta, live, ended = read_journal(path)
        except OSError:
            continue
        if meta.get('endpoint') != endpoint:
            continue
        if meta.get('pid') == os.getpid():
            continue
        if not ended and meta.get('host') == host and _pid_alive(int(meta.get('pid', 0))):
            continue  # another run in progress on this machine
        if not ended and meta.get('host') != host and time.time() - meta.get('time', 0) < 86400:
            continue  # possibly running elsewhere; leave it for a day
        out.append((path, meta, live))
    return out


def recover(cfg, state_dir, out, nuke):
    """Remove buckets recorded by dead runs. Returns (removed, failed) counts."""
    failed = removed = 0
    jobs = stale_journals(state_dir, cfg.endpoint)
    for path, meta, live in jobs:
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('time', 0)))
        if not live:
            os.unlink(path)
            continue
        out.write('recovering %d bucket(s) from interrupted run %s (pid %s, %s)\n' % (
            len(live), os.path.basename(path), meta.get('pid'), when))
        remaining = {}
        for bucket, user in sorted(live.items()):
            creds = cfg.alt if user == 'alt' and cfg.alt else cfg.main
            client = cfg.make_client(creds, user)
            client.deadline = time.time() + cfg.cleanup_timeout if cfg.cleanup_timeout else None
            try:
                nuke(client, bucket)
                out.write('  %-63s removed\n' % bucket)
                removed += 1
            except Exception as e:
                out.write('  %-63s FAILED: %s\n' % (bucket, e))
                remaining[bucket] = user
        out.flush()
        if remaining:
            failed += len(remaining)
            with open(path, 'a') as f:
                for b in live:
                    if b not in remaining:
                        f.write(json.dumps({'op': 'remove', 'bucket': b, 'time': time.time()}) + '\n')
        else:
            os.unlink(path)
    return removed, failed


class RunState(object):
    """Shared abort flag, journal and watchdog bookkeeping for one run."""

    def __init__(self, cfg, journal):
        self.cfg = cfg
        self.journal = journal
        self.abort_event = threading.Event()
        self.paused = threading.Event()
        self.abort_reason = None
        self._lock = threading.Lock()
        self.active = {}          # worker id -> (test, start time, ctx)
        self.signal_received = None
        self.started = time.time()

    def abort(self, reason):
        with self._lock:
            if self.abort_reason is None:
                self.abort_reason = reason
        self.abort_event.set()

    @property
    def aborted(self):
        return self.abort_event.is_set()
