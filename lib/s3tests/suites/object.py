"""Single-object operations: PUT, GET, HEAD, DELETE, metadata, keys, ranges, conditionals."""

import base64
import datetime
import email.utils
import hashlib
import struct
import zlib

from .. import s3xml, sigv4
from ..assertions import contains, eq, error, ok, skip_if_unimplemented, status, true
from ..client import md5_b64, md5_hex
from ..runner import tags

TAGS = ('object',)


def etag(resp):
    return (resp.header('etag') or '').strip('"')


def _obj(ctx, size=100):
    b = ctx.new_bucket()
    data = ctx.data(size)
    ok(ctx.c.put_object(b, 'k', data), 'setup put')
    return b, data


@tags('smoke')
def test_put_get_roundtrip(ctx):
    """PUT then GET returns identical bytes; ETag is the MD5 of the body."""
    b = ctx.new_bucket()
    data = ctx.data(4096)
    eq(etag(ok(ctx.c.put_object(b, 'k', data), 'put')), md5_hex(data), 'PUT ETag')
    r = ok(ctx.c.get_object(b, 'k'), 'get')
    eq(r.body, data, 'body')
    eq(r.header('content-length'), str(len(data)), 'Content-Length')
    eq(etag(r), md5_hex(data), 'GET ETag')


@tags('smoke')
def test_put_zero_byte_object(ctx):
    """A zero-byte object stores and returns an empty body with the empty MD5 ETag."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'empty', b''))
    r = ok(ctx.c.get_object(b, 'empty'))
    eq(r.body, b'', 'body')
    eq(etag(r), 'd41d8cd98f00b204e9800998ecf8427e', 'ETag')


@tags('smoke')
def test_head_object(ctx):
    """HEAD returns Content-Length, ETag and a parseable Last-Modified, with no body."""
    b, data = _obj(ctx, 1234)
    r = ok(ctx.c.head_object(b, 'k'))
    eq(r.header('content-length'), '1234', 'Content-Length')
    eq(etag(r), md5_hex(data), 'ETag')
    lm = email.utils.parsedate_tz(r.header('last-modified') or '')
    true(lm is not None, 'Last-Modified not an HTTP date: %r' % r.header('last-modified'))
    eq(r.body, b'', 'HEAD body')


@tags('smoke')
def test_get_missing_key(ctx):
    """GET on a missing key returns 404 NoSuchKey."""
    error(ctx.c.get_object(ctx.new_bucket(), 'nope'), 404, 'NoSuchKey')


def test_head_missing_key(ctx):
    """HEAD on a missing key returns 404."""
    error(ctx.c.head_object(ctx.new_bucket(), 'nope'), 404)


def test_get_from_missing_bucket(ctx):
    """GET on a key in a missing bucket returns 404 NoSuchBucket."""
    error(ctx.c.get_object(ctx.bucket_name(), 'k'), 404, 'NoSuchBucket')


def test_put_to_missing_bucket(ctx):
    """PUT into a missing bucket returns 404 NoSuchBucket."""
    error(ctx.c.put_object(ctx.bucket_name(), 'k', b'x'), 404, 'NoSuchBucket')


@tags('smoke')
def test_delete_object(ctx):
    """DELETE returns 204 and the object is gone."""
    b, _ = _obj(ctx)
    status(ctx.c.delete_object(b, 'k'), 204, 'delete')
    error(ctx.c.get_object(b, 'k'), 404, 'NoSuchKey')


def test_delete_missing_key_returns_204(ctx):
    """DELETE on a missing key is idempotent and returns 204."""
    status(ctx.c.delete_object(ctx.new_bucket(), 'never-existed'), 204, 'delete missing key')


@tags('smoke')
def test_overwrite_object(ctx):
    """A second PUT replaces the body and ETag."""
    b, _ = _obj(ctx)
    data = ctx.data(50)
    ok(ctx.c.put_object(b, 'k', data))
    r = ok(ctx.c.get_object(b, 'k'))
    eq(r.body, data, 'body after overwrite')
    eq(etag(r), md5_hex(data), 'ETag after overwrite')


@tags('smoke')
def test_user_metadata_roundtrip(ctx):
    """x-amz-meta-* headers round-trip; names come back lowercase."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x', headers={'x-amz-meta-Color': 'blue', 'x-amz-meta-with-space': 'a b c',
                                                'x-amz-meta-empty-ish': '-'}))
    r = ok(ctx.c.head_object(b, 'k'))
    eq(r.header('x-amz-meta-color'), 'blue', 'x-amz-meta-color')
    eq(r.header('x-amz-meta-with-space'), 'a b c', 'x-amz-meta-with-space')
    eq(r.header('x-amz-meta-empty-ish'), '-', 'x-amz-meta-empty-ish')


def test_metadata_replaced_on_overwrite(ctx):
    """Overwriting an object without metadata drops the old metadata."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x', headers={'x-amz-meta-old': '1'}))
    ok(ctx.c.put_object(b, 'k', b'y'))
    eq(ok(ctx.c.head_object(b, 'k')).header('x-amz-meta-old'), None, 'old metadata')


def test_standard_headers_roundtrip(ctx):
    """Content-Type, Cache-Control, Content-Disposition, Content-Language and Expires are stored."""
    b = ctx.new_bucket()
    hdrs = {
        'Content-Type': 'application/x-s3tests',
        'Cache-Control': 'max-age=3600',
        'Content-Disposition': 'attachment; filename="report.txt"',
        'Content-Language': 'en-US',
        'Expires': 'Thu, 01 Dec 2033 16:00:00 GMT',
    }
    ok(ctx.c.put_object(b, 'k', b'x', headers=hdrs))
    r = ok(ctx.c.get_object(b, 'k'))
    for k, v in hdrs.items():
        eq(r.header(k), v, k)


def test_default_content_type(ctx):
    """Objects stored without Content-Type get an octet-stream type."""
    b, _ = _obj(ctx)
    contains(('binary/octet-stream', 'application/octet-stream'),
             ok(ctx.c.head_object(b, 'k')).header('content-type'), 'default Content-Type')


@tags('integrity')
def test_content_md5_valid(ctx):
    """PUT with a correct Content-MD5 succeeds."""
    data = ctx.data(1000)
    ok(ctx.c.put_object(ctx.new_bucket(), 'k', data, headers={'Content-MD5': md5_b64(data)}))


@tags('integrity')
def test_content_md5_mismatch(ctx):
    """PUT with a well-formed but wrong Content-MD5 returns 400 BadDigest."""
    b = ctx.new_bucket()
    error(ctx.c.put_object(b, 'k', b'actual', headers={'Content-MD5': md5_b64(b'other')}), 400, 'BadDigest')
    error(ctx.c.head_object(b, 'k'), 404, what='object must not exist')


@tags('integrity')
def test_content_md5_malformed(ctx):
    """PUT with a malformed Content-MD5 returns 400 InvalidDigest."""
    error(ctx.c.put_object(ctx.new_bucket(), 'k', b'x', headers={'Content-MD5': 'not-base64!!'}),
          400, 'InvalidDigest')


TRICKY_KEYS = [
    'space here', 'plus+sign', 'percent%20literal', 'lone%percent', 'unicode-üñ-中文',
    'emoji-\U0001f600', 'nested/a/b/c', 'trailing-slash/', 'tilde~x', "single'quote", 'double"quote',
    'semi;colon', 'equals=sign', 'amp&ersand', 'hash#tag', 'question?mark', 'star*', 'comma,x', 'at@x',
    'dollar$x', 'paren(1)', 'bracket[1]', 'brace{1}', 'caret^x', 'backtick`x', 'pipe|x', 'back\\slash',
    'lt<gt>', 'dots..x', ' leading space', 'trailing space ', 'colon:x', 'exclaim!x',
]


@tags('keys')
def test_tricky_key_names(ctx):
    """Keys with reserved, encoded and non-ASCII characters round-trip and list exactly."""
    b = ctx.new_bucket()
    failures = []
    stored = []
    for k in TRICKY_KEYS:
        body = k.encode('utf-8')
        r = ctx.c.put_object(b, k, body)
        if not r.ok:
            failures.append('PUT %r -> HTTP %d %s' % (k, r.status, r.error_code))
            continue
        stored.append(k)
        g = ctx.c.get_object(b, k)
        if g.status != 200 or g.body != body:
            failures.append('GET %r -> HTTP %d body_match=%s' % (k, g.status, g.body == body))
    listed = []
    params = {'max-keys': '1000'}
    while True:
        root = ok(ctx.c.list_objects_v2(b, params), 'list').xml()
        listed += s3xml.keys(root)
        if not s3xml.truncated(root):
            break
        params['continuation-token'] = root.findtext('NextContinuationToken')
    for k in stored:
        if k not in listed:
            failures.append('key %r missing from listing' % k)
    eq(failures, [], 'tricky key failures')


@tags('keys', 'limits')
def test_key_length_1024(ctx):
    """A 1024-byte key is accepted."""
    b = ctx.new_bucket()
    k = 'k' * 1024
    ok(ctx.c.put_object(b, k, b'x'), 'put 1024-byte key')
    eq(ok(ctx.c.get_object(b, k)).body, b'x', 'body')


@tags('keys', 'limits')
def test_key_length_1025_rejected(ctx):
    """A 1025-byte key is rejected with 400 (KeyTooLongError on AWS)."""
    error(ctx.c.put_object(ctx.new_bucket(), 'k' * 1025, b'x'), 400)


@tags('large')
def test_large_object_integrity(ctx):
    """A large single-PUT object (large_object_mb) reads back with the same MD5."""
    size = ctx.cfg.large_object_mb * 1024 * 1024
    b = ctx.new_bucket()
    data = ctx.data(size)
    ok(ctx.c.put_object(b, 'big', data), 'put %d MiB' % ctx.cfg.large_object_mb)
    r = ok(ctx.c.get_object(b, 'big'))
    eq(len(r.body), size, 'length')
    eq(md5_hex(r.body), md5_hex(data), 'md5 of body')


@tags('range', 'smoke')
def test_range_first_bytes(ctx):
    """Range bytes=0-9 returns 206 and the first 10 bytes."""
    b, data = _obj(ctx)
    r = status(ctx.c.get_object(b, 'k', headers={'Range': 'bytes=0-9'}), 206)
    eq(r.body, data[:10], 'body')
    eq(r.header('content-range'), 'bytes 0-9/100', 'Content-Range')
    eq(r.header('content-length'), '10', 'Content-Length')


@tags('range')
def test_range_suffix(ctx):
    """Range bytes=-10 returns the last 10 bytes."""
    b, data = _obj(ctx)
    r = status(ctx.c.get_object(b, 'k', headers={'Range': 'bytes=-10'}), 206)
    eq(r.body, data[-10:], 'body')
    eq(r.header('content-range'), 'bytes 90-99/100', 'Content-Range')


@tags('range')
def test_range_open_ended(ctx):
    """Range bytes=90- returns from offset 90 to the end."""
    b, data = _obj(ctx)
    r = status(ctx.c.get_object(b, 'k', headers={'Range': 'bytes=90-'}), 206)
    eq(r.body, data[90:], 'body')


@tags('range')
def test_range_end_past_size(ctx):
    """A range ending past the object is truncated to the object size."""
    b, data = _obj(ctx)
    r = status(ctx.c.get_object(b, 'k', headers={'Range': 'bytes=95-500'}), 206)
    eq(r.body, data[95:], 'body')
    eq(r.header('content-range'), 'bytes 95-99/100', 'Content-Range')


@tags('range')
def test_range_unsatisfiable(ctx):
    """A range starting past the end returns 416 InvalidRange."""
    b, _ = _obj(ctx)
    error(ctx.c.get_object(b, 'k', headers={'Range': 'bytes=100-200'}), 416, 'InvalidRange')


@tags('conditional')
def test_get_if_match(ctx):
    """If-Match with the current ETag returns 200; a different ETag returns 412."""
    b, data = _obj(ctx)
    ok(ctx.c.get_object(b, 'k', headers={'If-Match': '"%s"' % md5_hex(data)}), 'matching ETag')
    error(ctx.c.get_object(b, 'k', headers={'If-Match': '"%s"' % md5_hex(b'z')}), 412, 'PreconditionFailed')


@tags('conditional')
def test_get_if_none_match(ctx):
    """If-None-Match with the current ETag returns 304; a different ETag returns 200."""
    b, data = _obj(ctx)
    status(ctx.c.get_object(b, 'k', headers={'If-None-Match': '"%s"' % md5_hex(data)}), 304, 'matching ETag')
    ok(ctx.c.get_object(b, 'k', headers={'If-None-Match': '"%s"' % md5_hex(b'z')}), 'other ETag')


@tags('conditional')
def test_get_if_modified_since(ctx):
    """If-Modified-Since: the object's Last-Modified gives 304, a day earlier gives 200."""
    b, _ = _obj(ctx)
    lm = ok(ctx.c.head_object(b, 'k')).header('last-modified')
    status(ctx.c.get_object(b, 'k', headers={'If-Modified-Since': lm}), 304, 'not modified since Last-Modified')
    earlier = email.utils.formatdate(email.utils.mktime_tz(email.utils.parsedate_tz(lm)) - 86400, usegmt=True)
    ok(ctx.c.get_object(b, 'k', headers={'If-Modified-Since': earlier}), 'modified since a day earlier')


@tags('conditional')
def test_get_if_unmodified_since(ctx):
    """If-Unmodified-Since: a day before Last-Modified gives 412, Last-Modified itself gives 200."""
    b, _ = _obj(ctx)
    lm = ok(ctx.c.head_object(b, 'k')).header('last-modified')
    earlier = email.utils.formatdate(email.utils.mktime_tz(email.utils.parsedate_tz(lm)) - 86400, usegmt=True)
    error(ctx.c.get_object(b, 'k', headers={'If-Unmodified-Since': earlier}), 412, 'PreconditionFailed')
    ok(ctx.c.get_object(b, 'k', headers={'If-Unmodified-Since': lm}), 'unmodified since Last-Modified')


def test_response_header_overrides(ctx):
    """response-* query parameters override the returned headers on signed GET."""
    b, _ = _obj(ctx)
    q = {
        'response-content-type': 'text/x-override',
        'response-content-language': 'fr',
        'response-cache-control': 'no-cache',
        'response-content-disposition': 'attachment; filename=override.bin',
        'response-expires': 'Fri, 01 Jan 2038 00:00:00 GMT',
    }
    r = ok(ctx.c.get_object(b, 'k', query=q))
    for k, v in q.items():
        eq(r.header(k[len('response-'):]), v, k)


@tags('conditional', 'conditional_write')
def test_put_if_none_match_star(ctx):
    """PUT with If-None-Match: * creates once, then returns 412 when the key exists."""
    b = ctx.new_bucket()
    r = ctx.c.put_object(b, 'k', b'first', headers={'If-None-Match': '*'})
    skip_if_unimplemented(r, 'conditional PUT')
    ok(r, 'first conditional put')
    error(ctx.c.put_object(b, 'k', b'second', headers={'If-None-Match': '*'}), 412, 'PreconditionFailed')
    eq(ok(ctx.c.get_object(b, 'k')).body, b'first', 'body unchanged')


@tags('conditional', 'conditional_write')
def test_put_if_match(ctx):
    """PUT with If-Match succeeds for the current ETag and returns 412 for a stale one."""
    b, data = _obj(ctx)
    r = ctx.c.put_object(b, 'k', b'v2', headers={'If-Match': '"%s"' % md5_hex(b'stale')})
    skip_if_unimplemented(r, 'conditional PUT')
    error(r, 412, 'PreconditionFailed', 'stale If-Match')
    ok(ctx.c.put_object(b, 'k', b'v2', headers={'If-Match': '"%s"' % md5_hex(data)}), 'current If-Match')


@tags('auth')
def test_unsigned_payload(ctx):
    """x-amz-content-sha256: UNSIGNED-PAYLOAD is accepted."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'unsigned', payload_hash=sigv4.UNSIGNED_PAYLOAD))
    eq(ok(ctx.c.get_object(b, 'k')).body, b'unsigned', 'body')


@tags('auth', 'integrity')
def test_payload_hash_mismatch(ctx):
    """A signed x-amz-content-sha256 that does not match the body is rejected with 400."""
    b = ctx.new_bucket()
    r = ctx.c.put_object(b, 'k', b'real body', payload_hash=sigv4.sha256_hex(b'other body'))
    error(r, 400, ('XAmzContentSHA256Mismatch', 'BadDigest'))
    error(ctx.c.head_object(b, 'k'), 404, what='object must not exist')


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


@tags('checksum', 'integrity')
def test_checksum_crc32(ctx):
    """x-amz-checksum-crc32 is validated, echoed, and returned with checksum mode."""
    b = ctx.new_bucket()
    data = ctx.data(2048)
    crc = _b64(struct.pack('>I', zlib.crc32(data) & 0xffffffff))
    r = ctx.c.put_object(b, 'k', data, headers={'x-amz-checksum-crc32': crc})
    skip_if_unimplemented(r, 'flexible checksums')
    ok(r, 'put with crc32')
    eq(r.header('x-amz-checksum-crc32'), crc, 'PUT response checksum')
    g = ok(ctx.c.get_object(b, 'k', headers={'x-amz-checksum-mode': 'ENABLED'}))
    eq(g.header('x-amz-checksum-crc32'), crc, 'GET checksum header')


@tags('checksum', 'integrity')
def test_checksum_sha256(ctx):
    """x-amz-checksum-sha256 is validated and echoed."""
    b = ctx.new_bucket()
    data = ctx.data(2048)
    digest = _b64(hashlib.sha256(data).digest())
    r = ctx.c.put_object(b, 'k', data, headers={'x-amz-checksum-sha256': digest})
    skip_if_unimplemented(r, 'flexible checksums')
    eq(ok(r).header('x-amz-checksum-sha256'), digest, 'PUT response checksum')


@tags('checksum', 'integrity')
def test_checksum_mismatch(ctx):
    """A wrong x-amz-checksum-crc32 is rejected with 400 and nothing is stored."""
    b = ctx.new_bucket()
    wrong = _b64(struct.pack('>I', zlib.crc32(b'other') & 0xffffffff))
    r = ctx.c.put_object(b, 'k', b'actual', headers={'x-amz-checksum-crc32': wrong})
    skip_if_unimplemented(r, 'flexible checksums')
    error(r, 400)
    error(ctx.c.head_object(b, 'k'), 404, what='object must not exist')


def test_last_modified_is_recent(ctx):
    """Last-Modified is within 15 minutes of the client clock (detects clock skew on the cluster)."""
    b, _ = _obj(ctx)
    lm = email.utils.mktime_tz(email.utils.parsedate_tz(ok(ctx.c.head_object(b, 'k')).header('last-modified')))
    now = (datetime.datetime.utcnow() - datetime.datetime(1970, 1, 1)).total_seconds()
    true(abs(now - lm) < 900, 'Last-Modified differs from client clock by %ds' % int(now - lm))
