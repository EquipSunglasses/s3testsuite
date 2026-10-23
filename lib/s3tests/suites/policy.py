"""Bucket policies: storage, validation and enforcement."""

import json

from ..assertions import eq, error, ok, skip_if_disabled, status
from ..runner import tags

TAGS = ('policy', 'permissions')


def _doc(b, effect, principal, actions, resource_suffix='/*'):
    return json.dumps({'Version': '2012-10-17', 'Statement': [{
        'Sid': 's3tests', 'Effect': effect, 'Principal': principal, 'Action': actions,
        'Resource': ['arn:aws:s3:::%s%s' % (b, resource_suffix)]}]})


def _put(ctx, b, doc):
    return ctx.c.put_bucket_sub(b, 'policy', doc, headers={'Content-Type': 'application/json'}, md5=False)


def test_policy_none_configured(ctx):
    """GET ?policy on a new bucket returns 404 NoSuchBucketPolicy."""
    error(ctx.c.get_bucket_sub(ctx.new_bucket(), 'policy'), 404, 'NoSuchBucketPolicy')


def test_policy_roundtrip(ctx):
    """PUT/GET/DELETE ?policy; the stored document is equivalent JSON."""
    b = ctx.new_bucket()
    # A Deny statement is not "public", so this works with Block Public Access on.
    doc = _doc(b, 'Deny', {'AWS': ['*']}, ['s3:DeleteObjectTagging'])
    status(_put(ctx, b, doc), (200, 204), 'put policy')
    got = json.loads(ok(ctx.c.get_bucket_sub(b, 'policy')).text)
    eq(got['Statement'][0]['Effect'], 'Deny', 'Effect')
    eq(got['Statement'][0]['Sid'], 's3tests', 'Sid')
    status(ctx.c.delete_bucket_sub(b, 'policy'), 204, 'delete policy')
    error(ctx.c.get_bucket_sub(b, 'policy'), 404, 'NoSuchBucketPolicy')


def test_policy_malformed_json(ctx):
    """A non-JSON policy is rejected with 400 MalformedPolicy."""
    error(_put(ctx, ctx.new_bucket(), '{"Version": "2012-10-17", "Statement": ['), 400, 'MalformedPolicy')


def test_policy_wrong_resource_bucket(ctx):
    """A policy whose Resource names another bucket is rejected with 400 MalformedPolicy."""
    b = ctx.new_bucket()
    doc = _doc(ctx.bucket_name(), 'Allow', {'AWS': ['*']}, ['s3:GetObject'])
    error(_put(ctx, b, doc), 400, 'MalformedPolicy')


@tags('anonymous')
def test_policy_public_read(ctx):
    """An Allow s3:GetObject policy for * lets anonymous clients read objects."""
    b = ctx.acl_bucket()
    ok(ctx.c.put_object(b, 'k', b'public'))
    error(ctx.anon.get_object(b, 'k'), 403, 'AccessDenied', 'anonymous GET before policy')
    r = _put(ctx, b, _doc(b, 'Allow', {'AWS': ['*']}, ['s3:GetObject']))
    if not r.ok:
        skip_if_disabled(r, 'public bucket policy')
    status(r, (200, 204), 'put public policy')
    r = ctx.eventually(lambda: ctx.anon.get_object(b, 'k'), lambda r: r.ok, 'public-read policy')
    eq(ok(r, 'anonymous GET after policy').body, b'public', 'body')
    error(ctx.anon.put_object(b, 'other', b'x'), 403, 'AccessDenied', 'anonymous PUT stays denied')


def test_policy_explicit_deny_applies_to_owner(ctx):
    """An explicit Deny on s3:GetObject blocks the bucket owner too."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    status(_put(ctx, b, _doc(b, 'Deny', {'AWS': ['*']}, ['s3:GetObject'])), (200, 204))
    error(ctx.eventually(lambda: ctx.c.get_object(b, 'k'), lambda r: r.status == 403, 'deny policy'),
          403, 'AccessDenied', 'owner GET under deny')
    status(ctx.c.delete_bucket_sub(b, 'policy'), 204, 'owner can still delete the policy')
    ok(ctx.eventually(lambda: ctx.c.get_object(b, 'k'), lambda r: r.ok, 'policy removal'),
       'owner GET after policy removal')


@tags('multiuser')
def test_policy_grants_other_user(ctx):
    """A policy naming the second user's principal grants read and list access."""
    alt = ctx.alt
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'shared'))
    error(alt.get_object(b, 'k'), 403, 'AccessDenied', 'alt GET before policy')
    doc = json.dumps({'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Principal': {'AWS': [ctx.alt_principal()]}, 'Action': ['s3:GetObject'],
         'Resource': ['arn:aws:s3:::%s/*' % b]},
        {'Effect': 'Allow', 'Principal': {'AWS': [ctx.alt_principal()]}, 'Action': ['s3:ListBucket'],
         'Resource': ['arn:aws:s3:::%s' % b]}]})
    status(_put(ctx, b, doc), (200, 204), 'put policy')
    eq(ok(ctx.eventually(lambda: alt.get_object(b, 'k'), lambda r: r.ok, 'policy grant'), 'alt GET').body,
       b'shared', 'body')
    ok(alt.list_objects_v2(b), 'alt list')
    error(alt.delete_object(b, 'k'), 403, 'AccessDenied', 'alt DELETE not granted')


@tags('multiuser')
def test_policy_other_user_cannot_change_policy(ctx):
    """A non-owner cannot read or write the bucket policy."""
    alt = ctx.alt
    b = ctx.new_bucket()
    error(alt.put_bucket_sub(b, 'policy', _doc(b, 'Allow', {'AWS': ['*']}, ['s3:GetObject']), md5=False),
          403, 'AccessDenied')
    error(alt.get_bucket_sub(b, 'policy'), (403, 404), what='alt GET policy')
