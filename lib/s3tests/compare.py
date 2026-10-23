"""Compare results documents: across solutions (--compare), run to run (--diff)
and over time (--trend). Standard library only."""

import csv
import html
import io
import json
import os


MARK = {'pass': 'PASS', 'fail': 'FAIL', 'error': 'ERROR', 'skip': 'SKIP'}


def cell(t):
    """Short, comparable description of one test result."""
    if t is None:
        return '-'
    o = t['outcome']
    if o == 'skip':
        return 'SKIP %s' % (t.get('skip_category') or '')
    if o in ('fail', 'error'):
        kind = t.get('failure_kind') or (t.get('message') or '').split(':')[0]
        return ('%s %s' % (MARK[o], kind[:48])).strip()
    return 'PASS'


def _index(doc):
    return dict((t['id'], t) for t in doc['tests'])


def _names(docs):
    """Column names: the label, made unique with the run date where labels repeat."""
    labels = [d['run']['label'] for d in docs]
    out = []
    for d in docs:
        name = d['run']['label']
        if labels.count(name) > 1:
            name = '%s@%s' % (name, d['run']['started'][:16].replace('T', ' '))
        out.append(name)
    return out


def comparability(docs):
    notes = []
    fps = set(d['test_set']['selected_fingerprint'] for d in docs)
    if len(fps) > 1:
        notes.append('runs used different test selections; only tests present in a run are compared for it')
    builds = set('%s/%s' % (d['tool']['version'], d['tool']['build']) for d in docs)
    if len(builds) > 1:
        notes.append('runs used different s3tests builds: %s' % ', '.join(sorted(builds)))
    for d in docs:
        if not d['target'].get('second_user'):
            notes.append('%s: no second user configured, cross-user tests skipped' % d['run']['label'])
        if d['run'].get('aborted'):
            notes.append('%s: run stopped early (%s)' % (d['run']['label'], d['run'].get('abort_reason')))
    return notes


# --- diff: one run against another --------------------------------------------

def diff(base, cur):
    b, c = _index(base), _index(cur)
    out = {'regressions': [], 'fixes': [], 'newly_skipped': [], 'no_longer_skipped': [],
           'failure_changed': [], 'added': sorted(set(c) - set(b)), 'removed': sorted(set(b) - set(c))}
    for tid in sorted(set(b) & set(c)):
        ob, oc = b[tid]['outcome'], c[tid]['outcome']
        row = {'id': tid, 'before': cell(b[tid]), 'after': cell(c[tid])}
        if ob == 'pass' and oc in ('fail', 'error'):
            out['regressions'].append(row)
        elif ob in ('fail', 'error') and oc == 'pass':
            out['fixes'].append(row)
        elif ob != 'skip' and oc == 'skip':
            out['newly_skipped'].append(row)
        elif ob == 'skip' and oc != 'skip':
            out['no_longer_skipped'].append(row)
        elif ob in ('fail', 'error') and oc in ('fail', 'error') and row['before'] != row['after']:
            out['failure_changed'].append(row)
    return out


def diff_counts(d):
    return dict((k, len(v)) for k, v in d.items())


def format_diff(base, cur, d):
    lines = ['%s (%s, %s) -> %s (%s, %s)' % (
        base['run']['label'], base['run']['started'], base['tool']['build'],
        cur['run']['label'], cur['run']['started'], cur['tool']['build'])]
    pr_b, pr_c = base['summary']['pass_rate'], cur['summary']['pass_rate']
    lines.append('pass rate %s%% -> %s%%' % (pr_b, pr_c))
    titles = [('regressions', 'Regressions (passed before, fail now)'), ('fixes', 'Fixed (failed before, pass now)'),
              ('failure_changed', 'Still failing, differently'), ('newly_skipped', 'Newly skipped'),
              ('no_longer_skipped', 'No longer skipped')]
    for key, title in titles:
        if d[key]:
            lines.append('\n%s: %d' % (title, len(d[key])))
            for row in d[key]:
                lines.append('  %-58s %s -> %s' % (row['id'], row['before'], row['after']))
    for key, title in (('added', 'Tests only in the newer run'), ('removed', 'Tests only in the older run')):
        if d[key]:
            lines.append('\n%s: %d' % (title, len(d[key])))
            lines.extend('  %s' % t for t in d[key][:30])
            if len(d[key]) > 30:
                lines.append('  ... %d more' % (len(d[key]) - 30))
    if not any(d[k] for k in d):
        lines.append('\nno changes')
    return '\n'.join(lines) + '\n'


# --- compare: several solutions side by side --------------------------------

def matrix(docs, show_all=False):
    idx = [_index(d) for d in docs]
    ids = sorted(set().union(*[set(i) for i in idx]))
    rows = []
    for tid in ids:
        cells = [cell(i.get(tid)) for i in idx]
        outcomes = set(i[tid]['outcome'] for i in idx if tid in i)
        differs = len(outcomes) > 1 or any(c == '-' for c in cells)
        if show_all or differs:
            rows.append((tid, cells, differs))
    return ids, rows


def _summary_rows(docs, names):
    rows = []
    for name, d in zip(names, docs):
        s = d['summary']
        rows.append([name, d['run']['started'][:16].replace('T', ' '), '%s/%s' % (d['tool']['version'], d['tool']['build']),
                     d['run']['tier'], str(s['tests']), str(s['pass']), str(s['fail']), str(s['error']), str(s['skip']),
                     '%s%%' % s['pass_rate'] if s['pass_rate'] is not None else 'n/a',
                     _visibility(d)])
    return rows


def _visibility(d):
    """'3/180 up to 2.7s': new buckets that returned 404 after CreateBucket, and the longest wait."""
    v = d.get('bucket_visibility')
    if not v or not v.get('checked'):
        return 'n/a'
    if not v['delayed']:
        return 'immediate'
    out = '%d/%d up to %.1fs' % (v['delayed'], v['checked'], v['max_wait_seconds'])
    if v['never_ready']:
        out += ', %d never' % v['never_ready']
    return out


def _module_rows(docs, names):
    mods = sorted(set().union(*[set(d['summary']['by_module']) for d in docs]))
    rows = []
    for m in mods:
        row = [m]
        for d in docs:
            c = d['summary']['by_module'].get(m)
            if not c:
                row.append('-')
            else:
                pr = '%s%%' % c['pass_rate'] if c['pass_rate'] is not None else 'n/a'
                row.append('%s (%d/%d)' % (pr, c['pass'], c['pass'] + c['fail'] + c['error']))
        rows.append(row)
    return rows


def _table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    fmt = '  '.join('%%-%ds' % w for w in widths)
    out = [fmt % tuple(headers), fmt % tuple('-' * w for w in widths)]
    out.extend(fmt % tuple(r) for r in rows)
    return '\n'.join(x.rstrip() for x in out)


SUMMARY_HEADERS = ['solution', 'started (UTC)', 'version/build', 'tier', 'tests', 'pass', 'fail', 'error', 'skip',
                   'pass rate', 'new bucket 404s']


def compare_text(docs, show_all=False):
    names = _names(docs)
    out = [_table(SUMMARY_HEADERS, _summary_rows(docs, names)), '',
           'Pass rate by module (passed/attempted; skips excluded):',
           _table(['module'] + names, _module_rows(docs, names))]
    ids, rows = matrix(docs, show_all)
    out.append('')
    out.append('%s (%d of %d tests):' % ('All tests' if show_all else 'Tests whose outcome differs', len(rows), len(ids)))
    out.append(_table(['test'] + names, [[tid] + cells for tid, cells, _ in rows]) if rows else '  none')
    notes = comparability(docs)
    if notes:
        out.append('\nNotes:')
        out.extend('  - %s' % n for n in notes)
    return '\n'.join(out) + '\n'


def compare_markdown(docs, show_all=False):
    names = _names(docs)

    def md(headers, rows):
        esc = lambda v: str(v).replace('|', '\\|')
        lines = ['| ' + ' | '.join(esc(h) for h in headers) + ' |', '|' + '---|' * len(headers)]
        lines.extend('| ' + ' | '.join(esc(c) for c in r) + ' |' for r in rows)
        return '\n'.join(lines)
    ids, rows = matrix(docs, show_all)
    out = ['## Summary', '', md(SUMMARY_HEADERS, _summary_rows(docs, names)), '',
           '## Pass rate by module', '', md(['module'] + names, _module_rows(docs, names)), '',
           '## %s (%d of %d tests)' % ('All tests' if show_all else 'Tests whose outcome differs', len(rows), len(ids)), '',
           md(['test'] + names, [['`%s`' % tid] + cells for tid, cells, _ in rows]) if rows else 'none']
    notes = comparability(docs)
    if notes:
        out += ['', '## Notes', ''] + ['- %s' % n for n in notes]
    return '\n'.join(out) + '\n'


def compare_csv(docs):
    """One row per test and solution: easy to pivot in a spreadsheet or load into a database."""
    names = _names(docs)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['solution', 'run_id', 'started_utc', 'version', 'build', 'tier', 'test_id', 'module', 'test_tier',
                'outcome', 'status', 'skip_category', 'failure_kind', 'http_status', 's3_code', 'duration_s',
                'message'])
    for name, d in zip(names, docs):
        for t in d['tests']:
            w.writerow([name, d['run']['id'], d['run']['started'], d['tool']['version'], d['tool']['build'],
                        d['run']['tier'], t['id'], t['module'], t['tier'], t['outcome'], t['status'],
                        t['skip_category'] or '', t.get('failure_kind') or '', t['http_status'] or '',
                        t['s3_code'] or '', t['duration_s'], t['message']])
    return buf.getvalue()


def compare_html(docs, show_all=True):
    names = _names(docs)
    e = html.escape
    ids, rows = matrix(docs, show_all=True)

    def tbl(headers, body_rows, cls=''):
        h = ''.join('<th>%s</th>' % e(x) for x in headers)
        b = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % e(str(c)) for c in r) for r in body_rows)
        return '<table class="%s"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (cls, h, b)

    trs = []
    for tid, cells, differs in rows:
        tds = ''.join('<td class="o-%s">%s</td>' % (c.split(' ')[0].lower() if c != '-' else 'none', e(c)) for c in cells)
        trs.append('<tr class="%s"><td><code>%s</code></td>%s</tr>' % ('diff' if differs else 'same', e(tid), tds))
    notes = ''.join('<li>%s</li>' % e(n) for n in comparability(docs))
    return HTML_PAGE % {
        'summary': tbl(SUMMARY_HEADERS, _summary_rows(docs, names)),
        'modules': tbl(['module'] + names, _module_rows(docs, names)),
        'heads': ''.join('<th>%s</th>' % e(n) for n in names),
        'rows': ''.join(trs), 'ndiff': sum(1 for r in rows if r[2]), 'ntotal': len(ids),
        'notes': ('<h2>Notes</h2><ul>%s</ul>' % notes) if notes else '',
    }


HTML_PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>S3 conformance comparison</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1f21;--muted:#5f6368;--line:#e2e2de;--pass:#e5f3ec;--pass-ink:#1b5e43;--fail:#fbe9e7;--fail-ink:#9b2c1c;--skip:#f1f1ee;--skip-ink:#5f6368;--err:#fdf0dc;--err-ink:#8a5300}
@media (prefers-color-scheme:dark){:root{--bg:#141617;--panel:#1c1f21;--ink:#e6e6e3;--muted:#9aa0a6;--line:#2c3033;--pass:#16302a;--pass-ink:#8fdcbf;--fail:#3a1d19;--fail-ink:#f2a497;--skip:#25292b;--skip-ink:#9aa0a6;--err:#3a2c14;--err-ink:#f0c27a}}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:16px;margin:28px 0 8px}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%%}th,td{padding:6px 10px;border-top:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.o-pass{background:var(--pass);color:var(--pass-ink)}.o-fail{background:var(--fail);color:var(--fail-ink)}
.o-error{background:var(--err);color:var(--err-ink)}.o-skip,.o-none{background:var(--skip);color:var(--skip-ink)}
label{display:inline-flex;gap:6px;align-items:center;margin:4px 0 10px;color:var(--muted)}
body.only-diff tr.same{display:none}
</style></head><body class="only-diff"><div class="wrap">
<h1>S3 conformance comparison</h1>
<div class="scroll">%(summary)s</div>
<h2>Pass rate by module (passed/attempted; skips excluded)</h2>
<div class="scroll">%(modules)s</div>
<h2>Tests (%(ndiff)s of %(ntotal)s differ)</h2>
<label><input type="checkbox" id="all"> show all tests</label>
<div class="scroll"><table><thead><tr><th>test</th>%(heads)s</tr></thead><tbody>%(rows)s</tbody></table></div>
%(notes)s
</div><script>document.getElementById('all').addEventListener('change',function(e){document.body.classList.toggle('only-diff',!e.target.checked)});</script>
</body></html>
'''


def compare(docs, fmt='text', show_all=False):
    if fmt == 'csv':
        return compare_csv(docs)
    if fmt == 'md':
        return compare_markdown(docs, show_all)
    if fmt == 'html':
        return compare_html(docs, show_all)
    if fmt == 'json':
        names = _names(docs)
        ids, rows = matrix(docs, show_all)
        return json.dumps({'solutions': names, 'notes': comparability(docs),
                           'summary': dict((n, d['summary']) for n, d in zip(names, docs)),
                           'bucket_visibility': dict((n, d.get('bucket_visibility')) for n, d in zip(names, docs)),
                           'tests': [dict(id=tid, results=dict(zip(names, cells))) for tid, cells, _ in rows]},
                          indent=1, sort_keys=True) + '\n'
    return compare_text(docs, show_all)


# --- trend: one solution over time -------------------------------------------

def read_history(label_dir, last=None):
    path = os.path.join(label_dir, 'history.jsonl')
    if not os.path.isfile(path):
        raise ValueError('%s: no history.jsonl (runs must use --results-dir)' % label_dir)
    runs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    continue
    return runs[-last:] if last else runs


def trend(label_dir, last=20):
    runs = read_history(label_dir, last)
    if not runs:
        return 'no runs recorded in %s\n' % label_dir
    headers = ['started (UTC)', 'build', 'tier', 'tests', 'pass', 'fail', 'error', 'skip', 'pass rate', 'changes']
    rows = []
    for r in runs:
        ch = r.get('changes') or {}
        bits = []
        if ch.get('regressions'):
            bits.append('%d regressed' % ch['regressions'])
        if ch.get('fixes'):
            bits.append('%d fixed' % ch['fixes'])
        if r.get('exit_code') == 3 and not r['tests']:
            bits.append('DID NOT START (endpoint unreachable or credentials rejected)')
        elif r.get('aborted'):
            bits.append('stopped early')
        rows.append([r['started'][:16].replace('T', ' '), r['build'], r['tier'], r['tests'], r['pass'], r['fail'],
                     r['error'], r['skip'], '%s%%' % r['pass_rate'] if r['pass_rate'] is not None else 'n/a',
                     ', '.join(bits)])
    out = ['%s: last %d run(s)' % (runs[-1]['label'], len(runs)), _table(headers, [[str(c) for c in r] for r in rows])]
    # A test is flaky when its outcome changes back and forth between runs of the same build.
    flips = {}
    for prev, cur in zip(runs, runs[1:]):
        if prev['build'] != cur['build']:
            continue
        for tid, o in cur.get('outcomes', {}).items():
            p = prev.get('outcomes', {}).get(tid)
            if p and p != o and 's' not in (p, o):
                flips[tid] = flips.get(tid, 0) + 1
    if flips:
        out.append('\nFlaky (outcome changed between runs of the same build):')
        for tid, n in sorted(flips.items(), key=lambda kv: (-kv[1], kv[0])):
            seq = ''.join(r.get('outcomes', {}).get(tid, '.') for r in runs)
            out.append('  %-58s %d change(s)  %s' % (tid, n, seq))
        out.append('  (sequence oldest to newest: p=pass f=fail e=error s=skip .=not run)')
    return '\n'.join(out) + '\n'
