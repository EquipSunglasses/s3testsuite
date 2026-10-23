"""Assertion helpers with S3-aware failure messages.

Plain `assert` works in tests too; these helpers add the HTTP status, S3 error
code and request line to the failure message.
"""


# Why a test was skipped. Comparisons across platforms depend on telling
# "the platform does not support this" apart from "this run was not set up for it".
NOT_CONFIGURED = 'not-configured'      # the run lacks something (second user, TLS, wait time)
NOT_IMPLEMENTED = 'not-implemented'    # the server answered NotImplemented / 501
DISABLED = 'disabled-by-policy'        # the platform or account switched the feature off
NOT_RUN = 'not-run'                    # the run stopped before the test finished
SKIP_CATEGORIES = (NOT_CONFIGURED, NOT_IMPLEMENTED, DISABLED, NOT_RUN)


class SkipTest(Exception):
    """Raise to mark a test as skipped; category is one of SKIP_CATEGORIES."""

    def __init__(self, reason, category=NOT_CONFIGURED):
        Exception.__init__(self, reason)
        self.category = category


class S3AssertionError(AssertionError):
    """An assertion about an S3 response; carries the status, error code and request."""

    def __init__(self, message, resp=None):
        AssertionError.__init__(self, message)
        self.http_status = getattr(resp, 'status', None)
        self.s3_code = getattr(resp, 'error_code', None) if resp is not None else None
        self.request = ('%s %s' % (resp.method, resp.url.split('?')[0])) if resp is not None else None


def skip(reason, category=NOT_CONFIGURED):
    raise SkipTest(reason, category)


def _tuple(v):
    if v is None:
        return ()
    return tuple(v) if isinstance(v, (tuple, list, set, frozenset)) else (v,)


def _resp_detail(resp):
    code, msg = resp.error_code, resp.error_message
    if code is None and resp.status >= 300 and resp.method != 'HEAD':
        code = '(no S3 error code in body)'
    return '%s%s [%s]' % (code or '', (': ' + msg) if msg else '', resp.describe())


def eq(actual, expected, what='value'):
    if actual != expected:
        raise AssertionError('%s: expected %r, got %r' % (what, expected, actual))


def true(cond, message):
    if not cond:
        raise AssertionError(message)


def contains(container, item, what='collection'):
    if item not in container:
        raise AssertionError('%s: expected to contain %r, got %r' % (what, item, container))


def not_contains(container, item, what='collection'):
    if item in container:
        raise AssertionError('%s: expected not to contain %r, got %r' % (what, item, container))


def ok(resp, what='request'):
    """Assert a 2xx response and return it."""
    if not (200 <= resp.status < 300):
        raise S3AssertionError('%s: expected 2xx, got HTTP %d %s' % (what, resp.status, _resp_detail(resp)), resp)
    return resp


def status(resp, expected, what='request'):
    if resp.status not in _tuple(expected):
        raise S3AssertionError('%s: expected HTTP %s, got HTTP %d %s' % (
            what, '/'.join(str(s) for s in _tuple(expected)), resp.status, _resp_detail(resp)), resp)
    return resp


def error(resp, expected_status, code=None, what='request'):
    """Assert an error response with one of the given statuses and S3 codes."""
    statuses, codes = _tuple(expected_status), _tuple(code)
    if resp.status not in statuses or (codes and resp.error_code not in codes):
        raise S3AssertionError('%s: expected HTTP %s%s, got HTTP %d %s' % (
            what, '/'.join(str(s) for s in statuses),
            (' ' + '|'.join(codes)) if codes else '', resp.status, _resp_detail(resp)), resp)
    return resp


def skip_if_unimplemented(resp, feature):
    if resp.status == 501 or resp.error_code in ('NotImplemented', 'NotSupported'):
        raise SkipTest('%s not implemented by server (HTTP %d %s)' % (feature, resp.status, resp.error_code),
                       NOT_IMPLEMENTED)
    return resp


# Responses meaning a feature is switched off by platform or account policy
# rather than broken. Matched on S3 error code or on message text (AWS
# reports Block Public Access and SSE-C blocking as plain AccessDenied).
_DISABLED = (
    ('AccessControlListNotSupported', None,
     'ACLs are disabled on this bucket (Object Ownership = BucketOwnerEnforced)'),
    ('InvalidBucketAclWithObjectOwnership', None,
     'ACLs are disabled on this bucket (Object Ownership = BucketOwnerEnforced)'),
    (None, 'Block Public Access',
     'public ACLs or policies are blocked by S3 Block Public Access (account-level setting)'),
    (None, 'Customer provided keys',
     'SSE-C is blocked for this bucket (AWS blocks SSE-C on new buckets by default)'),
)


def skip_if_disabled(resp, feature):
    """SKIP (not FAIL) when the platform reports the feature as administratively disabled."""
    msg = resp.error_message or ''
    for code, text, reason in _DISABLED:
        if (code and resp.error_code == code) or (text and text in msg):
            raise SkipTest('%s: %s' % (feature, reason), DISABLED)
    return resp
