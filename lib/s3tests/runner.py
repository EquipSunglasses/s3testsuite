"""Test discovery, selection and execution (no pytest/nose/tox)."""

import fnmatch
import importlib
import itertools
import pkgutil
import queue
import threading
import time
import traceback

from . import suites
from .assertions import NOT_RUN, S3AssertionError, SkipTest
from .context import TestContext, nuke_bucket
from .safety import Journal, RunAborted, RunState, is_connection_error

PASS, FAIL, ERROR, SKIP, XFAIL, XPASS = 'PASS', 'FAIL', 'ERROR', 'SKIP', 'XFAIL', 'XPASS'
BAD = (FAIL, ERROR)

# Tiers. Each test belongs to the smallest tier that includes it; larger
# tiers contain the smaller ones.
#   smoke     core data path: buckets, put/get/head/delete, listing, multipart,
#             copy, batch delete, presigned URLs (tests tagged 'smoke')
#   standard  everything an S3 application commonly relies on, without slow,
#             large, limit-probing or newer optional features
#   full      every test
TIERS = ('smoke', 'standard', 'full')
STANDARD_MODULES = frozenset(['bucket', 'object', 'listing', 'multipart', 'copy', 'multidelete',
                              'versioning', 'tagging', 'lifecycle', 'auth'])
STANDARD_EXCLUDE_TAGS = frozenset(['slow', 'large', 'limits', 'checksum', 'conditional_write',
                                   'lifecycle_expiration'])


def tags(*names):
    """Decorator adding tags to a test function."""
    def deco(fn):
        fn._tags = tuple(getattr(fn, '_tags', ())) + names
        return fn
    return deco


class TestCase(object):
    def __init__(self, module, func):
        self.module = module.__name__.rsplit('.', 1)[-1]
        self.name = func.__name__
        self.id = '%s.%s' % (self.module, self.name)
        self.func = func
        self.tags = set(getattr(module, 'TAGS', ())) | {self.module} | set(getattr(func, '_tags', ()))
        doc = (func.__doc__ or '').strip().splitlines()
        self.doc = doc[0].strip() if doc else ''
        if 'smoke' in self.tags:
            self.tier = 'smoke'
        elif self.module in STANDARD_MODULES and not (self.tags & STANDARD_EXCLUDE_TAGS):
            self.tier = 'standard'
        else:
            self.tier = 'full'

    def in_tier(self, tier):
        return TIERS.index(self.tier) <= TIERS.index(tier)


class Result(object):
    def __init__(self, test, status, duration=0.0, message='', detail='', trace=None, warnings=None,
                 leftovers=None, skip_category=None):
        self.test = test
        self.skip_category = skip_category if status == SKIP else None
        self.http_status = None      # status of the response a FAIL was about
        self.s3_code = None          # S3 error code of that response
        self.request = None          # METHOD /path of that response
        self.expected_failure = None # reason from [expected_failures], when listed
        self.rerun = False           # re-run after a connection error
        self.status = status
        self.duration = duration
        self.message = message
        self.detail = detail
        self.trace = trace or []
        self.warnings = warnings or []
        self.leftovers = leftovers or []


def discover():
    tests = []
    for info in sorted(pkgutil.iter_modules(suites.__path__), key=lambda m: m[1]):
        name = info[1]
        if name.startswith('_'):
            continue
        mod = importlib.import_module('%s.%s' % (suites.__name__, name))
        for attr, fn in list(vars(mod).items()):
            if attr.startswith('test_') and callable(fn) and getattr(fn, '__module__', None) == mod.__name__:
                tests.append(TestCase(mod, fn))
    return tests


def _match(test_id, pattern):
    if any(ch in pattern for ch in '*?['):
        return fnmatch.fnmatchcase(test_id, pattern)
    return pattern in test_id


def select(tests, patterns=None, include_tags=None, exclude_tags=None, tier='full'):
    out = []
    inc, exc = set(include_tags or ()), set(exclude_tags or ())
    for t in tests:
        if not t.in_tier(tier):
            continue
        if patterns and not any(_match(t.id, p) for p in patterns):
            continue
        if inc and not (t.tags & inc):
            continue
        if exc and (t.tags & exc):
            continue
        out.append(t)
    return out


def expected_failure(cfg, test):
    for pattern, reason in cfg.expected_failures.items():
        if test.id == pattern or fnmatch.fnmatchcase(test.id, pattern):
            return reason
    return None


def run_test(cfg, test, full_trace=False, state=None, on_ctx=None):
    ctx = TestContext(cfg, test.id, state)
    if on_ctx:
        on_ctx(ctx)
    t0 = time.time()
    status, message, detail, conn_error = PASS, '', '', False
    skip_cat, failed_resp = None, None
    try:
        test.func(ctx)
    except SkipTest as e:
        status, message, skip_cat = SKIP, str(e), e.category
    except RunAborted:
        status, message, skip_cat = SKIP, 'not completed: run aborted (%s)' % (
            state.abort_reason if state else 'aborted'), NOT_RUN
    except AssertionError as e:
        status, message, detail = FAIL, str(e) or 'assertion failed', traceback.format_exc()
        if isinstance(e, S3AssertionError):
            failed_resp = e
    except Exception as e:  # network errors, parse errors, deadlines, test bugs
        status, message, detail = ERROR, '%s: %s' % (type(e).__name__, e), traceback.format_exc()
        conn_error = is_connection_error(e)
    trace = ctx.http_history(full=full_trace) if status in BAD else []
    try:
        ctx.cleanup()
    except Exception as e:
        ctx.warnings.append('cleanup error: %s' % e)
    xf = expected_failure(cfg, test)
    if xf is not None:
        if status in BAD:
            status, message = XFAIL, '%s (%s)' % (xf, message)
        elif status == PASS:
            status, message = XPASS, 'listed in [expected_failures] but passed: %s' % xf
    res = Result(test, status, time.time() - t0, message, detail, trace, ctx.warnings, ctx.leftovers, skip_cat)
    res.connection_error = conn_error
    res.expected_failure = xf
    if failed_resp is not None:
        res.http_status, res.s3_code, res.request = failed_resp.http_status, failed_resp.s3_code, failed_resp.request
    return res


def _creds_for(cfg, label):
    return cfg.alt if label == 'alt' and cfg.alt else cfg.main


def _rescue(cfg, state, buckets, res):
    """Remove the buckets of an abandoned test with fresh clients (the stuck ones may never return)."""
    for name, old in buckets:
        client = cfg.make_client(_creds_for(cfg, old.label), old.label)
        client.deadline = time.time() + cfg.cleanup_timeout if cfg.cleanup_timeout else None
        try:
            nuke_bucket(client, name)
            state.journal.removed(name)
        except Exception as e:
            res.warnings.append('cleanup of bucket %s failed: %s' % (name, e))
            res.leftovers.append((name, client))


def run(cfg, tests, jobs=1, fail_fast=False, on_result=None, full_trace=False, state=None, on_notice=None):
    """Run tests on daemon worker threads, with a watchdog, circuit breaker and abort handling.

    The calling (main) thread never blocks on a test, so signals, hung
    requests and an unreachable endpoint are always handled.
    """
    if state is None:
        state = RunState(cfg, Journal(None, cfg, enabled=False))
    pending = queue.Queue()
    for t in tests:
        pending.put(t)
    done = queue.Queue()
    lock = threading.Lock()
    abandoned = set()
    threads = {}
    rescues = []
    seq = itertools.count()
    results = []
    reported = set()

    def worker(wid):
        while not state.aborted:
            while state.paused.is_set() and not state.aborted:
                time.sleep(0.2)  # endpoint outage: wait for the main thread to resume or abort
            if state.aborted:
                return
            try:
                t = pending.get_nowait()
            except queue.Empty:
                return

            def on_ctx(ctx, t=t):
                with lock:
                    state.active[wid] = (t, time.time(), ctx)
            res = run_test(cfg, t, full_trace, state, on_ctx)
            with lock:
                state.active.pop(wid, None)
                if wid in abandoned:
                    return
            done.put(res)

    def start_worker():
        wid = next(seq)
        th = threading.Thread(target=worker, args=(wid,), name='s3tests-worker-%d' % wid)
        th.daemon = True
        threads[wid] = th
        th.start()

    def report(res):
        if res.test.id in reported:
            return
        reported.add(res.test.id)
        results.append(res)
        if on_result:
            on_result(res)

    def abandon(wid, t, start, ctx, why):
        with lock:
            if wid in abandoned or state.active.get(wid, (None,))[0] is not t:
                return
            abandoned.add(wid)
            state.active.pop(wid, None)
        res = Result(t, ERROR, time.time() - start, why, '', ctx.http_history(), list(ctx.warnings))
        res.connection_error = False
        res.hung = True
        th = threading.Thread(target=_rescue, args=(cfg, state, ctx.bucket_list(), res),
                              name='s3tests-rescue-%d' % wid)
        th.daemon = True
        th.start()
        rescues.append(th)
        report(res)
        if not state.aborted:
            start_worker()  # replace the stuck worker so the run keeps moving

    def notice(msg):
        if on_notice:
            on_notice(msg)

    def ensure_workers():
        alive = [wid for wid, th in threads.items() if th.is_alive() and wid not in abandoned]
        for _ in range(max(0, min(jobs, pending.qsize()) - len(alive))):
            start_worker()

    held = {}       # test id -> first result, for tests that failed only on a connection error
    retried = set()

    def requeue_held(reason):
        for tid, first in sorted(held.items()):
            retried.add(tid)
            pending.put(first.test)
        if held:
            notice('re-running %d test(s) that failed on a connection error (%s)' % (len(held), reason))
        ensure_workers()

    for _ in range(max(1, min(jobs, len(tests)))):
        start_worker()

    paused_at = None
    next_probe = 0
    probe = cfg.make_client(cfg.main, 'main')
    probe.retries = 0
    conn_errors = 0
    abort_seen = None
    run_deadline = state.started + cfg.max_runtime * 60 if cfg.max_runtime else None
    while True:
        try:
            res = done.get(timeout=0.25)
        except queue.Empty:
            res = None
        if res is not None:
            tid = res.test.id
            conn_err = res.status == ERROR and getattr(res, 'connection_error', False)
            if conn_err and cfg.rerun_connection_errors and tid not in retried and not state.aborted:
                held[tid] = res            # report after one more attempt
            else:
                if tid in held:
                    first = held.pop(tid)
                    res.rerun = True
                    res.warnings.append('re-run after a connection error on the first attempt: %s'
                                        % first.message.splitlines()[0][:200])
                report(res)
            if conn_err:
                conn_errors += 1
            elif res.status not in (SKIP,):
                conn_errors = 0
            if cfg.breaker and conn_errors >= cfg.breaker and not state.aborted and paused_at is None:
                last = res.message.splitlines()[0][:160]
                if cfg.outage_wait > 0:
                    state.paused.set()
                    paused_at = time.time()
                    next_probe = paused_at + 2
                    notice('endpoint unreachable (%d consecutive connection errors, last: %s); pausing new '
                           'tests and waiting up to %ds for it to return' % (conn_errors, last, cfg.outage_wait))
                else:
                    state.abort('circuit breaker: %d consecutive connection errors, last: %s' % (conn_errors, last))
            if fail_fast and res.status in BAD and not state.aborted:
                state.abort('--fail-fast after %s' % res.test.id)
            continue
        now = time.time()
        if paused_at is not None and not state.aborted and now >= next_probe:
            try:
                up = probe.list_buckets().status < 500
            except Exception:
                up = False
            probe.close()
            if up:
                notice('endpoint reachable again after %ds; resuming' % (time.time() - paused_at))
                paused_at = None
                conn_errors = 0
                state.paused.clear()
                requeue_held('after the outage')
            elif time.time() - paused_at > cfg.outage_wait:
                state.abort('circuit breaker: endpoint unreachable for %ds' % (time.time() - paused_at))
                state.paused.clear()
                paused_at = None
            else:
                next_probe = time.time() + min(10, 2 + (time.time() - paused_at) / 6)
        if run_deadline and now > run_deadline and not state.aborted:
            state.abort('max runtime of %d min reached' % cfg.max_runtime)
        if state.aborted and abort_seen is None:
            abort_seen = now
        with lock:
            active = list(state.active.items())
        for wid, (t, start, ctx) in active:
            if ctx.deadline is not None and now > ctx.deadline + cfg.hang_grace:
                abandon(wid, t, start, ctx, 'hung: no progress %ds past its time limit; abandoned, '
                        'buckets cleaned from another thread' % cfg.hang_grace)
            elif abort_seen is not None and now - abort_seen > cfg.hang_grace:
                abandon(wid, t, start, ctx, 'abandoned %ds after the run was aborted (%s)' % (
                    cfg.hang_grace, state.abort_reason))
        alive = [th for wid, th in threads.items() if th.is_alive() and wid not in abandoned]
        if not alive and done.empty() and (paused_at is None or state.aborted):
            if held and not state.aborted and any(tid not in retried for tid in held):
                requeue_held('once, at the end of the run')
                continue
            break

    for res in held.values():  # aborted before the re-run: report the first attempt
        report(res)

    while True:  # tests never started because the run was aborted
        try:
            t = pending.get_nowait()
        except queue.Empty:
            break
        report(Result(t, SKIP, 0.0, 'not run: %s' % state.abort_reason, skip_category=NOT_RUN))
    limit = time.time() + (cfg.cleanup_timeout or 60) + 10
    for th in rescues:
        th.join(max(0.0, limit - time.time()))
    order = dict((t.id, i) for i, t in enumerate(tests))
    results.sort(key=lambda r: order[r.test.id])
    return results
