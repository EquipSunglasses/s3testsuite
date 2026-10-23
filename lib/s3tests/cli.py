"""Command line entry point."""

import argparse
import json
import os
import signal
import sys
import time

from . import LIBDIR, version_line, cleanup, config, report, runner, safety, selftest
from . import client as client_mod
from . import s3xml
from . import compare as compare_mod
from . import results as results_mod

EPILOG = """examples:
  s3tests --check                      verify endpoint, TLS and credentials
  s3tests --smoke                      21 core data-path tests, about a minute
  s3tests --standard                   140 tests: what S3 applications commonly rely on
  s3tests --full                       all 190 tests (default, or [run] tier)
  s3tests --cleanup --dry-run          list leftover test buckets
  s3tests --cleanup --yes              delete them (only names starting with bucket_prefix)
  s3tests --recover                    clean up after runs that were killed or crashed
  s3tests --max-runtime 30             abort with cleanup after 30 minutes
  s3tests multipart listing            run tests whose id contains a word
  s3tests 'object.test_range_*'        glob on test ids
  s3tests -t versioning -x slow        select or exclude by tag
  s3tests -j 8 --junit out/junit.xml   parallel run with JUnit output for CI
  s3tests -v object.test_put_get_roundtrip   traceback and HTTP history on failure

comparing solutions and runs:
  s3tests --label vendor-a --results-dir /var/lib/s3tests      record a run
  s3tests --compare /var/lib/s3tests                           latest run of every label, side by side
  s3tests --compare /var/lib/s3tests --format html -o cmp.html
  s3tests --diff OLD.json NEW.json                             regressions and fixes
  s3tests --trend /var/lib/s3tests/vendor-a                    history and flaky tests

exit status: 0 all passed, 1 failures or leftover buckets, 2 usage or config error,
             3 endpoint unreachable, 4 run stopped early, 5 regression (--fail-on-regression),
             130 interrupted
"""


def _split(values):
    out = []
    for v in values or ():
        out.extend(p.strip() for p in v.split(',') if p.strip())
    return out


def build_parser():
    p = argparse.ArgumentParser(prog='s3tests', description='S3 protocol conformance tests '
                                '(standard library only, no tox/pytest).', epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('patterns', nargs='*', metavar='PATTERN',
                   help='test id substring or glob (module.test_name)')
    p.add_argument('-c', '--config', help='config file (default: $S3TESTS_CONF, ./s3tests.conf, '
                   '~/.s3tests.conf, /etc/s3tests/s3tests.conf)')
    p.add_argument('--endpoint', help='override [target] endpoint')
    p.add_argument('--region', help='override [target] region')
    p.add_argument('--addressing', choices=('path', 'virtual'), help='override addressing style')
    p.add_argument('--ca-bundle', help='CA bundle for TLS verification')
    p.add_argument('-k', '--insecure', action='store_true', help='do not verify TLS certificates')
    tier = p.add_mutually_exclusive_group()
    tier.add_argument('--tier', choices=runner.TIERS, help='test tier (default from [run] tier, else full)')
    tier.add_argument('--smoke', dest='tier', action='store_const', const='smoke', help='same as --tier smoke')
    tier.add_argument('--standard', dest='tier', action='store_const', const='standard',
                      help='same as --tier standard')
    tier.add_argument('--full', dest='tier', action='store_const', const='full', help='same as --tier full')
    p.add_argument('-t', '--tag', action='append', help='only run tests with any of these tags')
    p.add_argument('-x', '--exclude-tag', action='append', help='skip tests with any of these tags')
    p.add_argument('--no-default-excludes', action='store_true', help='ignore exclude_tags from the config')
    p.add_argument('-j', '--jobs', type=int, help='parallel workers (default from config, else 1)')
    p.add_argument('--junit', metavar='FILE', help='write JUnit XML results')
    p.add_argument('--json', metavar='FILE', help='write the results document (schema s3tests.results/1)')
    out = p.add_argument_group('results for comparison and automation')
    out.add_argument('--label', help='name of the system under test, e.g. vendor-a-lab (default [target] label, '
                     'else the endpoint host)')
    out.add_argument('--results-dir', metavar='DIR',
                     help='file each run under DIR/<label>/<UTC time>-<run id>/ (results.json, junit.xml, '
                          'console.log, changes.txt) and keep DIR/<label>/latest.json and history.jsonl')
    out.add_argument('--fail-on-regression', action='store_true',
                     help='with --results-dir: exit 5 (instead of 0 or 1) when a test that passed in the '
                          'previous run fails now')
    out.add_argument('-q', '--quiet', action='store_true', help='print only failures and the summary')
    out.add_argument('--compare', nargs='+', metavar='RESULTS',
                     help='compare runs side by side: results.json files, label directories, or a --results-dir root')
    out.add_argument('--diff', nargs=2, metavar=('OLD', 'NEW'), help='changes between two runs; exit 1 on regressions')
    out.add_argument('--trend', metavar='LABEL_DIR', help='pass rate history and flaky tests for one label')
    out.add_argument('--format', choices=('text', 'md', 'csv', 'html', 'json'), default='text',
                     help='output format for --compare (and json for --diff)')
    out.add_argument('--all', action='store_true', help='--compare: list every test, not only those that differ')
    out.add_argument('--last', type=int, default=20, help='--trend: number of runs (default 20)')
    out.add_argument('-o', '--output', metavar='FILE', help='write --compare/--diff/--trend output to FILE')
    p.add_argument('-v', '--verbose', action='count', default=0,
                   help='-v tracebacks and HTTP history on failure, -vv include headers')
    p.add_argument('--fail-fast', action='store_true', help='stop after the first failure')
    p.add_argument('--list', action='store_true', help='list selected tests and exit')
    p.add_argument('--list-tags', action='store_true', help='list tags with test counts and exit')
    p.add_argument('--check', action='store_true', help='check connectivity and credentials, then exit')
    p.add_argument('--show-config', action='store_true', help='print the effective configuration and exit')
    p.add_argument('--selftest', action='store_true', help='offline signer self-test (no endpoint needed)')
    p.add_argument('--cleanup', action='store_true',
                   help='delete leftover buckets whose names start with bucket_prefix, then exit')
    p.add_argument('--dry-run', action='store_true', help='with --cleanup: list, do not delete')
    p.add_argument('--older-than', type=int, metavar='MIN',
                   help='with --cleanup: only buckets created more than MIN minutes ago '
                        '(protects runs in progress)')
    p.add_argument('-y', '--yes', action='store_true', help='with --cleanup: do not ask for confirmation')
    p.add_argument('--recover', action='store_true',
                   help='remove buckets recorded by runs that died (kill -9, crash), then exit')
    p.add_argument('--no-recover', action='store_true',
                   help='do not clean up after dead runs before starting')
    p.add_argument('--max-runtime', type=int, metavar='MIN',
                   help='abort the run (with cleanup) after MIN minutes')
    p.add_argument('--no-color', action='store_true', help='disable colored output')
    p.add_argument('--version', action='store_true', help='show version, build, install path and Python, then exit')
    return p


def tls_hint(exc, cfg):
    """Explain certificate failures in terms of what to configure."""
    text = str(exc)
    if 'CERTIFICATE_VERIFY_FAILED' not in text and 'SSLCertVerification' not in type(exc).__name__:
        return ''
    host = cfg.endpoint.split('://', 1)[1].split('/')[0]
    if 'mismatch' in text or "doesn't match" in text:
        return '\n'.join(['', 'TLS verification failed: the certificate is trusted but not issued for %r.' % host,
                           'Use the hostname the certificate names (check its SAN entries), not an IP or alias.',
                           'See the names:  openssl s_client -connect %s </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName'
                           % (host if ':' in host else host + ':443')])
    lines = ['', 'TLS verification failed. Python does not trust the CA that issued the server certificate.']
    if 'local issuer' in text:
        lines.append('Either this Python has no CA store, or a proxy is re-signing traffic with an internal CA.')
    lines += [
        'Fix: set [target] ca_bundle (or S3TESTS_CA_BUNDLE, --ca-bundle) to a PEM file containing the issuing root CA,',
        '     e.g. /etc/pki/tls/certs/ca-bundle.crt on RHEL, /etc/ssl/cert.pem on macOS, or your corporate root CA.',
        'See the issuer:  openssl s_client -connect %s:%s </dev/null 2>/dev/null | grep "i:"' % (
            cfg.endpoint.split('://', 1)[1].split('/')[0].split(':')[0],
            cfg.endpoint.split('://', 1)[1].split('/')[0].split(':')[1] if cfg.endpoint.count(':') > 1 else '443'),
        'Use -k only to confirm the diagnosis; it disables certificate checks.',
    ]
    return '\n'.join(lines)


def _check(cfg):
    ok = True
    for label, creds in (('main', cfg.main), ('alt', cfg.alt)):
        if creds is None:
            print('%-4s  not configured' % label)
            continue
        c = cfg.make_client(creds, label)
        try:
            r = c.list_buckets()
        except Exception as e:
            print('%-4s  cannot reach %s: %s: %s%s' % (label, cfg.endpoint, type(e).__name__, e, tls_hint(e, cfg)))
            return False
        if r.ok:
            owner = r.xml().findtext('Owner/ID')
            print('%-4s  OK  owner=%s  buckets=%d  (%.0f ms)' % (
                label, owner, len(r.xml().findall('Buckets/Bucket')), r.elapsed * 1000))
        else:
            print('%-4s  HTTP %d %s %s' % (label, r.status, r.error_code or '', r.error_message or ''))
            ok = False
    return ok


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.compare or args.diff or args.trend:
        return _analysis(args)
    if args.version:
        import platform
        print(version_line())
        print('library : %s' % LIBDIR)
        print('python  : %s %s' % (platform.python_version(), sys.executable))
        return 0
    if args.selftest:
        return selftest.run()

    overrides = {'endpoint': args.endpoint, 'region': args.region, 'addressing': args.addressing,
                 'ca_bundle': args.ca_bundle, 'jobs': args.jobs, 'tier': args.tier,
                 'max_runtime': args.max_runtime, 'label': args.label}
    if args.insecure:
        overrides['verify_ssl'] = False
    try:
        cfg = config.load(args.config, overrides)
    except config.ConfigError as e:
        print('s3tests: %s' % e, file=sys.stderr)
        return 2

    if not (args.list or args.list_tags or args.selftest):
        for w in cfg.warnings:
            print('warning: %s' % w, file=sys.stderr)

    tests = runner.discover()
    if args.list_tags:
        counts = {}
        for t in tests:
            for tag in t.tags:
                counts[tag] = counts.get(tag, 0) + 1
        for tag in sorted(counts):
            print('%-24s %4d' % (tag, counts[tag]))
        return 0

    include = _split(args.tag) or cfg.include_tags
    exclude = _split(args.exclude_tag) + ([] if args.no_default_excludes else cfg.exclude_tags)
    tier = cfg.tier if not args.patterns or args.tier else 'full'
    selected = runner.select(tests, args.patterns, include, exclude, tier)

    if args.list:
        for t in selected:
            print('%-58s %-9s %s' % (t.id, t.tier, ','.join(sorted(t.tags - {t.module}))))
        print('\n%d of %d tests selected (tier %s)' % (len(selected), len(tests), tier), file=sys.stderr)
        return 0

    if args.show_config:  # before validation, so it helps diagnose a broken config
        print(cfg.describe())
        try:
            cfg.validate()
        except config.ConfigError as e:
            print('\nproblem: %s' % e)
            return 2
        return 0

    try:
        cfg.validate()
    except config.ConfigError as e:
        print('s3tests: %s' % e, file=sys.stderr)
        return 2
    if args.check:
        print(cfg.describe() + '\n')
        return 0 if _check(cfg) else 3
    if args.recover:
        return _recover(cfg, cfg.state_dir or safety.default_state_dir(), explicit=True)
    if args.cleanup:
        try:
            return cleanup.sweep(cfg, dry_run=args.dry_run, older_than=args.older_than,
                                 assume_yes=args.yes, jobs=max(1, cfg.jobs))
        except Exception as e:
            print('s3tests: cleanup failed: %s: %s%s' % (type(e).__name__, e, tls_hint(e, cfg)), file=sys.stderr)
            return 3

    if not selected:
        print('s3tests: no tests match the selection', file=sys.stderr)
        return 2

    preflight_error = None
    try:
        r = cfg.make_client(cfg.main, 'main').list_buckets()
        if not r.ok:
            preflight_error = 'preflight ListBuckets failed: HTTP %d %s %s' % (
                r.status, r.error_code or '', r.error_message or '')
    except Exception as e:
        preflight_error = 'cannot reach %s: %s: %s' % (cfg.endpoint, type(e).__name__, e)
        print('s3tests: %s%s' % (preflight_error, tls_hint(e, cfg)), file=sys.stderr)
    else:
        if preflight_error:
            print('s3tests: %s' % preflight_error, file=sys.stderr)
    if preflight_error:
        if args.results_dir:
            _record_unreachable(args, cfg, tier, include, exclude, tests, selected, preflight_error)
        return 3

    state_dir = cfg.state_dir or safety.default_state_dir()
    if cfg.recover and not args.no_recover:
        _recover(cfg, state_dir)

    label = cfg.label
    run_meta = {'id': results_mod.new_run_id(), 'label': label, 'started': time.time(), 'tier': tier,
                'patterns': args.patterns, 'include_tags': include, 'exclude_tags': exclude,
                'jobs': max(1, cfg.jobs)}
    run_dir = None
    tee = None
    if args.results_dir:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(run_meta['started']))
        run_dir = os.path.join(args.results_dir, safe_label(label), '%s-%s' % (stamp, run_meta['id']))
        try:
            os.makedirs(run_dir)
            tee = _Tee(sys.stdout, open(os.path.join(run_dir, 'console.log'), 'w'))
            sys.stdout = tee
        except OSError as e:
            print('s3tests: cannot create results directory %s: %s' % (run_dir, e), file=sys.stderr)
            return 2

    journal = safety.Journal(state_dir, cfg)
    if not journal.enabled:
        print('warning: cannot write run journal in %s (%s); a hard kill could leave buckets that only '
              '--cleanup will find' % (state_dir, getattr(journal, 'error', '')), file=sys.stderr)
    state = safety.RunState(cfg, journal)
    restore = _install_signal_handlers(state, journal, cfg)

    console = report.Console(color=False if (args.no_color or tee) else None, verbose=args.verbose,
                             quiet=args.quiet)

    def on_result(res):
        try:
            console.result(res)
        except (BrokenPipeError, IOError):
            # stdout closed (e.g. piped into head): keep cleaning up, stop printing
            _silence_stdout()
            state.abort('output closed (broken pipe)')

    def on_notice(msg):
        try:
            print('>>> %s' % msg)
            sys.stdout.flush()
        except (BrokenPipeError, IOError):
            _silence_stdout()

    try:
        console.start(len(selected), cfg, tier, label=label, run_id=run_meta['id'])
    except (BrokenPipeError, IOError):
        _silence_stdout()
    t0 = time.time()
    try:
        results = runner.run(cfg, selected, jobs=max(1, cfg.jobs), fail_fast=args.fail_fast,
                             on_result=on_result, full_trace=args.verbose >= 2, state=state,
                             on_notice=on_notice)
        if state.aborted:
            print('\nrun aborted: %s' % state.abort_reason)
        still = _final_cleanup(cfg, state, journal, results)
    finally:
        restore()
    elapsed = time.time() - t0
    console.summary(results, elapsed, state.abort_reason if state.aborted else None)
    rep = getattr(state, 'cleanup_report', None)
    if rep:
        line = 'Cleanup: %d test bucket(s) created, %d removed, %d remain' % (
            rep['created'], rep['created'] - len(rep['remaining']), len(rep['remaining']))
        line += ' (verified against the endpoint)' if rep['verified'] else ' (not verified)'
        if rep['stale_removed'] or rep['stale_failed']:
            line += '; %d stale bucket(s) from earlier runs removed' % len(rep['stale_removed'])
            if rep['stale_failed']:
                line += ', %d could not be removed' % len(rep['stale_failed'])
        print(line)
    ns = client_mod.NET_STATS
    if ns['requests']:
        print('network: %d requests over %d connections; retries: %d connect, %d stale keep-alive, '
              '%d dropped reads, %d SlowDown' % (ns['requests'], ns['connections'], ns['connect_retries'],
                                                 ns['stale_retries'], ns['read_retries'], ns['slowdown_retries']))
    bv = results_mod.bucket_visibility()
    if bv['checked']:
        if bv['delayed']:
            line = ('bucket visibility: %d of %d new bucket(s) returned 404 after CreateBucket succeeded; '
                    'longest %.1fs, mean %.1fs' % (bv['delayed'], bv['checked'], bv['max_wait_seconds'],
                                                   bv['mean_wait_seconds']))
            if bv['never_ready']:
                line += '; %d not readable within bucket_ready_timeout (%ds)' % (bv['never_ready'],
                                                                                  cfg.bucket_ready_timeout)
            print(line)
        else:
            print('bucket visibility: all %d new bucket(s) readable immediately after CreateBucket' % bv['checked'])
    hung = [r for r in results if getattr(r, 'hung', False)]
    if hung:
        print('\n%d test(s) hung and were abandoned: %s' % (len(hung), ', '.join(r.test.id for r in hung)))
    journal.close()
    if still:
        print('\n%d test bucket(s) remain on %s:' % (len(still), cfg.endpoint))
        for name, err in still:
            print('  %s: %s' % (name, err))
        if journal.enabled:
            print('they are recorded in %s and will be retried automatically on the next run,' % journal.path)
            print('or now with: s3tests --recover')
        else:
            print('remove them with: s3tests --cleanup')

    if state.signal_received:
        rc = 130
    elif state.aborted and not args.fail_fast:
        rc = 4
    else:
        rc = 1 if still or any(r.status in runner.BAD for r in results) else 0

    run_meta.update(finished=time.time(), aborted=state.aborted, abort_reason=state.abort_reason, exit_code=rc)
    doc = results_mod.build(results, cfg, run_meta, tests, selected, ns, [n for n, _ in still],
                            getattr(state, 'cleanup_report', None))
    regressions = 0
    if run_dir:
        regressions = _record_run(args.results_dir, label, run_dir, doc, results, cfg, elapsed)
    if args.junit:
        report.write_junit(args.junit, results, cfg, elapsed, label=label, run_id=run_meta['id'])
    if args.json:
        results_mod.write(args.json, doc)
    if tee:
        sys.stdout = tee.primary
        tee.secondary.close()
    if regressions and args.fail_on_regression and rc in (0, 1):
        rc = 5  # a new regression is the most actionable signal; known failures alone give 1
    return rc


def safe_label(label):
    out = ''.join(c if (c.isalnum() or c in '._-') else '_' for c in label).strip('._')
    return out or 'unnamed'


class _Tee(object):
    """Write console output to the terminal and to console.log."""

    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary

    def write(self, data):
        self.primary.write(data)
        try:
            self.secondary.write(data)
        except ValueError:
            pass

    def flush(self):
        self.primary.flush()
        try:
            self.secondary.flush()
        except ValueError:
            pass

    def isatty(self):
        return False

    def fileno(self):
        return self.primary.fileno()


def _record_run(root, label, run_dir, doc, results, cfg, elapsed):
    """Write results.json and junit.xml, compare with the previous run, update latest.json
    and history.jsonl. Returns the number of regressions since the previous run."""
    label_dir = os.path.dirname(run_dir)
    results_mod.write(os.path.join(run_dir, 'results.json'), doc)
    report.write_junit(os.path.join(run_dir, 'junit.xml'), results, cfg, elapsed, label=label, run_id=doc['run']['id'])
    latest = os.path.join(label_dir, 'latest.json')
    changes = None
    regressions = 0
    if os.path.isfile(latest):
        try:
            prev = results_mod.load(latest)
            d = compare_mod.diff(prev, doc)
            changes = compare_mod.diff_counts(d)
            regressions = changes['regressions']
            text = compare_mod.format_diff(prev, doc, d)
            with open(os.path.join(run_dir, 'changes.txt'), 'w') as f:
                f.write(text)
            if any(changes[k] for k in ('regressions', 'fixes', 'failure_changed', 'newly_skipped',
                                        'no_longer_skipped')):
                print('\nChanges since the previous run (%s):' % prev['run']['started'])
                for line in text.splitlines()[2:]:
                    print(line)
            else:
                print('\nNo changes since the previous run (%s).' % prev['run']['started'])
        except (ValueError, KeyError, OSError) as e:
            print('warning: could not compare with %s: %s' % (latest, e), file=sys.stderr)
    results_mod.write(latest, doc)
    with open(os.path.join(label_dir, 'history.jsonl'), 'a') as f:
        f.write(results_mod.history_line(doc, changes) + '\n')
    print('results: %s' % run_dir)
    return regressions


def _record_unreachable(args, cfg, tier, include, exclude, tests, selected, reason):
    """Record a run that could not start, so trends show the outage. latest.json is left
    pointing at the last run that did execute, so comparisons keep using real results."""
    now = time.time()
    run = {'id': results_mod.new_run_id(), 'label': cfg.label, 'started': now, 'finished': now, 'tier': tier,
           'patterns': args.patterns, 'include_tags': include, 'exclude_tags': exclude, 'jobs': max(1, cfg.jobs),
           'aborted': True, 'abort_reason': reason, 'exit_code': 3}
    doc = results_mod.build([], cfg, run, tests, selected)
    label_dir = os.path.join(args.results_dir, safe_label(cfg.label))
    run_dir = os.path.join(label_dir, '%s-%s' % (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now)), run['id']))
    try:
        os.makedirs(run_dir)
        results_mod.write(os.path.join(run_dir, 'results.json'), doc)
        with open(os.path.join(label_dir, 'history.jsonl'), 'a') as f:
            f.write(results_mod.history_line(doc) + '\n')
        print('results: %s (run did not start)' % run_dir, file=sys.stderr)
    except OSError as e:
        print('s3tests: cannot record the failed run in %s: %s' % (run_dir, e), file=sys.stderr)


def _analysis(args):
    """--compare / --diff / --trend: work on saved results, no endpoint needed."""
    out = sys.stdout
    if args.output:
        out = open(args.output, 'w')
    try:
        if args.trend:
            out.write(compare_mod.trend(args.trend, last=args.last))
            return 0
        if args.diff:
            base, cur = (results_mod.load(p) for p in args.diff)
            d = compare_mod.diff(base, cur)
            if args.format == 'json':
                out.write(json.dumps(d, indent=1, sort_keys=True) + '\n')
            else:
                out.write(compare_mod.format_diff(base, cur, d))
            return 1 if d['regressions'] else 0
        paths = []
        for p in args.compare:
            # a --results-dir root: compare the latest run of every label in it
            if os.path.isdir(p) and not os.path.isfile(os.path.join(p, 'latest.json')) \
                    and not os.path.isfile(os.path.join(p, 'results.json')):
                subs = sorted(os.path.join(p, d) for d in os.listdir(p)
                              if os.path.isfile(os.path.join(p, d, 'latest.json')))
                if not subs:
                    raise ValueError('%s: no results found' % p)
                paths.extend(subs)
            else:
                paths.append(p)
        docs = [results_mod.load(p) for p in paths]
        out.write(compare_mod.compare(docs, args.format, show_all=args.all))
        if args.output:
            print('wrote %s (%d solution(s))' % (args.output, len(docs)))
        return 0
    except (ValueError, OSError) as e:
        print('s3tests: %s' % e, file=sys.stderr)
        return 2
    finally:
        if args.output:
            out.close()

def _final_cleanup(cfg, state, journal, results):
    """Make sure the run leaves nothing behind before it exits.

    Buckets that tests could not remove (for example because the network
    dropped) are retried with backoff for up to final_cleanup_window seconds.
    Ctrl-C, or the window running out, leaves them in the journal for the next
    run. Buckets left by earlier dead runs are cleaned here too.
    """
    def client_for(user):
        return cfg.make_client(cfg.alt if user == 'alt' and cfg.alt else cfg.main, user)

    pending = [(c, b) for r in results for b, c in r.leftovers]
    seen = set(b for _, b in pending)
    for b, user in sorted(journal.live().items()):  # e.g. tests still stuck when the run ended
        if b not in seen:
            pending.append((client_for(user), b))
    still = []
    if pending:
        print('\ncleaning up %d bucket(s) that tests could not remove:' % len(pending))
        still = cleanup.remove_buckets(cfg, pending, jobs=max(1, cfg.jobs), journal=journal)
    give_up = time.time() + cfg.final_cleanup_window
    wait = 10
    while still and time.time() < give_up and not state.signal_received:
        wait = max(1, min(wait, int(give_up - time.time())))
        print('%d bucket(s) still present; retrying in %ds (cleanup window %ds, Ctrl-C to stop waiting)'
              % (len(still), wait, cfg.final_cleanup_window))
        end = time.time() + wait
        while time.time() < end and not state.signal_received:
            time.sleep(0.2)
        if state.signal_received:
            break
        live = journal.live()
        retry = [(client_for(live.get(name, 'main')), name) for name, _ in still]
        still = cleanup.remove_buckets(cfg, retry, jobs=max(1, cfg.jobs), journal=journal)
        wait = min(wait * 2, 60)
    if cfg.recover and not state.signal_received:
        state_dir = cfg.state_dir or safety.default_state_dir()
        if safety.stale_journals(state_dir, cfg.endpoint):
            print('\nchecking for buckets left by earlier runs:')
            _recover(cfg, state_dir)
    report = {'created': len(journal.all_created()), 'remaining': [], 'verified': False,
              'stale_removed': [], 'stale_failed': []}
    if not state.signal_received:
        still = _verify_cleanup(cfg, journal, still, report)
        if cfg.sweep_stale_buckets:
            _sweep_stale(cfg, journal, report)
    state.cleanup_report = report
    return still


def _verify_cleanup(cfg, journal, still, report):
    """Check against the endpoint that every bucket this run created is gone.
    ListBuckets once per user; anything still listed is confirmed with HEAD (listings can lag
    behind deletes) and removed once more."""
    created = journal.all_created()
    if not created:
        report['verified'] = True
        return still
    by_user = {}
    for name, user in created.items():
        by_user.setdefault(user if user == 'alt' and cfg.alt else 'main', []).append(name)
    remaining = []
    try:
        for user, names in by_user.items():
            client = cfg.make_client(cfg.alt if user == 'alt' else cfg.main, user)
            lb = client.list_buckets()  # every bucket, not only the prefix: tests also create other names
            if not lb.ok:
                raise RuntimeError('ListBuckets returned HTTP %d %s' % (lb.status, lb.error_code or ''))
            listed = set(s3xml.bucket_names(lb.xml()))
            for name in sorted(set(names) & listed):
                h = client.head_bucket(name)
                if h.status == 404:
                    continue  # listing lag; the bucket is gone
                remaining.append((client, name))
            client.close()
    except Exception as e:
        print('warning: could not verify cleanup: %s: %s' % (type(e).__name__, e), file=sys.stderr)
        report['remaining'] = sorted(n for n, _ in still)
        return still
    if remaining:
        print('\n%d bucket(s) from this run are still present; removing:' % len(remaining))
        failed = cleanup.remove_buckets(cfg, remaining, jobs=max(1, cfg.jobs), journal=journal)
        known = dict(still)
        known.update(failed)
        still = sorted(known.items())
    report['verified'] = True
    report['remaining'] = sorted(n for n, _ in still)
    return still


def _sweep_stale(cfg, journal, report):
    """Remove test buckets left by earlier runs that kept no journal (killed on another host,
    older versions, deleted state directory). Only buckets with the configured prefix and
    older than stale_bucket_minutes are touched, so runs in progress elsewhere are safe."""
    floor = (cfg.test_timeout + cfg.cleanup_timeout + cfg.hang_grace) // 60 + 5
    minutes = max(cfg.stale_bucket_minutes, floor)
    mine = set(journal.all_created())
    items = []
    try:
        for user, creds in (('main', cfg.main), ('alt', cfg.alt)):
            if creds is None:
                continue
            client = cfg.make_client(creds, user)
            for name, _ in cleanup.find_test_buckets(client, cfg.bucket_prefix, minutes):
                if name not in mine:
                    items.append((cfg.make_client(creds, user), name))
            client.close()
    except Exception as e:
        print('warning: could not check for stale test buckets: %s: %s' % (type(e).__name__, e), file=sys.stderr)
        return
    if not items:
        return
    print('\nremoving %d stale test bucket(s) left by earlier runs (prefix %r, older than %d min):'
          % (len(items), cfg.bucket_prefix, minutes))
    failed = cleanup.remove_buckets(cfg, items, jobs=max(1, cfg.jobs))
    bad = set(n for n, _ in failed)
    report['stale_removed'] = sorted(n for _, n in items if n not in bad)
    report['stale_failed'] = sorted(bad)


def _silence_stdout():
    devnull = open(os.devnull, 'w')
    try:
        os.dup2(devnull.fileno(), sys.stdout.fileno())
    except OSError:
        pass
    sys.stdout = devnull


def _recover(cfg, state_dir, explicit=False):
    """Clean up buckets left by runs that died (kill -9, crash, power loss)."""
    try:
        removed, failed = safety.recover(cfg, state_dir, sys.stdout, cleanup.nuke_bucket)
    except Exception as e:
        print('warning: recovery of earlier runs failed: %s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 1
    if failed:
        print('warning: %d bucket(s) from earlier runs could not be removed; see above' % failed, file=sys.stderr)
    elif explicit and not removed:
        print('nothing to recover for %s' % cfg.endpoint)
    return 1 if failed else 0


def _install_signal_handlers(state, journal, cfg):
    """First Ctrl-C/SIGTERM/SIGHUP: abort and clean up. Second: exit at once."""
    state.signal_received = None
    previous = {}

    def handler(signum, frame):
        try:
            name = signal.Signals(signum).name
        except (AttributeError, ValueError):
            name = 'signal %d' % signum
        if name == 'SIGINT':
            name = 'SIGINT (Ctrl-C)'
        if state.signal_received is None:
            state.signal_received = name
            state.abort('interrupted by %s' % name)
            sys.stderr.write('\n%s: stopping new tests, letting running tests stop and clean up '
                             '(at most %ds). Press Ctrl-C again to exit immediately.\n' % (name, cfg.hang_grace))
            sys.stderr.flush()
            return
        sys.stderr.write('\nexiting immediately. ')
        live = journal.live()
        if live and journal.enabled:
            sys.stderr.write('%d bucket(s) are recorded in %s and will be removed on the next run '
                             '(or: s3tests --recover).\n' % (len(live), journal.path))
        elif live:
            sys.stderr.write('%d bucket(s) may remain; run: s3tests --cleanup\n' % len(live))
        else:
            sys.stderr.write('no test buckets were left.\n')
        sys.stderr.flush()
        os._exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, 'SIGHUP', None)):
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def restore():
        for sig, old in previous.items():
            signal.signal(sig, old)
    return restore

if __name__ == '__main__':
    sys.exit(main())
