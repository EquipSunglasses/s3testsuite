"""DeleteObjects (POST ?delete)."""

from .. import s3xml
from ..assertions import eq, error, ok
from ..client import md5_b64
from ..runner import tags

TAGS = ('multidelete', 'object')


@tags('smoke')
def test_delete_multiple_objects(ctx):
    """Deleting several keys (one missing) reports each as Deleted and removes them."""
    b = ctx.new_bucket()
    for k in ('a', 'b', 'c'):
        ok(ctx.c.put_object(b, k, b'x'))
    root = ok(ctx.c.delete_objects(b, ['a', 'b', 'c', 'missing'])).xml()
    eq(sorted(d.findtext('Key') for d in root.findall('Deleted')), ['a', 'b', 'c', 'missing'], 'Deleted keys')
    eq(root.findall('Error'), [], 'Error entries')
    eq(s3xml.keys(ok(ctx.c.list_objects_v2(b)).xml()), [], 'remaining keys')


def test_delete_multiple_quiet(ctx):
    """Quiet mode returns no Deleted entries on success."""
    b = ctx.new_bucket()
    for k in ('a', 'b'):
        ok(ctx.c.put_object(b, k, b'x'))
    root = ok(ctx.c.delete_objects(b, ['a', 'b'], quiet=True)).xml()
    eq(root.findall('Deleted'), [], 'Deleted entries in quiet mode')
    eq(s3xml.keys(ok(ctx.c.list_objects_v2(b)).xml()), [], 'remaining keys')


def test_delete_multiple_special_keys(ctx):
    """XML-special and non-ASCII keys are handled in DeleteObjects."""
    b = ctx.new_bucket()
    keys = ['a&b', 'c<d>', 'e"f', 'über', 'sp ace']
    for k in keys:
        ok(ctx.c.put_object(b, k, b'x'))
    root = ok(ctx.c.delete_objects(b, keys)).xml()
    eq(sorted(d.findtext('Key') for d in root.findall('Deleted')), sorted(keys), 'Deleted keys')


@tags('limits')
def test_delete_multiple_over_1000_keys(ctx):
    """More than 1000 keys in one request is rejected with 400 MalformedXML."""
    b = ctx.new_bucket()
    error(ctx.c.delete_objects(b, ['k%d' % i for i in range(1001)]), 400, ('MalformedXML', 'InvalidRequest'))


def test_delete_multiple_malformed_xml(ctx):
    """A malformed body is rejected with 400 MalformedXML."""
    b = ctx.new_bucket()
    body = b'<Delete><Object><Key>a</Key></Object>'
    r = ctx.c.request('POST', b, query=[('delete', None)], body=body, headers={'Content-MD5': md5_b64(body)})
    error(r, 400, 'MalformedXML')


@tags('versioning')
def test_delete_multiple_versioned_creates_markers(ctx):
    """In a versioned bucket, deleting by key creates delete markers."""
    b = ctx.versioned_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    root = ok(ctx.c.delete_objects(b, ['k'])).xml()
    d = root.find('Deleted')
    eq(d is not None, True, 'Deleted entry present')
    eq(s3xml.is_true(d.findtext('DeleteMarker')), True, 'DeleteMarker flag')
    eq(bool(d.findtext('DeleteMarkerVersionId')), True, 'DeleteMarkerVersionId present')
