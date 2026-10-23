"""Sweep leftover test buckets (s3tests --cleanup).

Finds every bucket whose name starts with [target] bucket_prefix, for the
main and alt users, and removes it with all objects, versions, delete
markers, incomplete uploads and GOVERNANCE-locked objects. Buckets that do
not start with the prefix are never touched.
"""

import calendar
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .context import nuke_bucket  # noqa: F401  (re-exported for safety.recover)


def _created_epoch(text):
    try:
        return calendar.timegm(time.strptime((text or '')[:19], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return None


def find_test_buckets(client, prefix, older_than_minutes=None):
    r = client.list_buckets()
    if not r.ok:
        raise RuntimeError('ListBuckets for %s failed: HTTP %d %s' % (client.label, r.status, r.error_code or ''))
    out = []
    cutoff = time.time() - older_than_minutes * 60 if older_than_minutes else None
    for b in r.xml().iter('Bucket'):
        name = b.findtext('Name') or ''
        if not name.startswith(prefix):
            continue
        created = _created_epoch(b.findtext('CreationDate'))
        if cutoff is not None and created is not None and created > cutoff:
            continue
        out.append((name, created))
    return sorted(out)


def remove_buckets(cfg, items, jobs=4, out=sys.stdout, journal=None):
    """items: list of (client, bucket). Returns list of (bucket, error) that failed."""
    lock = threading.Lock()
    failed = []

    def one(item):
        client, name = item
        client.deadline = time.time() + cfg.cleanup_timeout if cfg.cleanup_timeout else None
        t0 = time.time()
        try:
            nuke_bucket(client, name)
            if journal is not None:
                journal.removed(name)
            msg, err = 'removed', None
        except Exception as e:
            msg, err = 'FAILED: %s' % e, str(e)
        finally:
            client.close()
        with lock:
            out.write('  %-63s %s (%.1fs)\n' % (name, msg, time.time() - t0))
            out.flush()
            if err:
                failed.append((name, err))

    if jobs <= 1:
        for it in items:
            one(it)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(one, items))
    return failed


def sweep(cfg, dry_run=False, older_than=None, assume_yes=False, jobs=4, out=sys.stdout):
    users = [('main', cfg.main)] + ([('alt', cfg.alt)] if cfg.alt else [])
    items = []
    for label, creds in users:
        client = cfg.make_client(creds, label)
        found = find_test_buckets(client, cfg.bucket_prefix, older_than)
        for name, created in found:
            age = '' if created is None else ' (%.0f min old)' % ((time.time() - created) / 60)
            out.write('%-5s %s%s\n' % (label, name, age))
            items.append((cfg.make_client(creds, label), name))
    if not items:
        out.write('no buckets starting with %r%s\n' % (
            cfg.bucket_prefix, ' older than %d min' % older_than if older_than else ''))
        return 0
    out.write('\n%d bucket(s) starting with %r on %s\n' % (len(items), cfg.bucket_prefix, cfg.endpoint))
    if dry_run:
        out.write('dry run: nothing deleted\n')
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            out.write('refusing to delete without --yes when not attached to a terminal\n')
            return 2
        out.write('Delete them and everything in them? [y/N] ')
        out.flush()
        if sys.stdin.readline().strip().lower() not in ('y', 'yes'):
            out.write('aborted\n')
            return 1
    out.write('\n')
    failed = remove_buckets(cfg, items, jobs=jobs, out=out)
    out.write('\n%d removed, %d failed\n' % (len(items) - len(failed), len(failed)))
    return 1 if failed else 0
