"""AWS Signature Version 4 (header and query-string forms) for S3.

Implemented from the published AWS specification with hashlib/hmac only.
Verified against the AWS reference vectors in s3tests.selftest.
"""

import datetime
import hashlib
import hmac
from urllib.parse import quote

ALGORITHM = 'AWS4-HMAC-SHA256'
UNSIGNED_PAYLOAD = 'UNSIGNED-PAYLOAD'
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()

# Headers that are never signed: hop-by-hop, or commonly added or rewritten by
# http.client and proxies after signing.
_UNSIGNED_HEADERS = frozenset([
    'authorization', 'user-agent', 'expect', 'connection',
    'content-length', 'accept-encoding', 'x-amzn-trace-id',
])


def uri_encode(value, keep_slash=False):
    """RFC 3986 encoding as SigV4 requires (unreserved: A-Z a-z 0-9 - _ . ~)."""
    if value is None:
        return ''
    return quote(str(value), safe='-_.~/' if keep_slash else '-_.~')


def sha256_hex(data):
    return hashlib.sha256(data or b'').hexdigest()


def _hmac(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def derive_signing_key(secret_key, datestamp, region, service):
    k_date = _hmac(('AWS4' + secret_key).encode('utf-8'), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, 'aws4_request')


def canonical_query_string(params):
    pairs = sorted((uri_encode(k), uri_encode('' if v is None else v)) for k, v in params)
    return '&'.join('%s=%s' % kv for kv in pairs)


def _normalize_value(value):
    return ' '.join(str(value).strip().split())


def canonical_request(method, canonical_uri, params, headers, payload_hash):
    """Return (canonical_request_string, signed_headers_string)."""
    hdrs = {}
    for name, value in headers.items():
        lname = name.lower().strip()
        if lname in _UNSIGNED_HEADERS:
            continue
        hdrs[lname] = _normalize_value(value)
    names = sorted(hdrs)
    canonical_headers = ''.join('%s:%s\n' % (n, hdrs[n]) for n in names)
    signed = ';'.join(names)
    creq = '\n'.join([
        method.upper(), canonical_uri, canonical_query_string(params),
        canonical_headers, signed, payload_hash,
    ])
    return creq, signed


def amz_dates(when=None):
    when = when or datetime.datetime.utcnow()
    return when.strftime('%Y%m%dT%H%M%SZ'), when.strftime('%Y%m%d')


def _signature(secret_key, datestamp, region, service, amz_date, scope, creq):
    sts = '\n'.join([ALGORITHM, amz_date, scope, sha256_hex(creq.encode('utf-8'))])
    key = derive_signing_key(secret_key, datestamp, region, service)
    return hmac.new(key, sts.encode('utf-8'), hashlib.sha256).hexdigest(), sts


def authorization_header(method, canonical_uri, params, headers, payload_hash,
                         access_key, secret_key, region, service='s3'):
    """Compute the Authorization header value.

    `headers` must already include host and x-amz-date (and normally
    x-amz-content-sha256). `canonical_uri` must already be URI-encoded.
    """
    amz_date = None
    for name, value in headers.items():
        if name.lower() == 'x-amz-date':
            amz_date = value
    if not amz_date:
        raise ValueError('x-amz-date header is required for SigV4 header signing')
    datestamp = amz_date[:8]
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    creq, signed = canonical_request(method, canonical_uri, params, headers, payload_hash)
    sig, _ = _signature(secret_key, datestamp, region, service, amz_date, scope, creq)
    return '%s Credential=%s/%s, SignedHeaders=%s, Signature=%s' % (
        ALGORITHM, access_key, scope, signed, sig)


def presign_params(method, canonical_uri, host, params, access_key, secret_key, region,
                   expires, when=None, service='s3', session_token=None):
    """Return the full list of query parameters for a presigned URL."""
    amz_date, datestamp = amz_dates(when)
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    q = list(params)
    q.extend([
        ('X-Amz-Algorithm', ALGORITHM),
        ('X-Amz-Credential', '%s/%s' % (access_key, scope)),
        ('X-Amz-Date', amz_date),
        ('X-Amz-Expires', str(int(expires))),
        ('X-Amz-SignedHeaders', 'host'),
    ])
    if session_token:
        q.append(('X-Amz-Security-Token', session_token))
    creq, _ = canonical_request(method, canonical_uri, q, {'host': host}, UNSIGNED_PAYLOAD)
    sig, _ = _signature(secret_key, datestamp, region, service, amz_date, scope, creq)
    q.append(('X-Amz-Signature', sig))
    return q
