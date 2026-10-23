"""Multipart upload: initiate, parts, complete, abort, list, copy parts, error cases."""

from .. import s3xml
from ..assertions import contains, eq, error, ok, status, true
from ..client import md5_digest, md5_hex
from ..runner import tags

TAGS = ('multipart',)
MiB = 1024 * 1024


def _ps(ctx):
    return ctx.cfg.part_size_mb * MiB


def _initiate(ctx, b, k, headers=None):
    r = ok(ctx.c.create_multipart_upload(b, k, headers=headers), 'initiate')
    uid = r.xml().findtext('UploadId')
    true(bool(uid), 'no UploadId in InitiateMultipartUploadResult')
    return uid


def _upload(ctx, b, k, sizes, headers=None):
    uid = _initiate(ctx, b, k, headers)
    parts, chunks = [], []
    for n, size in enumerate(sizes, 1):
        d = ctx.data(size)
        r = ok(ctx.c.upload_part(b, k, uid, n, d), 'upload part %d' % n)
        eq((r.header('etag') or '').strip('"'), md5_hex(d), 'part %d ETag' % n)
        parts.append((n, r.header('etag')))
        chunks.append(d)
    return uid, parts, chunks


def _complete(ctx, b, k, uid, parts):
    r = ok(ctx.c.complete_multipart_upload(b, k, uid, parts), 'complete')
    root = r.xml()
    if root.tag == 'Error':  # S3 may return 200 with an error document
        raise AssertionError('complete returned 200 with error %s: %s' % (
            root.findtext('Code'), root.findtext('Message')))
    eq(root.tag, 'CompleteMultipartUploadResult', 'complete root element')
    return root


def _mp_etag(chunks):
    return '%s-%d' % (md5_hex(b''.join(md5_digest(c) for c in chunks)), len(chunks))


@tags('smoke')
def test_multipart_basic(ctx):
    """Three-part upload completes; body, length and ETag (md5-of-md5s-N) are correct."""
    b = ctx.new_bucket()
    ps = _ps(ctx)
    uid, parts, chunks = _upload(ctx, b, 'mp', [ps, ps, 1024])
    root = _complete(ctx, b, 'mp', uid, parts)
    data = b''.join(chunks)
    eq((root.findtext('ETag') or '').strip('"'), _mp_etag(chunks), 'complete ETag')
    eq(root.findtext('Key'), 'mp', 'complete Key')
    r = ok(ctx.c.get_object(b, 'mp'))
    eq(r.header('content-length'), str(len(data)), 'Content-Length')
    eq(md5_hex(r.body), md5_hex(data), 'md5 of body')
    eq((r.header('etag') or '').strip('"'), _mp_etag(chunks), 'GET ETag')


def test_multipart_single_small_part(ctx):
    """A one-part upload may be smaller than the minimum part size."""
    b = ctx.new_bucket()
    uid, parts, chunks = _upload(ctx, b, 'mp', [10])
    _complete(ctx, b, 'mp', uid, parts)
    eq(ok(ctx.c.get_object(b, 'mp')).body, chunks[0], 'body')


def test_multipart_metadata_preserved(ctx):
    """Content-Type and user metadata given at initiate are on the completed object."""
    b = ctx.new_bucket()
    uid, parts, _ = _upload(ctx, b, 'mp', [100], headers={'Content-Type': 'text/x-mp', 'x-amz-meta-src': 'mpu'})
    _complete(ctx, b, 'mp', uid, parts)
    r = ok(ctx.c.head_object(b, 'mp'))
    eq(r.header('content-type'), 'text/x-mp', 'Content-Type')
    eq(r.header('x-amz-meta-src'), 'mpu', 'x-amz-meta-src')


def test_multipart_list_parts(ctx):
    """ListParts returns part numbers, ETags and sizes."""
    b = ctx.new_bucket()
    ps = _ps(ctx)
    uid, parts, _ = _upload(ctx, b, 'mp', [ps, 2048])
    root = ok(ctx.c.list_parts(b, 'mp', uid)).xml()
    got = [(p.findtext('PartNumber'), p.findtext('ETag'), p.findtext('Size')) for p in root.findall('Part')]
    eq(got, [('1', parts[0][1], str(ps)), ('2', parts[1][1], '2048')], 'parts')
    eq(root.findtext('UploadId'), uid, 'UploadId')


def test_multipart_list_parts_paging(ctx):
    """ListParts pages with max-parts and part-number-marker."""
    b = ctx.new_bucket()
    uid, _, _ = _upload(ctx, b, 'mp', [10, 10, 10])
    root = ok(ctx.c.list_parts(b, 'mp', uid, {'max-parts': '2'})).xml()
    eq([p.findtext('PartNumber') for p in root.findall('Part')], ['1', '2'], 'first page')
    eq(s3xml.truncated(root), True, 'IsTruncated')
    marker = root.findtext('NextPartNumberMarker')
    root = ok(ctx.c.list_parts(b, 'mp', uid, {'part-number-marker': marker})).xml()
    eq([p.findtext('PartNumber') for p in root.findall('Part')], ['3'], 'second page')


def test_multipart_list_uploads(ctx):
    """ListMultipartUploads shows in-progress uploads and filters by prefix."""
    b = ctx.new_bucket()
    u1 = _initiate(ctx, b, 'logs/a')
    u2 = _initiate(ctx, b, 'data/b')
    root = ok(ctx.c.list_multipart_uploads(b)).xml()
    got = [(u.findtext('Key'), u.findtext('UploadId')) for u in root.findall('Upload')]
    contains(got, ('logs/a', u1), 'uploads')
    contains(got, ('data/b', u2), 'uploads')
    root = ok(ctx.c.list_multipart_uploads(b, {'prefix': 'logs/'})).xml()
    eq([u.findtext('Key') for u in root.findall('Upload')], ['logs/a'], 'prefix filter')


@tags('smoke')
def test_multipart_abort(ctx):
    """After abort the upload is gone: ListParts and UploadPart return 404 NoSuchUpload."""
    b = ctx.new_bucket()
    uid, _, _ = _upload(ctx, b, 'mp', [100])
    status(ctx.c.abort_multipart_upload(b, 'mp', uid), 204, 'abort')
    error(ctx.c.list_parts(b, 'mp', uid), 404, 'NoSuchUpload')
    error(ctx.c.upload_part(b, 'mp', uid, 2, b'x'), 404, 'NoSuchUpload')
    error(ctx.c.head_object(b, 'mp'), 404, what='object must not exist')


def test_multipart_bogus_upload_id(ctx):
    """UploadPart with an unknown upload ID returns 404 NoSuchUpload."""
    b = ctx.new_bucket()
    error(ctx.c.upload_part(b, 'mp', 'bogus-upload-id-0000', 1, b'x'), 404, 'NoSuchUpload')


def test_multipart_part_too_small(ctx):
    """A non-final part below the minimum size fails completion with 400 EntityTooSmall."""
    b = ctx.new_bucket()
    uid, parts, _ = _upload(ctx, b, 'mp', [1024, 1024])
    error(ctx.c.complete_multipart_upload(b, 'mp', uid, parts), 400, 'EntityTooSmall')


def test_multipart_invalid_part_order(ctx):
    """Completing with parts out of order returns 400 InvalidPartOrder."""
    b = ctx.new_bucket()
    ps = _ps(ctx)
    uid, parts, _ = _upload(ctx, b, 'mp', [ps, ps])
    error(ctx.c.complete_multipart_upload(b, 'mp', uid, list(reversed(parts))), 400, 'InvalidPartOrder')


def test_multipart_wrong_etag(ctx):
    """Completing with a wrong part ETag returns 400 InvalidPart."""
    b = ctx.new_bucket()
    uid, parts, _ = _upload(ctx, b, 'mp', [100])
    error(ctx.c.complete_multipart_upload(b, 'mp', uid, [(1, '"%s"' % md5_hex(b'nope'))]), 400, 'InvalidPart')


def test_multipart_missing_part(ctx):
    """Completing with a part number that was never uploaded returns 400 InvalidPart."""
    b = ctx.new_bucket()
    uid, parts, _ = _upload(ctx, b, 'mp', [_ps(ctx)])
    error(ctx.c.complete_multipart_upload(b, 'mp', uid, parts + [(2, parts[0][1])]), 400, 'InvalidPart')


def test_multipart_complete_no_parts(ctx):
    """Completing with an empty part list returns 400."""
    b = ctx.new_bucket()
    uid = _initiate(ctx, b, 'mp')
    error(ctx.c.complete_multipart_upload(b, 'mp', uid, []), 400, ('MalformedXML', 'InvalidRequest'))


def test_multipart_overwrite_part(ctx):
    """Re-uploading a part number replaces it; completion uses the latest data."""
    b = ctx.new_bucket()
    uid = _initiate(ctx, b, 'mp')
    ok(ctx.c.upload_part(b, 'mp', uid, 1, ctx.data(100)))
    d2 = ctx.data(100)
    r = ok(ctx.c.upload_part(b, 'mp', uid, 1, d2))
    _complete(ctx, b, 'mp', uid, [(1, r.header('etag'))])
    eq(ok(ctx.c.get_object(b, 'mp')).body, d2, 'body')


def test_multipart_upload_part_copy(ctx):
    """UploadPartCopy with byte ranges assembles a copy of the source object."""
    b = ctx.new_bucket()
    ps = _ps(ctx)
    src = ctx.data(ps + 4096)
    ok(ctx.c.put_object(b, 'src', src))
    uid = _initiate(ctx, b, 'dst')
    parts = []
    for n, rng in ((1, 'bytes=0-%d' % (ps - 1)), (2, 'bytes=%d-%d' % (ps, len(src) - 1))):
        root = ok(ctx.c.upload_part_copy(b, 'dst', uid, n, b, 'src', byte_range=rng), 'copy part %d' % n).xml()
        eq(root.tag, 'CopyPartResult', 'root element')
        parts.append((n, root.findtext('ETag')))
    _complete(ctx, b, 'dst', uid, parts)
    eq(md5_hex(ok(ctx.c.get_object(b, 'dst')).body), md5_hex(src), 'md5 of copy')


@tags('multipart_partnumber')
def test_multipart_get_part_number(ctx):
    """GET ?partNumber=1 returns exactly the first part and x-amz-mp-parts-count."""
    b = ctx.new_bucket()
    ps = _ps(ctx)
    uid, parts, chunks = _upload(ctx, b, 'mp', [ps, 100])
    _complete(ctx, b, 'mp', uid, parts)
    r = status(ctx.c.get_object(b, 'mp', query={'partNumber': '1'}), (200, 206))
    eq(md5_hex(r.body), md5_hex(chunks[0]), 'part 1 body')
    eq(r.header('x-amz-mp-parts-count'), '2', 'x-amz-mp-parts-count')


def test_multipart_object_replaced_by_put(ctx):
    """A regular PUT over a multipart object replaces it entirely."""
    b = ctx.new_bucket()
    uid, parts, _ = _upload(ctx, b, 'mp', [100])
    _complete(ctx, b, 'mp', uid, parts)
    ok(ctx.c.put_object(b, 'mp', b'small'))
    r = ok(ctx.c.get_object(b, 'mp'))
    eq(r.body, b'small', 'body')
    eq((r.header('etag') or '').strip('"'), md5_hex(b'small'), 'ETag')
