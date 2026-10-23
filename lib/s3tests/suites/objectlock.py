"""Object Lock (WORM): bucket setup, GOVERNANCE retention, legal hold.

Only GOVERNANCE mode is used so every test object can be removed afterwards
with x-amz-bypass-governance-retention. COMPLIANCE mode would leave objects
that cannot be deleted until their retention date.
"""

import datetime

from ..assertions import eq, error, ok, skip_if_unimplemented, status
from ..client import md5_b64, x, xml_doc

TAGS = ('worm',)
LOCK_HDR = {'x-amz-bucket-object-lock-enabled': 'true'}
BYPASS = {'x-amz-bypass-governance-retention': 'true'}


def _put(ctx, b, k, data, headers=None):
    # S3 requires an integrity header for uploads to buckets with Object Lock retention.
    hdrs = dict(headers or {})
    hdrs['Content-MD5'] = md5_b64(data)
    return ctx.c.put_object(b, k, data, headers=hdrs)


def _lock_bucket(ctx):
    name = ctx.bucket_name()
    r = ctx.c.create_bucket(name, headers=LOCK_HDR, location=ctx.cfg.location_constraint)
    skip_if_unimplemented(r, 'object lock')
    ok(r, 'create bucket with object lock')
    ctx.register_bucket(name)
    return name


def _until(seconds):
    t = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    return t.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def test_object_lock_bucket_enables_versioning(ctx):
    """Creating a lock-enabled bucket enables versioning and reports ObjectLockEnabled."""
    b = _lock_bucket(ctx)
    eq(ok(ctx.c.get_bucket_sub(b, 'versioning')).xml().findtext('Status'), 'Enabled', 'versioning')
    eq(ok(ctx.c.get_bucket_sub(b, 'object-lock')).xml().findtext('ObjectLockEnabled'), 'Enabled',
       'ObjectLockEnabled')


def test_object_lock_default_retention(ctx):
    """A default GOVERNANCE retention applies to new objects and blocks version deletes without bypass."""
    b = _lock_bucket(ctx)
    cfg = xml_doc('ObjectLockConfiguration', '<ObjectLockEnabled>Enabled</ObjectLockEnabled><Rule>'
                  '<DefaultRetention><Mode>GOVERNANCE</Mode><Days>1</Days></DefaultRetention></Rule>')
    ok(ctx.c.put_bucket_sub(b, 'object-lock', cfg), 'put object lock configuration')
    root = ok(ctx.c.get_bucket_sub(b, 'object-lock')).xml()
    eq(root.findtext('Rule/DefaultRetention/Mode'), 'GOVERNANCE', 'Mode')
    eq(root.findtext('Rule/DefaultRetention/Days'), '1', 'Days')
    vid = ok(_put(ctx, b, 'k', b'x')).header('x-amz-version-id')
    eq(ok(ctx.c.head_object(b, 'k')).header('x-amz-object-lock-mode'), 'GOVERNANCE', 'object lock mode')
    error(ctx.c.delete_object(b, 'k', version_id=vid), 403, 'AccessDenied', 'delete locked version')
    status(ctx.c.delete_object(b, 'k', version_id=vid, headers=BYPASS), 204, 'delete with bypass')


def test_object_retention_explicit(ctx):
    """PUT/GET ?retention on a version."""
    b = _lock_bucket(ctx)
    vid = ok(_put(ctx, b, 'k', b'x')).header('x-amz-version-id')
    until = _until(3600)
    ok(ctx.c.put_object_sub(b, 'k', 'retention',
                            xml_doc('Retention', x('Mode', 'GOVERNANCE') + x('RetainUntilDate', until)),
                            version_id=vid), 'put retention')
    root = ok(ctx.c.get_object_sub(b, 'k', 'retention', version_id=vid)).xml()
    eq(root.findtext('Mode'), 'GOVERNANCE', 'Mode')
    eq((root.findtext('RetainUntilDate') or '')[:19], until[:19], 'RetainUntilDate')
    error(ctx.c.delete_object(b, 'k', version_id=vid), 403, 'AccessDenied', 'delete retained version')


def test_object_retention_headers_on_put(ctx):
    """x-amz-object-lock-mode and retain-until-date on PUT set retention."""
    b = _lock_bucket(ctx)
    r = ok(_put(ctx, b, 'k', b'x', headers={'x-amz-object-lock-mode': 'GOVERNANCE',
                                                    'x-amz-object-lock-retain-until-date': _until(3600)}))
    h = ok(ctx.c.head_object(b, 'k', version_id=r.header('x-amz-version-id')))
    eq(h.header('x-amz-object-lock-mode'), 'GOVERNANCE', 'mode header')


def test_legal_hold(ctx):
    """A legal hold blocks deletion even with governance bypass until it is removed."""
    b = _lock_bucket(ctx)
    vid = ok(_put(ctx, b, 'k', b'x')).header('x-amz-version-id')
    ok(ctx.c.put_object_sub(b, 'k', 'legal-hold', xml_doc('LegalHold', x('Status', 'ON')), version_id=vid))
    eq(ok(ctx.c.get_object_sub(b, 'k', 'legal-hold', version_id=vid)).xml().findtext('Status'), 'ON', 'Status')
    error(ctx.c.delete_object(b, 'k', version_id=vid, headers=BYPASS), 403, 'AccessDenied', 'delete under hold')
    ok(ctx.c.put_object_sub(b, 'k', 'legal-hold', xml_doc('LegalHold', x('Status', 'OFF')), version_id=vid))
    status(ctx.c.delete_object(b, 'k', version_id=vid), 204, 'delete after hold removed')


def test_object_lock_on_unlocked_bucket(ctx):
    """Object lock configuration on a bucket created without it is rejected (409 or 400)."""
    b = ctx.new_bucket()
    cfg = xml_doc('ObjectLockConfiguration', '<ObjectLockEnabled>Enabled</ObjectLockEnabled>')
    r = ctx.c.put_bucket_sub(b, 'object-lock', cfg)
    skip_if_unimplemented(r, 'object lock')
    error(r, (400, 409), ('InvalidBucketState', 'InvalidRequest'))


def test_object_lock_suspend_versioning_denied(ctx):
    """Versioning cannot be suspended on a lock-enabled bucket."""
    b = _lock_bucket(ctx)
    error(ctx.c.set_versioning(b, 'Suspended'), (400, 409), ('InvalidBucketState', 'InvalidRequest'))
