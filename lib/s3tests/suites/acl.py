"""ACLs: canned ACLs, explicit grants, anonymous access.

Tests that need ACLs or public access create their bucket with
ctx.acl_bucket(), which asks for ObjectWriter ownership and removes the
bucket-level public access block. If the platform or account still refuses
(AWS account-level Block Public Access, ACLs disabled), the test is SKIPPED
with the reason rather than failed.
"""

from ..assertions import contains, eq, error, ok, skip_if_disabled, true
from ..runner import tags

TAGS = ('acl', 'permissions')


def _grants(root):
    out = []
    for g in root.iter('Grant'):
        grantee = g.find('Grantee')
        who = grantee.findtext('ID') or grantee.findtext('URI') or grantee.findtext('EmailAddress')
        out.append((who, g.findtext('Permission')))
    return out


def _okacl(resp, what):
    """2xx, or SKIP if ACLs/public access are administratively disabled."""
    if not resp.ok:
        skip_if_disabled(resp, what)
    return ok(resp, what)


def test_bucket_acl_default_owner_full_control(ctx):
    """A new bucket's ACL names the owner with FULL_CONTROL."""
    b = ctx.new_bucket()
    root = ok(ctx.c.get_bucket_sub(b, 'acl')).xml()
    owner = ctx.user_id(ctx.c)
    eq(root.findtext('Owner/ID'), owner, 'Owner/ID')
    contains(_grants(root), (owner, 'FULL_CONTROL'), 'grants')


def test_object_acl_default_owner_full_control(ctx):
    """A new object's ACL names the owner with FULL_CONTROL."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    root = ok(ctx.c.get_object_sub(b, 'k', 'acl')).xml()
    contains(_grants(root), (ctx.user_id(ctx.c), 'FULL_CONTROL'), 'grants')


@tags('anonymous')
def test_private_object_denies_anonymous(ctx):
    """Anonymous GET of a private object returns 403 AccessDenied."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    error(ctx.anon.get_object(b, 'k'), 403, 'AccessDenied')
    error(ctx.anon.list_objects(b), 403, 'AccessDenied', 'anonymous list')


@tags('anonymous')
def test_canned_public_read_object(ctx):
    """x-amz-acl: public-read on PUT lets anonymous clients GET the object."""
    b = ctx.acl_bucket()
    _okacl(ctx.c.put_object(b, 'k', b'pub', headers={'x-amz-acl': 'public-read'}), 'PUT with public-read')
    r = ctx.eventually(lambda: ctx.anon.get_object(b, 'k'), lambda r: r.ok, 'public-read ACL')
    eq(ok(r, 'anonymous GET').body, b'pub', 'body')
    error(ctx.anon.put_object(b, 'k', b'overwrite'), 403, 'AccessDenied', 'anonymous overwrite')


@tags('anonymous')
def test_canned_public_read_bucket(ctx):
    """A public-read bucket can be listed anonymously; its objects stay private."""
    b = ctx.acl_bucket()
    _okacl(ctx.c.request('PUT', b, query=[('acl', None)], headers={'x-amz-acl': 'public-read'}),
           'PUT bucket ACL public-read')
    ok(ctx.c.put_object(b, 'k', b'x'))
    ok(ctx.eventually(lambda: ctx.anon.list_objects(b), lambda r: r.ok, 'public-read bucket ACL'), 'anonymous list')
    error(ctx.anon.get_object(b, 'k'), 403, 'AccessDenied', 'anonymous GET of private object')


@tags('anonymous')
def test_put_object_acl_after_upload(ctx):
    """PUT ?acl with x-amz-acl changes access on an existing object, and back."""
    b = ctx.acl_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    _okacl(ctx.c.request('PUT', b, 'k', query=[('acl', None)], headers={'x-amz-acl': 'public-read'}),
           'PUT object ACL public-read')
    ok(ctx.eventually(lambda: ctx.anon.get_object(b, 'k'), lambda r: r.ok, 'public-read ACL'),
       'anonymous GET after public-read')
    ok(ctx.c.request('PUT', b, 'k', query=[('acl', None)], headers={'x-amz-acl': 'private'}))
    error(ctx.eventually(lambda: ctx.anon.get_object(b, 'k'), lambda r: r.status == 403, 'private ACL'),
          403, 'AccessDenied', 'anonymous GET after private')


@tags('multiuser')
def test_grant_read_to_other_user(ctx):
    """x-amz-grant-read for the second user's canonical ID allows that user to GET."""
    alt = ctx.alt
    b = ctx.acl_bucket()
    ok(ctx.c.put_object(b, 'private', b'x'))
    _okacl(ctx.c.put_object(b, 'shared', b'y', headers={'x-amz-grant-read': 'id="%s"' % ctx.user_id(alt)}),
           'PUT with x-amz-grant-read')
    error(alt.get_object(b, 'private'), 403, 'AccessDenied', 'alt GET private')
    eq(ok(ctx.eventually(lambda: alt.get_object(b, 'shared'), lambda r: r.ok, 'read grant'),
          'alt GET shared').body, b'y', 'body')
    error(alt.put_object(b, 'shared', b'z'), 403, 'AccessDenied', 'alt write without WRITE grant')


@tags('multiuser')
def test_grant_write_bucket_to_other_user(ctx):
    """x-amz-grant-write on a bucket lets the second user upload into it."""
    alt = ctx.alt
    b = ctx.acl_bucket()
    error(alt.put_object(b, 'k', b'x'), 403, 'AccessDenied', 'alt PUT before grant')
    owner = ctx.user_id(ctx.c)
    _okacl(ctx.c.request('PUT', b, query=[('acl', None)], headers={
        'x-amz-grant-full-control': 'id="%s"' % owner, 'x-amz-grant-write': 'id="%s"' % ctx.user_id(alt)}),
        'PUT bucket ACL with grants')
    ok(ctx.eventually(lambda: alt.put_object(b, 'k', b'x'), lambda r: r.ok, 'write grant'), 'alt PUT after grant')


@tags('anonymous')
def test_acl_xml_roundtrip(ctx):
    """An AccessControlPolicy document written with PUT ?acl reads back the same grants."""
    b = ctx.acl_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    owner = ctx.user_id(ctx.c)
    body = ('<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>%s</ID></Owner>'
            '<AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:type="CanonicalUser"><ID>%s</ID></Grantee><Permission>FULL_CONTROL</Permission></Grant>'
            '<Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Group">'
            '<URI>http://acs.amazonaws.com/groups/global/AllUsers</URI></Grantee><Permission>READ</Permission>'
            '</Grant></AccessControlList></AccessControlPolicy>') % (owner, owner)
    _okacl(ctx.c.put_object_sub(b, 'k', 'acl', body), 'PUT ACL document')
    grants = _grants(ok(ctx.c.get_object_sub(b, 'k', 'acl')).xml())
    contains(grants, (owner, 'FULL_CONTROL'), 'grants')
    contains(grants, ('http://acs.amazonaws.com/groups/global/AllUsers', 'READ'), 'grants')
    r = ctx.eventually(lambda: ctx.anon.get_object(b, 'k'), lambda r: r.ok, 'AllUsers READ grant')
    true(r.status == 200, 'anonymous GET should succeed after AllUsers READ, got HTTP %d' % r.status)
