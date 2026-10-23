"""Versioned, machine-readable run results (schema s3tests.results/1).

One JSON document per run. It is designed for automated, repeated runs whose
results are compared across platforms and over time:

* stable keys and test ids, tests sorted by id, UTC ISO-8601 timestamps
* a label naming the system under test, plus the tool version and build
* a fingerprint of the selected test set, so tools can tell whether two runs
  are comparable
* per test: the console status, a normalized outcome for comparisons, the skip
  category, and for failures the HTTP status and S3 error code

Normalized outcome (`outcome`) is what comparisons use:
  pass   PASS, or XPASS (passed despite being listed as an expected failure)
  fail   FAIL, or XFAIL (an expected failure is still a failure of the platform)
  error  ERROR (exception, timeout, connection problem)
  skip   SKIP, with skip_category saying why

The pass rate is pass / (pass + fail + error); skips are excluded and
counted separately. Additions to the schema add keys; a change that breaks
readers bumps the version in `schema`.
"""

import hashlib
import json
import os
import platform
import re
import socket
import time
import uuid

from . import __build__, __version__
from .runner import ERROR, FAIL, PASS, SKIP, XFAIL, XPASS

SCHEMA = 's3tests.results/1'
OUTCOME = {PASS: 'pass', XPASS: 'pass', FAIL: 'fail', XFAIL: 'fail', ERROR: 'error', SKIP: 'skip'}


def utc(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))


def new_run_id():
    return uuid.uuid4().hex[:12]


def fingerprint(test_ids):
    """Short stable hash of a set of test ids."""
    h = hashlib.sha256('\n'.join(sorted(test_ids)).encode('utf-8')).hexdigest()
    return h[:16]


def pass_rate(counts):
    denom = counts.get('pass', 0) + counts.get('fail', 0) + counts.get('error', 0)
    return round(100.0 * counts.get('pass', 0) / denom, 1) if denom else None


def _counts(records):
    c = {'pass': 0, 'fail': 0, 'error': 0, 'skip': 0}
    for r in records:
        c[r['outcome']] += 1
    c['pass_rate'] = pass_rate(c)
    return c


def _normalizer(prefix):
    """Replace generated bucket names with <bucket> and per-run ids (upload, version,
    presign parameters) with <id>, so the same failure reads the same in every run."""
    bucket = re.compile(re.escape(prefix) + r'[0-9a-f]{8}-[0-9]+[0-9a-z.-]*')
    ids = re.compile(r'((?:uploadId|versionId|upload-id-marker|version-id-marker|continuation-token|'
                     r'X-Amz-[A-Za-z-]+)=)[^&\s\]]+')

    noise = [
        (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'), '<uuid>'),
        (re.compile(r'\b[0-9a-f]{32,64}\b'), '<hex>'),
        (re.compile(r'\b0x[0-9a-f]+\b'), '0x..'),
        (re.compile(r"'[A-Za-z0-9+/]{6,}={1,2}'"), "'<base64>'"),
    ]

    def norm(text):
        if not text:
            return text
        text = ids.sub(r'\1<id>', bucket.sub('<bucket>', text))
        for pat, repl in noise:
            text = pat.sub(repl, text)
        return text
    return norm


def failure_kind(r, norm=lambda x: x):
    """Stable key for what went wrong: '<http status> <s3 code>' when a response was at fault,
    otherwise the name of the check (the text before the first colon)."""
    if r.status not in (FAIL, XFAIL, ERROR):
        return None
    if r.http_status:
        return ('%s %s' % (r.http_status, r.s3_code or '')).strip()
    head = norm((r.message or '').split(':')[0]).split(' [')[0].strip()
    return head[:80] or r.status.lower()


def test_record(r, norm=lambda x: x):
    rec = {
        'id': r.test.id,
        'module': r.test.module,
        'tier': r.test.tier,
        'tags': sorted(r.test.tags - {r.test.module}),
        'status': r.status,
        'outcome': OUTCOME[r.status],
        'skip_category': r.skip_category,
        'duration_s': round(r.duration, 3),
        'http_status': r.http_status,
        's3_code': r.s3_code,
        'failure_kind': failure_kind(r, norm),
        'request': norm(r.request),
        'message': norm((r.message or '').splitlines()[0][:500]) if r.message else '',
        'expected_failure': r.expected_failure,
        'rerun': bool(getattr(r, 'rerun', False)),
        'hung': bool(getattr(r, 'hung', False)),
        'warnings': [norm(w) for w in r.warnings],
    }
    if r.trace:
        rec['http_trace'] = r.trace
    return rec


def build(results, cfg, run, all_tests, selected, net_stats=None, leftovers=None, cleanup=None):
    """Assemble the results document.

    run: dict with id, label, started, finished, tier, patterns, include_tags,
         exclude_tags, jobs, aborted, abort_reason, exit_code
    """
    norm = _normalizer(cfg.bucket_prefix)
    records = sorted((test_record(r, norm) for r in results), key=lambda d: d['id'])
    by_module = {}
    for rec in records:
        by_module.setdefault(rec['module'], []).append(rec)
    skip_cats = {}
    for rec in records:
        if rec['skip_category']:
            skip_cats[rec['skip_category']] = skip_cats.get(rec['skip_category'], 0) + 1
    summary = _counts(records)
    summary['tests'] = len(records)
    summary['skip_categories'] = skip_cats
    summary['by_module'] = dict((m, _counts(rs)) for m, rs in sorted(by_module.items()))
    return {
        'schema': SCHEMA,
        'run': {
            'id': run['id'],
            'label': run['label'],
            'started': utc(run['started']),
            'finished': utc(run['finished']),
            'duration_s': round(run['finished'] - run['started'], 1),
            'tier': run['tier'],
            'selection': {
                'patterns': list(run.get('patterns') or []),
                'include_tags': list(run.get('include_tags') or []),
                'exclude_tags': list(run.get('exclude_tags') or []),
            },
            'jobs': run['jobs'],
            'aborted': bool(run.get('aborted')),
            'abort_reason': run.get('abort_reason'),
            'exit_code': run.get('exit_code'),
            'leftover_buckets': sorted(leftovers or []),
            'cleanup': {
                'buckets_created': (cleanup or {}).get('created'),
                'buckets_remaining': sorted((cleanup or {}).get('remaining') or []),
                'verified': bool((cleanup or {}).get('verified')),
                'stale_removed': len((cleanup or {}).get('stale_removed') or []),
                'stale_failed': sorted((cleanup or {}).get('stale_failed') or []),
            },
        },
        'tool': {
            'name': 's3tests',
            'version': __version__,
            'build': __build__,
            'python': platform.python_version(),
            'os': '%s %s' % (platform.system(), platform.release()),
            'host': socket.gethostname(),
        },
        'target': {
            'label': run['label'],
            'endpoint': cfg.endpoint,
            'region': cfg.region,
            'addressing': cfg.addressing,
            'second_user': cfg.alt is not None,
            'tls': cfg.endpoint.lower().startswith('https://'),
        },
        'test_set': {
            'catalog_size': len(all_tests),
            'catalog_fingerprint': fingerprint(t.id for t in all_tests),
            'selected': len(selected),
            'selected_fingerprint': fingerprint(t.id for t in selected),
        },
        'summary': summary,
        'network': dict(net_stats or {}),
        'tests': records,
    }


def write(path, doc):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write('\n')
    os.rename(tmp, path)  # atomic: readers never see a half-written file


def load(path):
    """Load a results document; accepts a file, a label directory (its latest.json),
    or a run directory (its results.json)."""
    if os.path.isdir(path):
        for name in ('latest.json', 'results.json'):
            if os.path.isfile(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise ValueError('%s: no latest.json or results.json' % path)
    with open(path) as f:
        doc = json.load(f)
    if not str(doc.get('schema', '')).startswith('s3tests.results/'):
        raise ValueError('%s: not an s3tests results file (use --results-dir or --json from s3tests 1.2+)' % path)
    doc['_path'] = path
    return doc


def history_line(doc, changes=None):
    s = doc['summary']
    line = {
        'run_id': doc['run']['id'], 'label': doc['run']['label'], 'started': doc['run']['started'],
        'duration_s': doc['run']['duration_s'], 'tier': doc['run']['tier'],
        'version': doc['tool']['version'], 'build': doc['tool']['build'],
        'selected_fingerprint': doc['test_set']['selected_fingerprint'],
        'tests': s['tests'], 'pass': s['pass'], 'fail': s['fail'], 'error': s['error'], 'skip': s['skip'],
        'pass_rate': s['pass_rate'], 'aborted': doc['run']['aborted'], 'exit_code': doc['run']['exit_code'],
        'outcomes': dict((t['id'], t['outcome'][0]) for t in doc['tests']),
    }
    if changes is not None:
        line['changes'] = changes
    return json.dumps(line, sort_keys=True)
