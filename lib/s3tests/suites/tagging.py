"""Object and bucket tagging."""

from .. import s3xml
from ..assertions import eq, error, ok
from ..client import x, xml_doc
from ..runner import tags

TAGS = ('tagging',)


def tagging_xml(tagmap):
    inner = ''.join('<Tag>%s%s</Tag>' % (x('Key', k), x('Value', v)) for k, v in sorted(tagmap.items()))
    return xml_doc('Tagging', '<TagSet>%s</TagSet>' % inner)


def test_object_tagging_roundtrip(ctx):
    """PUT/GET/DELETE ?tagging on an object."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    tagmap = {'project': 's3tests', 'env': 'lab', 'empty': ''}
    ok(ctx.c.put_object_sub(b, 'k', 'tagging', tagging_xml(tagmap)), 'put tagging')
    eq(s3xml.tagset(ok(ctx.c.get_object_sub(b, 'k', 'tagging')).xml()), tagmap, 'tags')
    ok(ctx.c.delete_object_sub(b, 'k', 'tagging'), 'delete tagging')
    eq(s3xml.tagset(ok(ctx.c.get_object_sub(b, 'k', 'tagging')).xml()), {}, 'tags after delete')


def test_object_tagging_header_on_put(ctx):
    """x-amz-tagging on PUT stores URL-encoded tags; GET reports x-amz-tagging-count."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x', headers={'x-amz-tagging': 'a=1&b=two%20words'}))
    eq(s3xml.tagset(ok(ctx.c.get_object_sub(b, 'k', 'tagging')).xml()), {'a': '1', 'b': 'two words'}, 'tags')
    eq(ok(ctx.c.get_object(b, 'k')).header('x-amz-tagging-count'), '2', 'x-amz-tagging-count')


@tags('limits')
def test_object_tagging_too_many(ctx):
    """More than 10 object tags is rejected with 400."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    r = ctx.c.put_object_sub(b, 'k', 'tagging', tagging_xml(dict(('k%d' % i, 'v') for i in range(11))))
    error(r, 400, ('BadRequest', 'InvalidTag'))


@tags('limits')
def test_object_tagging_key_too_long(ctx):
    """A tag key longer than 128 characters is rejected with 400."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    error(ctx.c.put_object_sub(b, 'k', 'tagging', tagging_xml({'k' * 129: 'v'})), 400, ('InvalidTag', 'BadRequest'))


def test_object_tagging_missing_key(ctx):
    """Tagging a missing object returns 404 NoSuchKey."""
    error(ctx.c.put_object_sub(ctx.new_bucket(), 'missing', 'tagging', tagging_xml({'a': 'b'})), 404, 'NoSuchKey')


def test_copy_keeps_tags(ctx):
    """CopyObject keeps the source tags by default."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'src', b'x', headers={'x-amz-tagging': 'a=1'}))
    ok(ctx.c.copy_object(b, 'dst', b, 'src'))
    eq(s3xml.tagset(ok(ctx.c.get_object_sub(b, 'dst', 'tagging')).xml()), {'a': '1'}, 'copied tags')


def test_bucket_tagging_roundtrip(ctx):
    """PUT/GET/DELETE ?tagging on a bucket; GET with no tags returns 404 NoSuchTagSet."""
    b = ctx.new_bucket()
    error(ctx.c.get_bucket_sub(b, 'tagging'), 404, 'NoSuchTagSet', 'GET before any tags')
    tagmap = {'cost-center': '1234', 'owner': 'storage'}
    ok(ctx.c.put_bucket_sub(b, 'tagging', tagging_xml(tagmap)), 'put tagging')
    eq(s3xml.tagset(ok(ctx.c.get_bucket_sub(b, 'tagging')).xml()), tagmap, 'tags')
    ok(ctx.c.delete_bucket_sub(b, 'tagging'), 'delete tagging')
    error(ctx.c.get_bucket_sub(b, 'tagging'), 404, 'NoSuchTagSet', 'GET after delete')
