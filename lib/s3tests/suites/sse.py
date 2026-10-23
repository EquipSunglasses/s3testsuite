"""Server-side encryption: SSE-C, SSE-S3 and default bucket encryption."""

import base64
import os

from ..assertions import NOT_CONFIGURED, eq, error, ok, skip, skip_if_disabled, skip_if_unimplemented
from ..client import md5_digest, md5_hex, xml_doc
from ..runner import tags

TAGS = ('sse',)


def _ssec_headers(key, prefix='x-amz-server-side-encryption-customer-'):
    return {
        prefix + 'algorithm': 'AES256',
        prefix + 'key': base64.b64encode(key).decode('ascii'),
        prefix + 'key-MD5': base64.b64encode(md5_digest(key)).decode('ascii'),
    }


def _ssec_put_ok(r, what='SSE-C put'):
    skip_if_unimplemented(r, 'SSE-C')
    if not r.ok:
        skip_if_disabled(r, 'SSE-C')
    return ok(r, what)


def _need_tls(ctx):
    if not ctx.cfg.secure and not ctx.cfg.allow_insecure_sse:
        skip('SSE-C requires https (or [target] allow_insecure_sse = true)', NOT_CONFIGURED)


@tags('sse_c')
def test_sse_c_roundtrip(ctx):
    """SSE-C PUT then GET with the same key returns the data."""
    _need_tls(ctx)
    b = ctx.new_bucket()
    key, data = os.urandom(32), ctx.data(4096)
    r = _ssec_put_ok(ctx.c.put_object(b, 'k', data, headers=_ssec_headers(key)))
    eq(r.header('x-amz-server-side-encryption-customer-algorithm'), 'AES256', 'algorithm echo')
    g = ok(ctx.c.get_object(b, 'k', headers=_ssec_headers(key)))
    eq(md5_hex(g.body), md5_hex(data), 'body')


@tags('sse_c')
def test_sse_c_get_without_key(ctx):
    """GET of an SSE-C object without the key returns 400."""
    _need_tls(ctx)
    b = ctx.new_bucket()
    _ssec_put_ok(ctx.c.put_object(b, 'k', b'secret', headers=_ssec_headers(os.urandom(32))))
    error(ctx.c.get_object(b, 'k'), 400, 'InvalidRequest')


@tags('sse_c')
def test_sse_c_get_wrong_key(ctx):
    """GET of an SSE-C object with a different key is refused (400 or 403)."""
    _need_tls(ctx)
    b = ctx.new_bucket()
    _ssec_put_ok(ctx.c.put_object(b, 'k', b'secret', headers=_ssec_headers(os.urandom(32))))
    error(ctx.c.get_object(b, 'k', headers=_ssec_headers(os.urandom(32))), (400, 403))


@tags('sse_c')
def test_sse_c_bad_key_md5(ctx):
    """An SSE-C key-MD5 that does not match the key returns 400."""
    _need_tls(ctx)
    hdrs = _ssec_headers(os.urandom(32))
    hdrs['x-amz-server-side-encryption-customer-key-MD5'] = base64.b64encode(md5_digest(b'x')).decode('ascii')
    r = ctx.c.put_object(ctx.new_bucket(), 'k', b'x', headers=hdrs)
    skip_if_unimplemented(r, 'SSE-C')
    error(r, 400)


@tags('sse_c', 'multipart')
def test_sse_c_multipart(ctx):
    """Multipart upload with SSE-C headers on initiate and every part."""
    _need_tls(ctx)
    b = ctx.new_bucket()
    key = os.urandom(32)
    hdrs = _ssec_headers(key)
    uid = _ssec_put_ok(ctx.c.create_multipart_upload(b, 'mp', headers=hdrs), 'SSE-C initiate').xml().findtext('UploadId')
    ps = ctx.cfg.part_size_mb * 1024 * 1024
    chunks = [ctx.data(ps), ctx.data(100)]
    parts = []
    for n, d in enumerate(chunks, 1):
        parts.append((n, ok(ctx.c.upload_part(b, 'mp', uid, n, d, headers=hdrs)).header('etag')))
    ok(ctx.c.complete_multipart_upload(b, 'mp', uid, parts))
    eq(md5_hex(ok(ctx.c.get_object(b, 'mp', headers=hdrs)).body), md5_hex(b''.join(chunks)), 'body')


@tags('sse_s3')
def test_sse_s3_put_get(ctx):
    """x-amz-server-side-encryption: AES256 is accepted and reported on GET."""
    b = ctx.new_bucket()
    r = ctx.c.put_object(b, 'k', b'data', headers={'x-amz-server-side-encryption': 'AES256'})
    skip_if_unimplemented(r, 'SSE-S3')
    eq(ok(r).header('x-amz-server-side-encryption'), 'AES256', 'PUT response header')
    g = ok(ctx.c.get_object(b, 'k'))
    eq(g.body, b'data', 'body')
    eq(g.header('x-amz-server-side-encryption'), 'AES256', 'GET response header')


@tags('sse_s3')
def test_bucket_default_encryption(ctx):
    """PUT/GET/DELETE ?encryption; new objects pick up the bucket default."""
    b = ctx.new_bucket()
    cfg = xml_doc('ServerSideEncryptionConfiguration',
                  '<Rule><ApplyServerSideEncryptionByDefault><SSEAlgorithm>AES256</SSEAlgorithm>'
                  '</ApplyServerSideEncryptionByDefault></Rule>')
    r = ctx.c.put_bucket_sub(b, 'encryption', cfg)
    skip_if_unimplemented(r, 'bucket encryption')
    ok(r, 'put encryption')
    root = ok(ctx.c.get_bucket_sub(b, 'encryption')).xml()
    eq(root.findtext('Rule/ApplyServerSideEncryptionByDefault/SSEAlgorithm'), 'AES256', 'SSEAlgorithm')
    ok(ctx.c.put_object(b, 'k', b'x'))
    eq(ok(ctx.c.head_object(b, 'k')).header('x-amz-server-side-encryption'), 'AES256', 'object encryption header')
    ok(ctx.c.delete_bucket_sub(b, 'encryption'), 'delete encryption')
    r = ctx.c.get_bucket_sub(b, 'encryption')
    if r.status != 200:  # AWS now always reports SSE-S3 as the default; others return 404
        error(r, 404, 'ServerSideEncryptionConfigurationNotFoundError')
