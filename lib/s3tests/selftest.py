"""Offline self-test: verifies the SigV4 signer against AWS reference vectors.

Needs no endpoint or credentials, so it is safe to run during a build
build and right after installing on a new host.
"""

import datetime
import platform
import sys
import xml.etree.ElementTree as ET

from . import LIBDIR, sigv4, version_line
from .client import md5_hex, strip_namespaces


def _check(name, got, want, failures):
    if got == want:
        print('PASS  %s' % name)
    else:
        print('FAIL  %s\n      expected %r\n      got      %r' % (name, want, got))
        failures.append(name)


def run():
    failures = []
    print('%s self-test on Python %s (%s)\nlibrary: %s\n' % (
        version_line(), platform.python_version(), sys.executable, LIBDIR))

    # AWS SigV4 test suite: get-vanilla
    auth = sigv4.authorization_header(
        'GET', '/', [], {'Host': 'example.amazonaws.com', 'X-Amz-Date': '20150830T123600Z'},
        sigv4.EMPTY_SHA256, 'AKIDEXAMPLE', 'wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY',
        'us-east-1', 'service')
    _check('sigv4 get-vanilla', auth,
           'AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, '
           'SignedHeaders=host;x-amz-date, '
           'Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31', failures)

    # S3 documentation example: GET object with Range header
    auth = sigv4.authorization_header(
        'GET', '/test.txt', [],
        {'Host': 'examplebucket.s3.amazonaws.com', 'Range': 'bytes=0-9',
         'x-amz-content-sha256': sigv4.EMPTY_SHA256, 'x-amz-date': '20130524T000000Z'},
        sigv4.EMPTY_SHA256, 'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        'us-east-1')
    _check('sigv4 s3 GET object', auth.split('Signature=')[1],
           'f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41', failures)

    # S3 documentation example: presigned URL
    q = sigv4.presign_params('GET', '/test.txt', 'examplebucket.s3.amazonaws.com', [],
                             'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                             'us-east-1', 86400, datetime.datetime(2013, 5, 24))
    _check('sigv4 s3 presigned URL', dict(q)['X-Amz-Signature'],
           'aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404', failures)

    _check('uri encode key', sigv4.uri_encode('a b/c+d~e*ü', keep_slash=True), 'a%20b/c%2Bd~e%2A%C3%BC', failures)
    _check('canonical query', sigv4.canonical_query_string([('prefix', 'a b'), ('delimiter', '/'), ('versions', None)]),
           'delimiter=%2F&prefix=a%20b&versions=', failures)
    from .client import _md5
    impl = 'hashlib' if type(_md5(b'')).__name__ != 'PyMD5' else 'pure-Python fallback (FIPS host)'
    _check('md5 via %s' % impl, md5_hex(b'abc'), '900150983cd24fb0d6963f7d28e17f72', failures)
    root = strip_namespaces(ET.fromstring(
        b'<A xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><B>x</B></A>'))
    _check('xml namespace strip', root.findtext('B'), 'x', failures)

    print('\n%s: %d check(s) failed' % ('FAILED', len(failures)) if failures else '\nOK: all self-test checks passed')
    return 1 if failures else 0
