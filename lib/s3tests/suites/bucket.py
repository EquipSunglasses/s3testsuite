"""Bucket operations: create, head, list, delete, naming rules, ownership."""

from .. import s3xml
from ..assertions import contains, eq, error, not_contains, ok, status
from ..runner import tags

TAGS = ('bucket',)


@tags('smoke')
def test_create_head_delete(ctx):
    """Create a bucket, HEAD it, delete it, then HEAD returns 404."""
    name = ctx.bucket_name()
    ok(ctx.c.create_bucket(name, location=ctx.cfg.location_constraint), 'create bucket')
    ctx.register_bucket(name)
    ok(ctx.c.head_bucket(name), 'head bucket')
    status(ctx.c.delete_bucket(name), 204, 'delete bucket')
    error(ctx.c.head_bucket(name), 404, what='head deleted bucket')


@tags('smoke')
def test_head_missing_bucket(ctx):
    """HEAD on a bucket that does not exist returns 404."""
    error(ctx.c.head_bucket(ctx.bucket_name()), 404)


@tags('publicaccess')
def test_delete_public_access_block_keeps_bucket(ctx):
    """DELETE ?publicAccessBlock removes only that setting; the bucket itself remains."""
    b = ctx.new_bucket()
    r = ctx.c.delete_bucket_sub(b, 'publicAccessBlock')
    h = ctx.c.head_bucket(b)
    if h.status == 404:
        raise AssertionError('DELETE ?publicAccessBlock (HTTP %d) deleted the bucket itself: the server ignored '
                             'the subresource and ran DeleteBucket' % r.status)
    ok(h, 'HEAD bucket after DELETE ?publicAccessBlock')


def test_delete_missing_bucket(ctx):
    """DELETE on a missing bucket returns 404 NoSuchBucket."""
    error(ctx.c.delete_bucket(ctx.bucket_name()), 404, 'NoSuchBucket')


def test_delete_nonempty_bucket(ctx):
    """DELETE on a bucket with objects returns 409 BucketNotEmpty."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    error(ctx.c.delete_bucket(b), 409, 'BucketNotEmpty')


def test_delete_bucket_with_incomplete_upload(ctx):
    """A bucket holding only an in-progress multipart upload can still be deleted or reports 409."""
    b = ctx.new_bucket()
    ok(ctx.c.create_multipart_upload(b, 'pending'))
    r = ctx.c.delete_bucket(b)
    status(r, (204, 409), 'delete bucket with pending upload')


def test_create_existing_bucket_same_owner(ctx):
    """Re-creating your own bucket returns 409 BucketAlreadyOwnedByYou (200 accepted for us-east-1 legacy)."""
    b = ctx.new_bucket()
    r = ctx.c.create_bucket(b, location=ctx.cfg.location_constraint)
    if r.status != 200:
        error(r, 409, ('BucketAlreadyOwnedByYou', 'BucketAlreadyExists'))


@tags('multiuser')
def test_create_existing_bucket_other_owner(ctx):
    """Creating a bucket another user owns returns 409 BucketAlreadyExists."""
    alt = ctx.alt
    b = ctx.new_bucket()
    error(alt.create_bucket(b, location=ctx.cfg.location_constraint), 409, 'BucketAlreadyExists')


@tags('smoke')
def test_list_buckets_includes_new_bucket(ctx):
    """ListBuckets shows a new bucket with CreationDate and the Owner element."""
    b = ctx.new_bucket()
    root = ok(ctx.c.list_buckets()).xml()
    contains(s3xml.bucket_names(root), b, 'ListBuckets names')
    for el in root.iter('Bucket'):
        if el.findtext('Name') == b:
            eq(bool(el.findtext('CreationDate')), True, 'CreationDate present')
    eq(bool(root.findtext('Owner/ID')), True, 'Owner/ID present')


@tags('multiuser')
def test_list_buckets_excludes_other_users(ctx):
    """ListBuckets for one user does not show another user's buckets."""
    alt = ctx.alt
    b = ctx.new_bucket(client=alt)
    not_contains(s3xml.bucket_names(ok(ctx.c.list_buckets()).xml()), b, 'main user ListBuckets')


def test_get_bucket_location(ctx):
    """GET ?location returns a LocationConstraint document."""
    b = ctx.new_bucket()
    root = ok(ctx.c.get_bucket_sub(b, 'location')).xml()
    eq(root.tag, 'LocationConstraint', 'root element')


def test_bucket_name_valid_forms(ctx):
    """63-character names and names with dots, dashes and digits are accepted."""
    names = [ctx.bucket_name(length=63), ctx.bucket_name().replace('-', '.', 1), ctx.bucket_name() + '-9-z']
    for n in names:
        ok(ctx.c.create_bucket(n, location=ctx.cfg.location_constraint), 'create bucket %r' % n)
        ctx.register_bucket(n)


@tags('naming')
def test_bucket_name_invalid_forms(ctx):
    """Invalid bucket names are rejected with 400 InvalidBucketName."""
    base = ctx.bucket_name()
    bad = {
        'too short': 'ab',
        'too long (64)': (base + 'x' * 64)[:64],
        'uppercase': base + 'Upper',
        'underscore': base + '_x',
        'starts with dash': '-' + base,
        'ends with dash': base + '-',
        'ip address': '192.168.5.%d' % (len(base) % 200 + 1),
        'adjacent dots': base + '..x',
        'dash dot': base + '-.x',
    }
    failures = []
    for why, name in sorted(bad.items()):
        r = ctx.c.create_bucket(name, location=ctx.cfg.location_constraint)
        if r.ok:
            ctx.register_bucket(name)
        if r.status != 400 or r.error_code != 'InvalidBucketName':
            failures.append('%s %r -> HTTP %d %s' % (why, name, r.status, r.error_code))
    eq(failures, [], 'names not rejected with 400 InvalidBucketName')


@tags('multiuser', 'permissions')
def test_other_user_cannot_list_private_bucket(ctx):
    """Another user listing a private bucket gets 403 AccessDenied."""
    alt = ctx.alt
    b = ctx.new_bucket()
    error(alt.list_objects(b), 403, 'AccessDenied')
    error(alt.head_bucket(b), 403, what='alt HEAD bucket')


@tags('multiuser', 'permissions')
def test_other_user_cannot_delete_bucket(ctx):
    """Another user cannot delete a bucket they do not own."""
    alt = ctx.alt
    b = ctx.new_bucket()
    error(alt.delete_bucket(b), 403, 'AccessDenied')
    ok(ctx.c.head_bucket(b), 'bucket still exists')
