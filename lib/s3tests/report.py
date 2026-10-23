"""Console and JUnit XML reporting. The JSON results document lives in results.py."""

import os
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET

from . import LIBDIR, __build__, __version__, version_line
from .runner import BAD, ERROR, FAIL, PASS, SKIP, XFAIL, XPASS

STATUSES = (PASS, FAIL, ERROR, SKIP, XFAIL, XPASS)
_COLORS = {PASS: '32', FAIL: '31', ERROR: '35', SKIP: '33', XFAIL: '36', XPASS: '34'}
_CTRL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _clean(s):
    return _CTRL.sub('?', s or '')


class Console(object):
    def __init__(self, stream=None, color=None, verbose=0, show_skips=True, quiet=False):
        self.quiet = quiet
        self.stream = stream or sys.stdout
        self.color = self.stream.isatty() if color is None else color
        self.verbose = verbose
        self.show_skips = show_skips
        self.done = 0
        self.total = 0

    def _c(self, status, text):
        return '\033[%sm%s\033[0m' % (_COLORS[status], text) if self.color else text

    def start(self, total, cfg, tier='full', label=None, run_id=None):
        self.total = total
        w = self.stream.write
        w('%s from %s\n' % (version_line(), LIBDIR))
        w('target %s: %s (%s, %s-style)%s\n' % (label or cfg.label, cfg.endpoint, cfg.region, cfg.addressing,
                                               ('  run %s' % run_id) if run_id else ''))
        w('running %d tests (tier %s) with %d job(s), %ds limit per test\n\n' % (
            total, tier, cfg.jobs, cfg.test_timeout))
        self.stream.flush()

    def result(self, r):
        self.done += 1
        if self.quiet and r.status not in BAD:
            return
        w = self.stream.write
        cat = ('  [%s]' % r.skip_category) if r.status == SKIP and getattr(r, 'skip_category', None) else ''
        w('%s %6.2fs  %s%s\n' % (self._c(r.status, '%-5s' % r.status), r.duration, r.test.id, cat))
        if r.status in BAD or (r.status == SKIP and self.verbose) or r.status in (XFAIL, XPASS):
            for line in (r.message or '').splitlines()[:6]:
                w('              %s\n' % line)
        if r.status in BAD and self.verbose:
            for line in r.detail.rstrip().splitlines()[-12:]:
                w('              | %s\n' % line)
            if r.trace:
                w('              http history:\n')
                for t in r.trace[-15:]:
                    for line in t.splitlines():
                        w('                %s\n' % line)
        for wmsg in r.warnings:
            w('              warning: %s\n' % wmsg)
        self.stream.flush()

    def summary(self, results, elapsed, aborted=None):
        w = self.stream.write
        counts = dict((s, 0) for s in STATUSES)
        by_mod = {}
        for r in results:
            counts[r.status] += 1
            m = by_mod.setdefault(r.test.module, dict((s, 0) for s in STATUSES))
            m[r.status] += 1
        w('\n%-14s %5s %5s %5s %5s %5s %5s\n' % ('module', 'pass', 'fail', 'error', 'skip', 'xfail', 'xpass'))
        for mod in sorted(by_mod):
            c = by_mod[mod]
            w('%-14s %5d %5d %5d %5d %5d %5d\n' % (mod, c[PASS], c[FAIL], c[ERROR], c[SKIP], c[XFAIL], c[XPASS]))
        parts = ', '.join(self._c(st, '%d %s' % (counts[st], st.lower())) for st in STATUSES if counts[st])
        w('\nResults: %s (%d tests in %.1fs)\n' % (parts or 'no tests run', len(results), elapsed))
        passed = counts[PASS] + counts[XPASS]
        attempted = passed + counts[FAIL] + counts[XFAIL] + counts[ERROR]
        if attempted:
            w('Pass rate: %.1f%% (%d of %d attempted; expected failures count as failures)\n'
              % (100.0 * passed / attempted, passed, attempted))
        cats = {}
        for r in results:
            if r.status == SKIP:
                c = getattr(r, 'skip_category', None) or 'unspecified'
                cats[c] = cats.get(c, 0) + 1
        if cats:
            w('Skipped: %s\n' % ', '.join('%d %s' % (n, c) for c, n in sorted(cats.items(), key=lambda kv: -kv[1])))
        if aborted:
            w('Run stopped early: %s\n' % aborted)
        failed = [r for r in results if r.status in BAD]
        if failed:
            w('\nFailures:\n')
            for r in failed:
                first = (r.message or '').splitlines()[0] if r.message else ''
                w('  %-5s %s: %s\n' % (r.status, r.test.id, first[:160]))
            if not self.verbose:
                w('\nrerun with -v for tracebacks and HTTP history, e.g. s3tests -v %s\n' % failed[0].test.id)
        self.stream.flush()


def write_junit(path, results, cfg, elapsed, label=None, run_id=None):
    label = label or cfg.label
    root = ET.Element('testsuites', name='s3tests %s' % label, tests=str(len(results)), time='%.3f' % elapsed)
    by_mod = {}
    for r in results:
        by_mod.setdefault(r.test.module, []).append(r)
    for mod in sorted(by_mod):
        rs = by_mod[mod]
        suite = ET.SubElement(root, 'testsuite', name='s3tests.' + mod, tests=str(len(rs)),
                              failures=str(sum(1 for r in rs if r.status == FAIL)),
                              errors=str(sum(1 for r in rs if r.status == ERROR)),
                              skipped=str(sum(1 for r in rs if r.status in (SKIP, XFAIL))),
                              time='%.3f' % sum(r.duration for r in rs),
                              hostname=socket.gethostname(),
                              timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        props = ET.SubElement(suite, 'properties')
        for k, v in (('label', label), ('run_id', run_id or ''), ('endpoint', cfg.endpoint),
                     ('region', cfg.region), ('addressing', cfg.addressing),
                     ('version', __version__), ('build', __build__)):
            ET.SubElement(props, 'property', name=k, value=str(v))
        for r in rs:
            tc = ET.SubElement(suite, 'testcase', classname='s3tests.' + mod, name=r.test.name,
                               time='%.3f' % r.duration)
            if r.status == FAIL:
                el = ET.SubElement(tc, 'failure', message=_clean(r.message)[:1000])
                el.text = _clean(r.detail)
            elif r.status == ERROR:
                el = ET.SubElement(tc, 'error', message=_clean(r.message)[:1000])
                el.text = _clean(r.detail)
            elif r.status in (SKIP, XFAIL):
                cat = getattr(r, 'skip_category', None) or ('expected-failure' if r.status == XFAIL else '')
                ET.SubElement(tc, 'skipped', message=_clean('%s [%s]: %s' % (r.status, cat, r.message))[:1000])
            if r.trace:
                so = ET.SubElement(tc, 'system-out')
                so.text = _clean('\n'.join(r.trace))
    _mkparent(path)
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def _mkparent(path):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
