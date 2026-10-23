"""CopyObject: same and cross bucket, metadata directives, conditions, errors."""

from ..assertions import eq, error, ok
from ..client import md5_hex
from ..runner import tags

TAGS = ('copy', 'object')


def _src(ctx, key='src', data=None, headers=None):
    b = ctx.new_bucket()
    data = data if data is not None else ctx.data(1000)
    ok(ctx.c.put_object(b, key, data, headers=headers), 'setup put')
    return b, data


@tags('smoke')
def test_copy_same_bucket(ctx):
    """Copy within a bucket; result XML carries ETag and LastModified; data matches."""
    b, data = _src(ctx)
    r = ok(ctx.c.copy_object(b, 'dst', b, 'src'))
    root = r.xml()
    eq(root.tag, 'CopyObjectResult', 'root element')
    eq((root.findtext('ETag') or '').strip('"'), md5_hex(data), 'CopyObjectResult ETag')
    eq(bool(root.findtext('LastModified')), True, 'LastModified present')
    eq(ok(ctx.c.get_object(b, 'dst')).body, data, 'copied body')


def test_copy_cross_bucket(ctx):
    """Copy to another bucket owned by the same user."""
    b, data = _src(ctx)
    b2 = ctx.new_bucket()
    ok(ctx.c.copy_object(b2, 'dst', b, 'src'))
    eq(ok(ctx.c.get_object(b2, 'dst')).body, data, 'copied body')


def test_copy_preserves_metadata(ctx):
    """Default metadata directive (COPY) keeps user metadata and Content-Type."""
    b, _ = _src(ctx, headers={'x-amz-meta-k': 'v', 'Content-Type': 'text/x-src'})
    ok(ctx.c.copy_object(b, 'dst', b, 'src'))
    r = ok(ctx.c.head_object(b, 'dst'))
    eq(r.header('x-amz-meta-k'), 'v', 'x-amz-meta-k')
    eq(r.header('content-type'), 'text/x-src', 'Content-Type')


def test_copy_replace_metadata(ctx):
    """x-amz-metadata-directive: REPLACE swaps metadata for the new headers."""
    b, _ = _src(ctx, headers={'x-amz-meta-old': '1'})
    ok(ctx.c.copy_object(b, 'dst', b, 'src', headers={
        'x-amz-metadata-directive': 'REPLACE', 'x-amz-meta-new': '2', 'Content-Type': 'text/x-new'}))
    r = ok(ctx.c.head_object(b, 'dst'))
    eq(r.header('x-amz-meta-old'), None, 'old metadata')
    eq(r.header('x-amz-meta-new'), '2', 'new metadata')
    eq(r.header('content-type'), 'text/x-new', 'Content-Type')


def test_copy_to_self_requires_change(ctx):
    """Copying an object onto itself without changes returns 400 InvalidRequest."""
    b, _ = _src(ctx)
    error(ctx.c.copy_object(b, 'src', b, 'src'), 400, 'InvalidRequest')


def test_copy_to_self_with_replace(ctx):
    """Copying onto itself with REPLACE updates metadata in place."""
    b, data = _src(ctx)
    ok(ctx.c.copy_object(b, 'src', b, 'src', headers={'x-amz-metadata-directive': 'REPLACE',
                                                     'x-amz-meta-touched': 'yes'}))
    r = ok(ctx.c.get_object(b, 'src'))
    eq(r.header('x-amz-meta-touched'), 'yes', 'metadata')
    eq(r.body, data, 'body unchanged')


def test_copy_missing_source(ctx):
    """Copying a missing source key returns 404 NoSuchKey."""
    b = ctx.new_bucket()
    error(ctx.c.copy_object(b, 'dst', b, 'missing'), 404, 'NoSuchKey')


def test_copy_missing_source_bucket(ctx):
    """Copying from a missing bucket returns 404 NoSuchBucket."""
    b = ctx.new_bucket()
    error(ctx.c.copy_object(b, 'dst', ctx.bucket_name(), 'k'), 404, 'NoSuchBucket')


@tags('conditional')
def test_copy_source_if_match(ctx):
    """x-amz-copy-source-if-match: current ETag copies, other ETag returns 412."""
    b, data = _src(ctx)
    ok(ctx.c.copy_object(b, 'a', b, 'src', headers={'x-amz-copy-source-if-match': '"%s"' % md5_hex(data)}))
    error(ctx.c.copy_object(b, 'b', b, 'src', headers={'x-amz-copy-source-if-match': '"%s"' % md5_hex(b'z')}),
          412, 'PreconditionFailed')


@tags('conditional')
def test_copy_source_if_none_match(ctx):
    """x-amz-copy-source-if-none-match with the current ETag returns 412."""
    b, data = _src(ctx)
    error(ctx.c.copy_object(b, 'a', b, 'src', headers={'x-amz-copy-source-if-none-match': '"%s"' % md5_hex(data)}),
          412, 'PreconditionFailed')


@tags('keys')
def test_copy_source_special_characters(ctx):
    """Copy works when the source key needs URL encoding."""
    key = 'dir with space/ünicode+plus?.bin'
    b, data = _src(ctx, key=key)
    ok(ctx.c.copy_object(b, 'dst', b, key))
    eq(ok(ctx.c.get_object(b, 'dst')).body, data, 'copied body')


@tags('multiuser', 'permissions')
def test_copy_from_unreadable_source_denied(ctx):
    """Another user cannot copy from a private object into their own bucket."""
    alt = ctx.alt
    b, _ = _src(ctx)
    b2 = ctx.new_bucket(client=alt)
    error(alt.copy_object(b2, 'stolen', b, 'src'), 403, 'AccessDenied')
