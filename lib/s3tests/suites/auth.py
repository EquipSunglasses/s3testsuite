"""Authentication: SigV4 failures, clock skew, presigned URLs, anonymous access."""

import datetime

from ..assertions import eq, error, ok, status
from ..runner import tags

TAGS = ('auth',)


@tags('smoke')
def test_wrong_secret_key(ctx):
    """A request signed with the wrong secret returns 403 SignatureDoesNotMatch."""
    c = ctx.custom_client(ctx.cfg.main.access_key, ctx.cfg.main.secret_key + 'x')
    error(c.list_buckets(), 403, 'SignatureDoesNotMatch')


def test_unknown_access_key(ctx):
    """An unknown access key returns 403 InvalidAccessKeyId."""
    c = ctx.custom_client('AKIAS3TESTSNOTREAL0', 'not-a-real-secret-key-000000000000000000')
    error(c.list_buckets(), 403, 'InvalidAccessKeyId')


def test_request_time_too_skewed(ctx):
    """A request dated 20 minutes in the past returns 403 RequestTimeTooSkewed."""
    when = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
    error(ctx.c.list_buckets(when=when), 403, 'RequestTimeTooSkewed')


def test_request_time_in_future_skewed(ctx):
    """A request dated 20 minutes in the future returns 403 RequestTimeTooSkewed."""
    when = datetime.datetime.utcnow() + datetime.timedelta(minutes=20)
    error(ctx.c.list_buckets(when=when), 403, 'RequestTimeTooSkewed')


def test_tampered_signed_header(ctx):
    """Changing a signed header after signing returns 403 SignatureDoesNotMatch."""
    b = ctx.new_bucket()
    # sign with one Content-Type, then send another by signing a different value via a raw header
    r = ctx.c.request('PUT', b, 'k', body=b'x', headers={'Content-Type': 'text/plain'})
    ok(r, 'control request')
    c = ctx.c
    orig = c._send

    def tamper(method, scheme, host, port, url, headers, body):
        headers = dict(headers)
        headers['Content-Type'] = 'text/html'
        return orig(method, scheme, host, port, url, headers, body)
    c._send = tamper
    try:
        r = c.request('PUT', b, 'k2', body=b'x', headers={'Content-Type': 'text/plain'})
    finally:
        c._send = orig
    error(r, 403, 'SignatureDoesNotMatch')


def test_malformed_authorization_header(ctx):
    """A garbage Authorization header is rejected with 400 or 403."""
    r = ctx.anon.request('GET', headers={'Authorization': 'AWS4-HMAC-SHA256 garbage'})
    status(r, (400, 403), 'malformed Authorization')


@tags('anonymous')
def test_anonymous_list_buckets(ctx):
    """Anonymous ListBuckets never lists buckets: 403, an empty list, or a redirect."""
    r = ctx.anon.list_buckets()
    if 300 <= r.status < 400:  # AWS redirects anonymous GET / to the S3 product page
        return
    if r.status == 200:
        eq(r.xml().findall('Buckets/Bucket'), [], 'anonymous bucket list')
    else:
        error(r, 403, 'AccessDenied')


@tags('presign', 'smoke')
def test_presigned_get(ctx):
    """A presigned GET URL works without credentials."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'presigned'))
    url = ctx.c.presign('GET', b, 'k', expires=300)
    eq(ok(ctx.anon.fetch('GET', url), 'GET presigned URL').body, b'presigned', 'body')


@tags('presign')
def test_presigned_put(ctx):
    """A presigned PUT URL uploads an object."""
    b = ctx.new_bucket()
    url = ctx.c.presign('PUT', b, 'uploaded', expires=300)
    ok(ctx.anon.fetch('PUT', url, body=b'via presign'), 'PUT presigned URL')
    eq(ok(ctx.c.get_object(b, 'uploaded')).body, b'via presign', 'body')


@tags('presign')
def test_presigned_special_key(ctx):
    """Presigned URLs work for keys that need encoding."""
    b = ctx.new_bucket()
    key = 'dir/space key+ü.txt'
    ok(ctx.c.put_object(b, key, b'x'))
    ok(ctx.anon.fetch('GET', ctx.c.presign('GET', b, key)), 'GET presigned URL')


@tags('presign')
def test_presigned_expired(ctx):
    """An expired presigned URL returns 403 AccessDenied."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    when = datetime.datetime.utcnow() - datetime.timedelta(seconds=120)
    url = ctx.c.presign('GET', b, 'k', expires=60, when=when)
    error(ctx.anon.fetch('GET', url), 403, 'AccessDenied')


@tags('presign')
def test_presigned_tampered_signature(ctx):
    """A presigned URL with a modified signature returns 403 SignatureDoesNotMatch."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    url = ctx.c.presign('GET', b, 'k')
    last = url[-1]
    url = url[:-1] + ('0' if last != '0' else '1')
    error(ctx.anon.fetch('GET', url), 403, 'SignatureDoesNotMatch')


@tags('presign')
def test_presigned_wrong_method(ctx):
    """A URL presigned for GET cannot be used for PUT."""
    b = ctx.new_bucket()
    url = ctx.c.presign('GET', b, 'k')
    error(ctx.anon.fetch('PUT', url, body=b'x'), 403, 'SignatureDoesNotMatch')
