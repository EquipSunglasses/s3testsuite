"""Bucket lifecycle configuration and (optionally) expiration processing."""

import time

from .. import s3xml
from ..assertions import NOT_CONFIGURED, eq, error, ok, skip, true
from ..client import xml_doc
from ..runner import tags

TAGS = ('lifecycle',)


def lc_xml(rules):
    return xml_doc('LifecycleConfiguration', ''.join(rules))


def rule(rid, body, prefix='', status='Enabled', filt=None):
    f = filt if filt is not None else '<Filter><Prefix>%s</Prefix></Filter>' % prefix
    return '<Rule><ID>%s</ID>%s<Status>%s</Status>%s</Rule>' % (rid, f, status, body)


def _get_rules(ctx, b):
    return ok(ctx.c.get_bucket_sub(b, 'lifecycle'), 'get lifecycle').xml().findall('Rule')


def test_lifecycle_none_configured(ctx):
    """GET ?lifecycle on a new bucket returns 404 NoSuchLifecycleConfiguration."""
    error(ctx.c.get_bucket_sub(ctx.new_bucket(), 'lifecycle'), 404, 'NoSuchLifecycleConfiguration')


def test_lifecycle_expiration_days_roundtrip(ctx):
    """An Expiration Days rule with a prefix filter round-trips; DELETE removes it."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('expire-logs', '<Expiration><Days>30</Days></Expiration>',
                                                         prefix='logs/')])), 'put lifecycle')
    rules = _get_rules(ctx, b)
    eq(len(rules), 1, 'rule count')
    r = rules[0]
    eq(r.findtext('ID'), 'expire-logs', 'ID')
    eq(r.findtext('Status'), 'Enabled', 'Status')
    eq(r.findtext('Expiration/Days'), '30', 'Expiration/Days')
    eq(r.findtext('Filter/Prefix') or r.findtext('Prefix'), 'logs/', 'prefix')
    ok(ctx.c.delete_bucket_sub(b, 'lifecycle'), 'delete lifecycle')
    error(ctx.c.get_bucket_sub(b, 'lifecycle'), 404, 'NoSuchLifecycleConfiguration')


def test_lifecycle_abort_incomplete_multipart(ctx):
    """AbortIncompleteMultipartUpload DaysAfterInitiation round-trips."""
    b = ctx.new_bucket()
    body = '<AbortIncompleteMultipartUpload><DaysAfterInitiation>7</DaysAfterInitiation></AbortIncompleteMultipartUpload>'
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('mpu-cleanup', body)])))
    eq(_get_rules(ctx, b)[0].findtext('AbortIncompleteMultipartUpload/DaysAfterInitiation'), '7', 'days')


@tags('versioning')
def test_lifecycle_noncurrent_expiration(ctx):
    """NoncurrentVersionExpiration and ExpiredObjectDeleteMarker rules round-trip."""
    b = ctx.versioned_bucket()
    rules = [
        rule('noncurrent', '<NoncurrentVersionExpiration><NoncurrentDays>10</NoncurrentDays>'
                           '</NoncurrentVersionExpiration>'),
        rule('markers', '<Expiration><ExpiredObjectDeleteMarker>true</ExpiredObjectDeleteMarker></Expiration>',
             prefix='m/'),
    ]
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml(rules)))
    got = dict((r.findtext('ID'), r) for r in _get_rules(ctx, b))
    eq(got['noncurrent'].findtext('NoncurrentVersionExpiration/NoncurrentDays'), '10', 'NoncurrentDays')
    eq(s3xml.is_true(got['markers'].findtext('Expiration/ExpiredObjectDeleteMarker')), True,
       'ExpiredObjectDeleteMarker')


def test_lifecycle_filter_and_tags(ctx):
    """A Filter with And (prefix + tag) round-trips."""
    b = ctx.new_bucket()
    filt = ('<Filter><And><Prefix>tmp/</Prefix><Tag><Key>class</Key><Value>scratch</Value></Tag>'
            '</And></Filter>')
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('and', '<Expiration><Days>1</Days></Expiration>',
                                                         filt=filt)])))
    r = _get_rules(ctx, b)[0]
    eq(r.findtext('Filter/And/Prefix'), 'tmp/', 'And/Prefix')
    eq(r.findtext('Filter/And/Tag/Key'), 'class', 'And/Tag/Key')


def test_lifecycle_disabled_rule(ctx):
    """A Disabled rule is stored with Status Disabled."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('off', '<Expiration><Days>5</Days></Expiration>',
                                                         status='Disabled')])))
    eq(_get_rules(ctx, b)[0].findtext('Status'), 'Disabled', 'Status')


def test_lifecycle_replace_config(ctx):
    """A second PUT replaces the whole configuration."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('one', '<Expiration><Days>5</Days></Expiration>')])))
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('two', '<Expiration><Days>6</Days></Expiration>')])))
    eq([r.findtext('ID') for r in _get_rules(ctx, b)], ['two'], 'rule ids')


def test_lifecycle_invalid_days(ctx):
    """Expiration Days of 0 is rejected with 400."""
    b = ctx.new_bucket()
    r = ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('zero', '<Expiration><Days>0</Days></Expiration>')]))
    error(r, 400, ('InvalidArgument', 'MalformedXML'))


def test_lifecycle_duplicate_ids(ctx):
    """Two rules with the same ID are rejected with 400 InvalidArgument."""
    b = ctx.new_bucket()
    rules = [rule('dup', '<Expiration><Days>1</Days></Expiration>', prefix='a/'),
             rule('dup', '<Expiration><Days>2</Days></Expiration>', prefix='b/')]
    error(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml(rules)), 400, ('InvalidArgument', 'MalformedXML'))


def test_lifecycle_malformed_xml(ctx):
    """A malformed lifecycle document is rejected with 400 MalformedXML."""
    b = ctx.new_bucket()
    error(ctx.c.put_bucket_sub(b, 'lifecycle', b'<LifecycleConfiguration><Rule>'), 400, 'MalformedXML')


@tags('slow', 'lifecycle_expiration')
def test_lifecycle_expiration_processing(ctx):
    """Objects under an Expiration Days=1 rule are removed by the lifecycle worker.

    Needs a lab gateway whose lifecycle interval is shortened for testing
    and [run] lifecycle_wait_seconds > 0.
    """
    wait = ctx.cfg.lifecycle_wait_seconds
    if wait <= 0:
        skip('set [run] lifecycle_wait_seconds (and shorten the gateway lifecycle interval) to run',
             NOT_CONFIGURED)
    ctx.extend_deadline(wait + 120)
    b = ctx.new_bucket()
    for i in range(3):
        ok(ctx.c.put_object(b, 'expire/%d' % i, b'x'))
    ok(ctx.c.put_object(b, 'keep/0', b'x'))
    ok(ctx.c.put_bucket_sub(b, 'lifecycle', lc_xml([rule('exp', '<Expiration><Days>1</Days></Expiration>',
                                                         prefix='expire/')])))
    deadline = time.time() + wait
    remaining = None
    while time.time() < deadline:
        remaining = s3xml.keys(ok(ctx.c.list_objects_v2(b)).xml())
        if remaining == ['keep/0']:
            return
        time.sleep(min(10, max(1, wait / 20.0)))
    true(False, 'after %ds keys are %r, expected only keep/0' % (wait, remaining))
