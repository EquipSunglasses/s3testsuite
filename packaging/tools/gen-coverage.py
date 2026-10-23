#!/usr/bin/env python3
"""Generate a standalone COVERAGE.html listing every s3tests test.

Usage: python3 packaging/tools/gen-coverage.py [output.html]
Reads the test functions directly, so the page always matches the code.
"""

import html
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'lib'))

from s3tests import __version__, runner  # noqa: E402

TIER_INFO = (
    ('smoke', 'Core data path: buckets, put/get/head/delete, listing, multipart, copy, batch delete, presign.'),
    ('standard', 'What S3 applications commonly rely on; no slow, large, limit-probing or newer optional features.'),
    ('full', 'Every test, including ACLs, policy, CORS, SSE, Object Lock, limits and checksums.'),
)

MODULE_INFO = {
    'bucket': ('Buckets', 'Create, head, delete, naming rules, ownership and cross-user isolation.'),
    'object': ('Objects', 'Single-object PUT/GET/HEAD/DELETE, metadata, key names, ranges, conditionals, integrity.'),
    'listing': ('Listing', 'ListObjects v1 and v2: prefixes, delimiters, paging, markers and encoding.'),
    'multipart': ('Multipart upload', 'Initiate, upload, complete, abort, list, part copy and error cases.'),
    'copy': ('Copy', 'CopyObject within and across buckets, metadata directives and conditions.'),
    'versioning': ('Versioning', 'Versions, delete markers, suspended (null) versions and version listing.'),
    'multidelete': ('Multi-object delete', 'DeleteObjects batches, quiet mode and limits.'),
    'tagging': ('Tagging', 'Object and bucket tag sets and their limits.'),
    'lifecycle': ('Lifecycle', 'Lifecycle configuration rules, validation and optional expiration processing.'),
    'cors': ('CORS', 'CORS configuration and preflight handling.'),
    'policy': ('Bucket policy', 'Policy storage, validation and enforcement.'),
    'acl': ('ACLs', 'Canned ACLs, explicit grants and anonymous access.'),
    'auth': ('Authentication', 'SigV4 failures, clock skew and presigned URLs.'),
    'sse': ('Server-side encryption', 'SSE-C, SSE-S3 and default bucket encryption.'),
    'objectlock': ('Object Lock', 'Lock-enabled buckets, GOVERNANCE retention and legal hold.'),
}
ORDER = list(MODULE_INFO)

NOTES = {
    'multiuser': 'needs [alt] user',
    'slow': 'long running',
    'lifecycle_expiration': 'needs lifecycle_wait_seconds',
    'sse_c': 'https or allow_insecure_sse',
}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'COVERAGE.html')
    tests = runner.discover()
    mods = {}
    for t in tests:
        mods.setdefault(t.module, []).append(t)
    order = [m for m in ORDER if m in mods] + sorted(m for m in mods if m not in ORDER)

    all_tags = {}
    for t in tests:
        for tag in t.tags - {t.module, 'smoke'}:
            all_tags[tag] = all_tags.get(tag, 0) + 1

    e = html.escape
    rows_nav, sections = [], []
    for m in order:
        title, blurb = MODULE_INFO.get(m, (m, ''))
        ts = mods[m]
        rows_nav.append('<a href="#m-%s"><span>%s</span><b>%d</b></a>' % (m, e(title), len(ts)))
        trs = []
        for t in ts:
            tags = sorted(t.tags - {t.module, 'smoke'})
            chips = ''.join('<span class="chip" title="%s">%s</span>' % (e(NOTES.get(g, '')), e(g)) for g in tags)
            trs.append('<tr data-search="%s" data-tags="%s" data-tier="%s"><td class="id"><code>%s</code></td>'
                       '<td>%s</td><td class="tier"><span class="t t-%s">%s</span></td><td class="tags">%s</td></tr>' % (
                           e((t.id + ' ' + t.doc + ' ' + ' '.join(tags)).lower()), e(' '.join(tags)), t.tier,
                           e(t.id), e(t.doc), t.tier, t.tier, chips))
        sections.append(
            '<section id="m-%s" class="mod"><header><h2>%s <span class="count">%d</span></h2>'
            '<p>%s <code>s3tests %s</code></p></header>'
            '<table><thead><tr><th>Test id</th><th>What it checks</th><th>Tier</th><th>Tags</th></tr></thead>'
            '<tbody>%s</tbody></table></section>' % (m, e(title), len(ts), e(blurb), e(m), ''.join(trs)))

    tier_counts = dict((tr, len(runner.select(tests, tier=tr))) for tr in runner.TIERS)
    tier_cards = ''.join('<div class="tiercard"><b>%d</b><span><code>s3tests --%s</code></span><p>%s</p></div>' % (
        tier_counts[tr], tr, e(desc)) for tr, desc in TIER_INFO)
    tier_buttons = ''.join('<button class="tierbtn%s" data-tier="%s">%s <i>%d</i></button>' % (
        ' on' if tr == 'full' else '', tr, tr, tier_counts[tr]) for tr in runner.TIERS)
    tag_buttons = ''.join('<button class="tagbtn" data-tag="%s">%s <i>%d</i></button>' % (e(g), e(g), n)
                          for g, n in sorted(all_tags.items()))

    page = TEMPLATE % {
        'version': e(__version__),
        'total': len(tests),
        'modules': len(order),
        'tags': len(all_tags),
        'date': time.strftime('%Y-%m-%d'),
        'nav': ''.join(rows_nav),
        'tagbuttons': tag_buttons,
        'tiercards': tier_cards,
        'tierbuttons': tier_buttons,
        'sections': ''.join(sections),
        'notes': json.dumps(NOTES),
    }
    with open(out, 'w') as f:
        f.write(page)
    print('wrote %s (%d tests, %d modules)' % (out, len(tests), len(order)))


TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>s3tests coverage</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1f21;--muted:#5f6368;--line:#e2e2de;--accent:#1f6f5c;--chip:#eef3f1;--chip-ink:#1f5c4d;--hl:#fff6d5}
@media (prefers-color-scheme:dark){:root{--bg:#141617;--panel:#1c1f21;--ink:#e6e6e3;--muted:#9aa0a6;--line:#2c3033;--accent:#5fc3a6;--chip:#22302c;--chip-ink:#9fdcc9;--hl:#3a3420}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
.wrap{max-width:1180px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 24px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:130px}
.stat b{display:block;font-size:26px;color:var(--accent)}
.stat span{color:var(--muted);font-size:13px}
.layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;align-items:start}
main{min-width:0}
nav{position:sticky;top:16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px}
nav a{display:flex;justify-content:space-between;padding:6px 10px;border-radius:6px;color:var(--ink);text-decoration:none;font-size:14px}
nav a:hover{background:var(--chip)}
nav b{color:var(--muted);font-weight:500}
.controls{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:16px}
input[type=search]{width:100%%;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:15px}
.tagrow{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tagbtn{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:999px;padding:3px 10px;font-size:12.5px;cursor:pointer}
.tagbtn i{font-style:normal;color:var(--muted)}
.tagbtn.on{background:var(--accent);border-color:var(--accent);color:#fff}
.tagbtn.on i{color:#e6fff7}
.tiers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:24px}
.tiercard{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.tiercard b{font-size:22px;color:var(--accent);margin-right:8px}
.tiercard p{margin:6px 0 0;color:var(--muted);font-size:13px}
.lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;align-self:center;margin-right:4px}
.tierbtn{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:6px;padding:3px 10px;font-size:12.5px;cursor:pointer}
.tierbtn i{font-style:normal;color:var(--muted)}
.tierbtn.on{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.tierbtn.on i{color:var(--bg)}
td.tier{width:1%%;white-space:nowrap}
.t{font-size:12px;border-radius:4px;padding:1px 6px;border:1px solid var(--line)}
.t-smoke{background:var(--accent);color:#fff;border-color:var(--accent)}
.t-standard{background:var(--chip);color:var(--chip-ink)}
.t-full{color:var(--muted)}
.shown{color:var(--muted);font-size:13px;margin-top:8px}
.mod{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:16px;overflow:hidden}
.mod header{padding:14px 16px 4px}
.mod h2{margin:0;font-size:18px}
.count{color:var(--muted);font-weight:500;font-size:14px}
.mod p{margin:4px 0 10px;color:var(--muted);font-size:14px}
.mod p code{color:var(--ink)}
table{width:100%%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;border-top:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
td.id{white-space:nowrap}
td.tags{width:1%%;white-space:nowrap}
.chip{display:inline-block;background:var(--chip);color:var(--chip-ink);border-radius:999px;padding:1px 8px;font-size:12px;margin:1px 2px}
tr.hide{display:none}
.mod.empty{display:none}
footer{color:var(--muted);font-size:13px;margin-top:24px}
@media (max-width:820px){.tiers{grid-template-columns:minmax(0,1fr)}td.tier{width:auto;display:inline-block}.layout{grid-template-columns:minmax(0,1fr)}nav{position:static;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}nav a{min-width:0}nav a span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}thead{display:none}table,tbody,tr,td{display:block;width:100%%}tr{border-top:1px solid var(--line);padding:8px 0}td{border:0;padding:2px 16px}td.id{white-space:normal;word-break:break-all}td.tags{width:100%%;white-space:normal}}
@media print{nav,.controls{display:none}.layout{display:block}.mod{break-inside:avoid-page}}
</style>
</head>
<body>
<div class="wrap">
<h1>s3tests coverage</h1>
<p class="sub">S3 protocol conformance suite, version %(version)s. Generated %(date)s from the test code.</p>
<div class="stats">
<div class="stat"><b>%(total)d</b><span>tests</span></div>
<div class="stat"><b>%(modules)d</b><span>modules</span></div>
<div class="stat"><b>%(tags)d</b><span>tags for selection</span></div>
</div>
<div class="tiers">%(tiercards)s</div>
<div class="layout">
<nav>%(nav)s</nav>
<main>
<div class="controls">
<input type="search" id="q" placeholder="Filter by test id, description or tag (e.g. presign, NoSuchKey, delete marker)">
<div class="tagrow tierrow"><span class="lbl">Tier</span>%(tierbuttons)s</div>
<div class="tagrow"><span class="lbl">Tags</span>%(tagbuttons)s</div>
<div class="shown" id="shown"></div>
</div>
%(sections)s
<footer>Run a module with <code>s3tests &lt;module&gt;</code>, a tag with <code>s3tests -t &lt;tag&gt;</code>, one test with <code>s3tests &lt;module.test_name&gt;</code>. Regenerate this page with <code>make coverage</code>.</footer>
</main>
</div>
</div>
<script>
(function(){
  var q=document.getElementById('q'),shown=document.getElementById('shown');
  var rows=[].slice.call(document.querySelectorAll('tbody tr'));
  var mods=[].slice.call(document.querySelectorAll('.mod'));
  var active={},tier='full',order={smoke:0,standard:1,full:2};
  [].slice.call(document.querySelectorAll('.tierbtn')).forEach(function(b){
    b.addEventListener('click',function(){tier=b.getAttribute('data-tier');
      [].slice.call(document.querySelectorAll('.tierbtn')).forEach(function(o){o.classList.toggle('on',o===b)});apply()});
  });
  [].slice.call(document.querySelectorAll('.tagbtn')).forEach(function(b){
    b.addEventListener('click',function(){var t=b.getAttribute('data-tag');
      if(active[t]){delete active[t];b.classList.remove('on')}else{active[t]=1;b.classList.add('on')}apply()});
  });
  function apply(){
    var term=q.value.trim().toLowerCase(),tags=Object.keys(active),n=0;
    rows.forEach(function(r){
      var ok=(!term||r.getAttribute('data-search').indexOf(term)>=0)&&order[r.getAttribute('data-tier')]<=order[tier];
      if(ok&&tags.length){var rt=' '+r.getAttribute('data-tags')+' ';ok=tags.some(function(t){return rt.indexOf(' '+t+' ')>=0})}
      r.classList.toggle('hide',!ok);if(ok)n++;
    });
    mods.forEach(function(m){m.classList.toggle('empty',!m.querySelector('tbody tr:not(.hide)'))});
    shown.textContent=(term||tags.length||tier!=='full')?n+' of '+rows.length+' tests shown':'';
  }
  q.addEventListener('input',apply);
})();
</script>
</body>
</html>
'''

if __name__ == '__main__':
    main()
