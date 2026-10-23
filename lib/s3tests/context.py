"""Per-test context: clients, unique names, bucket bookkeeping and cleanup."""

import binascii
import itertools
import os
import threading
import time

from . import s3xml
from .assertions import SkipTest, skip_if_disabled
from .client import Credentials, x, xml_doc

_id_cache = {}
_id_lock = threading.Lock()
BYPASS = {'x-amz-bypass-governance-retention': 'true'}

# How long new buckets took to become readable, across the whole run (see _wait_ready).
BUCKET_READY = {'checked': 0, 'delayed': 0, 'never_ready': 0, 'max_wait': 0.0, 'total_wait': 0.0}
_ready_lock = threading.Lock()


class TestContext(object):
    def __init__(self, cfg, test_id='', state=None):
        self.cfg = cfg
        self.state = state
        self.test_id = test_id
        self._token = binascii.hexlify(os.urandom(4)).decode('ascii')
        self._seq = itertools.count(1)
        self._clients = {}
        self._buckets = []
        self.warnings = []
        self.leftovers = []          # (bucket, client) that cleanup could not remove
        self.deadline = time.time() + cfg.test_timeout if cfg.test_timeout else None

    # clients -------------------------------------------------------------

    def _client(self, label, creds):
        if label not in self._clients:
            c = self.cfg.make_client(creds, label=label)
            c.bucket_tracker = self
            c.deadline = self.deadline
            c.abort_event = self.state.abort_event if self.state else None
            self._clients[label] = c
        return self._clients[label]

    def _set_deadline(self, deadline):
        self.deadline = deadline
        for c in self._clients.values():
            c.deadline = deadline

    def extend_deadline(self, seconds):
        """Allow a deliberately long test (e.g. lifecycle processing) more time."""
        self._set_deadline(time.time() + seconds)

    @property
    def c(self):
        """Client for the main user."""
        return self._client('main', self.cfg.main)

    @property
    def alt(self):
        """Client for the second user; skips the test when not configured."""
        if self.cfg.alt is None:
            raise SkipTest('second user not configured ([alt] section)')
        return self._client('alt', self.cfg.alt)

    @property
    def anon(self):
        """Client that sends unsigned (anonymous) requests."""
        return self._client('anon', None)

    def custom_client(self, access_key, secret_key, label='custom'):
        label = '%s%d' % (label, next(self._seq))
        return self._client(label, Credentials(access_key, secret_key, label=label))

    def user_id(self, client):
        """Canonical user ID of a client, from config or ListBuckets Owner/ID."""
        configured = {'main': self.cfg.main_user_id, 'alt': self.cfg.alt_user_id}.get(client.label)
        if configured:
            return configured
        key = (self.cfg.endpoint, client.creds.access_key)
        with _id_lock:
            if key not in _id_cache:
                r = client.list_buckets()
                if not r.ok:
                    raise AssertionError('cannot determine user id for %s: HTTP %d' % (client.label, r.status))
                _id_cache[key] = r.xml().findtext('Owner/ID')
            return _id_cache[key]

    def alt_principal(self):
        if self.cfg.alt_principal_arn:
            return self.cfg.alt_principal_arn
        return 'arn:aws:iam:::user/%s' % self.user_id(self.alt)

    # names and data --------------------------------------------------------

    def bucket_name(self, length=None):
        name = '%s%s-%d' % (self.cfg.bucket_prefix, self._token, next(self._seq))
        if length:
            name = (name + '-' + 'x' * 63)[:length]
            name = name[:-1] + '0'
        return name[:63]

    def key(self, stem='obj'):
        return '%s-%d' % (stem, next(self._seq))

    @staticmethod
    def data(size):
        return os.urandom(size)

    def new_bucket(self, client=None, headers=None, name=None):
        client = client or self.c
        name = name or self.bucket_name()
        r = client.create_bucket(name, headers=headers, location=self.cfg.location_constraint)
        if not r.ok:
            raise AssertionError('setup: create bucket %s failed: HTTP %d %s %s' % (
                name, r.status, r.error_code or '', r.error_message or ''))
        return name

    def register_bucket(self, name, client=None):
        """Record a bucket for cleanup (idempotent; CreateBucket calls register themselves)."""
        client = client or self.c
        if any(n == name for n, _ in self._buckets):
            return False
        self._buckets.append((name, client))
        if self.state:
            self.state.journal.created(name, client.label)
        return True

    def _unregister(self, name):
        self._buckets = [(n, c) for n, c in self._buckets if n != name]
        if self.state:
            self.state.journal.forget(name)

    # called by S3Client.create_bucket
    def pending(self, name, client):
        return self.register_bucket(name, client)

    def settle(self, newly_added, name, client, resp):
        """After CreateBucket: keep the record if the bucket is ours; drop it if the server
        refused to create it (so cleanup never touches another owner's bucket)."""
        ours = resp.ok or resp.error_code == 'BucketAlreadyOwnedByYou'
        if newly_added and not ours and resp.status < 500:
            self._unregister(name)
        if resp.ok and self.cfg.bucket_ready_timeout > 0:
            self._wait_ready(name, client)

    def _wait_ready(self, name, client):
        """After a successful CreateBucket, wait until the bucket is readable.

        Some clustered platforms acknowledge CreateBucket before every node
        knows the bucket, so the next request (often on another node behind a
        load balancer) gets 404 NoSuchBucket. This sends HEAD until it succeeds
        bucket_ready_checks times in a row, each on a new connection so a load
        balancer can route it to a different node. Only 404 counts as not ready;
        any other answer ends the wait and the test proceeds. A delay is reported
        as a warning and counted in the run summary, never silently absorbed.
        """
        timeout = self.cfg.bucket_ready_timeout
        need = self.cfg.bucket_ready_checks
        t0 = time.time()
        streak = misses = 0
        delay = 0.2
        ready = True
        while streak < need:
            client.close()
            r = client.head_bucket(name)
            if r.ok:
                streak += 1
                continue
            if r.status != 404:
                break
            streak = 0
            misses += 1
            if time.time() - t0 >= timeout:
                ready = False
                break
            client._sleep(delay)
            delay = min(delay * 1.5, 2.0)
        waited = time.time() - t0
        with _ready_lock:
            BUCKET_READY['checked'] += 1
            if misses:
                BUCKET_READY['delayed'] += 1
                BUCKET_READY['total_wait'] += waited
                BUCKET_READY['max_wait'] = max(BUCKET_READY['max_wait'], waited)
            if not ready:
                BUCKET_READY['never_ready'] += 1
        if not ready:
            self.warnings.append('bucket %s still returned 404 on HEAD %.0fs after CreateBucket succeeded'
                                 % (name, waited))
        elif misses:
            self.warnings.append('bucket %s returned 404 on HEAD for %.1fs after CreateBucket succeeded '
                                 '(%d of %d checks)' % (name, waited, misses, misses + streak))

    def bucket_list(self):
        return list(self._buckets)

    def versioned_bucket(self):
        b = self.new_bucket()
        r = self.c.set_versioning(b, 'Enabled')
        if not r.ok:
            raise AssertionError('setup: enable versioning failed: HTTP %d %s' % (r.status, r.error_code))
        return b

    def acl_bucket(self, client=None, headers=None):
        """A bucket where ACLs and public access are allowed, where the platform permits it.

        AWS creates buckets with Object Ownership = BucketOwnerEnforced (ACLs
        disabled) and Block Public Access on. This asks for ObjectWriter
        ownership and removes the bucket-level public access block. Servers
        that do not know these settings (most on-prem gateways) ignore them.
        """
        client = client or self.c
        name = self.bucket_name()
        hdrs = dict(headers or {})
        hdrs['x-amz-object-ownership'] = 'ObjectWriter'
        r = client.create_bucket(name, headers=hdrs, location=self.cfg.location_constraint)
        if r.status == 400 and r.error_code in ('InvalidArgument', 'InvalidRequest', 'NotImplemented'):
            hdrs.pop('x-amz-object-ownership')
            r = client.create_bucket(name, headers=hdrs, location=self.cfg.location_constraint)
        if not r.ok:
            skip_if_disabled(r, 'bucket setup')
            raise AssertionError('setup: create bucket %s failed: HTTP %d %s %s' % (
                name, r.status, r.error_code or '', r.error_message or ''))
        self.register_bucket(name, client)
        client.delete_bucket_sub(name, 'publicAccessBlock')
        return name

    # teardown ------------------------------------------------------------

    def cleanup(self):
        # Cleanup gets its own time budget and ignores the run abort flag, so a
        # test that hit its deadline or an aborted run still removes what it created.
        self._set_deadline(time.time() + self.cfg.cleanup_timeout if self.cfg.cleanup_timeout else None)
        for c in self._clients.values():
            c.abort_event = None
        for name, client in reversed(self._buckets):
            try:
                nuke_bucket(client, name)
                if self.state:
                    self.state.journal.removed(name)
            except Exception as e:  # never let cleanup mask the test result
                self.warnings.append('cleanup of bucket %s failed: %s' % (name, e))
                self.leftovers.append((name, client))
        self._buckets = []
        for c in self._clients.values():
            c.close()

    def eventually(self, fn, done, what, timeout=None):
        """Repeat fn() until done(response) or the propagation window ends; return the last response.

        For permission changes (ACLs, policies, public access blocks) that a
        platform applies asynchronously. AWS documents a delay of seconds; an
        on-prem gateway is usually immediate. The delay is reported as a
        warning so slow propagation is visible, never silently absorbed.
        """
        window = self.cfg.propagation_timeout if timeout is None else timeout
        t0 = time.time()
        delay = 0.5
        while True:
            r = fn()
            if done(r) or time.time() - t0 >= window:
                waited = time.time() - t0
                if waited >= 1.0 and done(r):
                    self.warnings.append('%s took %.1fs to take effect' % (what, waited))
                return r
            time.sleep(min(delay, max(0.0, window - (time.time() - t0))))
            delay = min(delay * 1.5, 3.0)

    def http_history(self, full=False):
        items = []
        for c in self._clients.values():
            items.extend(c.history)
        items.sort(key=lambda r: getattr(r, 'started', 0))
        return [r.trace(full) for r in items]


# --- bucket removal --------------------------------------------------------
#
# Rules that keep this correct on AWS and other S3-compatible gateways:
#  * Never send x-amz-bypass-governance-retention unless a delete was refused
#    with 403; AWS rejects the header on buckets without Object Lock.
#  * In a versioned or suspended bucket, only delete by explicit versionId.
#    A plain DELETE there writes a new delete marker instead of removing data.
#  * Stop when a pass removes nothing, and report what is stuck and why.


class CleanupError(Exception):
    pass


_policy_removed = {}


def _versioning_status(client, bucket):
    r = client.get_bucket_sub(bucket, 'versioning')
    if r.status == 404 and r.error_code == 'NoSuchBucket':
        return 'gone'
    if r.error_code == 'InvalidBucketName':
        return 'bad-name'  # some servers accept a name at create time and reject it later
    if not r.ok:
        return 'Unknown'
    return r.xml().findtext('Status') or None


def _abort_uploads(client, bucket):
    params = {}
    for _ in range(100):
        r = client.list_multipart_uploads(bucket, params)
        if not r.ok:
            return
        root = r.xml()
        for u in root.findall('Upload'):
            client.abort_multipart_upload(bucket, u.findtext('Key'), u.findtext('UploadId'))
        if not s3xml.truncated(root):
            return
        params = {'key-marker': root.findtext('NextKeyMarker') or '',
                  'upload-id-marker': root.findtext('NextUploadIdMarker') or ''}


def _delete_one(client, bucket, key, vid):
    """Delete one object version, escalating only when the server refuses. Returns (ok, reason)."""
    r = client.delete_object(bucket, key, version_id=vid)
    if r.ok or r.status == 404:
        return True, None
    if r.status == 403:  # Object Lock: GOVERNANCE retention and/or legal hold
        r = client.delete_object(bucket, key, version_id=vid, headers=BYPASS)
        if r.ok or r.status == 404:
            return True, None
        if vid is not None:
            client.put_object_sub(bucket, key, 'legal-hold', xml_doc('LegalHold', x('Status', 'OFF')),
                                  version_id=vid)
            r = client.delete_object(bucket, key, version_id=vid, headers=BYPASS)
            if r.ok or r.status == 404:
                return True, None
    return False, 'HTTP %d %s' % (r.status, r.error_code or '')


def _batch_delete(client, bucket, entries):
    """DeleteObjects for up to 1000 entries. Returns the entries the batch did not remove."""
    r = client.delete_objects(bucket, [(k, v) if v is not None else k for k, v in entries], quiet=True)
    if not r.ok:
        return list(entries)
    try:
        root = r.xml()
    except AssertionError:
        return list(entries)
    failed = set()
    for e in root.findall('Error'):
        failed.add((e.findtext('Key'), e.findtext('VersionId') or None))
    return [(k, v) for k, v in entries if (k, v) in failed or (k, None) in failed]


def _list_entries(client, bucket, versioned):
    """One page of (key, versionId) pairs still in the bucket."""
    if versioned:
        r = client.list_object_versions(bucket, {'max-keys': '1000'})
        if r.ok:
            return [(v['Key'], v['VersionId']) for v in s3xml.versions(r.xml())]
    r = client.list_objects_v2(bucket, {'max-keys': '1000'})
    if not r.ok:
        raise CleanupError('cannot list bucket: HTTP %d %s' % (r.status, r.error_code or ''))
    return [(k, None) for k in s3xml.keys(r.xml())]


def empty_bucket(client, bucket):
    """Remove every upload, object, version and delete marker. Returns False if the bucket is gone."""
    status = _versioning_status(client, bucket)
    if status == 'gone':
        return False
    if status == 'bad-name':
        return True  # cannot list or empty it; nuke_bucket still tries DeleteBucket
    versioned = status is not None  # Enabled, Suspended or Unknown: delete by version id
    _abort_uploads(client, bucket)
    stuck = {}
    previous = None
    while True:
        entries = [e for e in _list_entries(client, bucket, versioned) if e not in stuck]
        if not entries:
            break
        if previous is not None and set(entries) == previous:
            for e in entries:  # deletes reported success but nothing changed
                stuck.setdefault(e, 'still listed after delete')
            break
        previous = set(entries)
        for i in range(0, len(entries), 1000):
            for key, vid in _batch_delete(client, bucket, entries[i:i + 1000]):
                done, reason = _delete_one(client, bucket, key, vid)
                if not done:
                    stuck[(key, vid)] = reason
    if stuck and any('403' in why for why in stuck.values()) and not _policy_removed.get((id(client), bucket)):
        # A bucket policy (e.g. an explicit Deny left by a failed test) can block
        # deletes even for the owner. Remove it and retry the stuck entries once.
        _policy_removed[(id(client), bucket)] = True
        client.delete_bucket_sub(bucket, 'policy')
        for key, vid in list(stuck):
            done, reason = _delete_one(client, bucket, key, vid)
            if done:
                del stuck[(key, vid)]
            else:
                stuck[(key, vid)] = reason
    if stuck:
        sample = ', '.join('%s@%s (%s)' % (k, v or '-', why) for (k, v), why in list(stuck.items())[:3])
        raise CleanupError('%d object version(s) could not be deleted: %s' % (len(stuck), sample))
    return True


def nuke_bucket(client, bucket):
    last = None
    for attempt in range(3):
        if not empty_bucket(client, bucket):
            return
        r = client.delete_bucket(bucket)
        if r.ok or r.status == 404:
            return
        if r.error_code == 'InvalidBucketName':
            # the server rejects the name on every call; confirm it is not listed
            listed = client.list_buckets()
            if listed.ok and bucket not in s3xml.bucket_names(listed.xml()):
                return
        if r.status == 403:  # a bucket policy denying DeleteBucket; remove it and retry
            client.delete_bucket_sub(bucket, 'policy')
            r = client.delete_bucket(bucket)
            if r.ok or r.status == 404:
                return
        last = r
        time.sleep(0.5 * (attempt + 1))
    raise CleanupError('DELETE bucket returned HTTP %d %s' % (last.status, last.error_code or ''))
