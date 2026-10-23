"""Bucket versioning: enable, suspend, versions, delete markers."""

from .. import s3xml
from ..assertions import eq, error, ok, status, true
from ..runner import tags

TAGS = ('versioning',)


def _vstatus(ctx, b):
    return ok(ctx.c.get_bucket_sub(b, 'versioning')).xml().findtext('Status')


def _versions(ctx, b, key=None):
    vs = s3xml.versions(ok(ctx.c.list_object_versions(b), 'ListObjectVersions').xml())
    return [v for v in vs if key is None or v['Key'] == key]


def test_versioning_default_unset(ctx):
    """A new bucket has no versioning Status."""
    eq(_vstatus(ctx, ctx.new_bucket()), None, 'Status')


def test_versioning_enable_suspend(ctx):
    """Versioning can be enabled and then suspended."""
    b = ctx.new_bucket()
    ok(ctx.c.set_versioning(b, 'Enabled'))
    eq(_vstatus(ctx, b), 'Enabled', 'Status')
    ok(ctx.c.set_versioning(b, 'Suspended'))
    eq(_vstatus(ctx, b), 'Suspended', 'Status')


def test_versioning_invalid_status(ctx):
    """An invalid versioning status is rejected with 400."""
    b = ctx.new_bucket()
    error(ctx.c.set_versioning(b, 'Sideways'), 400, ('MalformedXML', 'IllegalVersioningConfigurationException'))


@tags('smoke')
def test_versioned_puts_keep_all_versions(ctx):
    """Two PUTs create two versions retrievable by versionId."""
    b = ctx.versioned_bucket()
    r1 = ok(ctx.c.put_object(b, 'k', b'one'))
    r2 = ok(ctx.c.put_object(b, 'k', b'two'))
    v1, v2 = r1.header('x-amz-version-id'), r2.header('x-amz-version-id')
    true(v1 and v2 and v1 != v2, 'distinct x-amz-version-id headers expected, got %r and %r' % (v1, v2))
    eq(ok(ctx.c.get_object(b, 'k', version_id=v1)).body, b'one', 'v1 body')
    eq(ok(ctx.c.get_object(b, 'k', version_id=v2)).body, b'two', 'v2 body')
    eq(ok(ctx.c.get_object(b, 'k')).body, b'two', 'latest body')
    vs = _versions(ctx, b, 'k')
    eq(sorted(v['VersionId'] for v in vs), sorted([v1, v2]), 'listed version ids')
    eq([v['VersionId'] for v in vs if v['IsLatest']], [v2], 'IsLatest')


def test_get_response_has_version_id(ctx):
    """GET and HEAD on a versioned object return x-amz-version-id."""
    b = ctx.versioned_bucket()
    vid = ok(ctx.c.put_object(b, 'k', b'x')).header('x-amz-version-id')
    eq(ok(ctx.c.get_object(b, 'k')).header('x-amz-version-id'), vid, 'GET version id')
    eq(ok(ctx.c.head_object(b, 'k')).header('x-amz-version-id'), vid, 'HEAD version id')


def test_delete_creates_delete_marker(ctx):
    """DELETE without versionId adds a delete marker; GET then returns 404; data versions remain."""
    b = ctx.versioned_bucket()
    vid = ok(ctx.c.put_object(b, 'k', b'x')).header('x-amz-version-id')
    r = status(ctx.c.delete_object(b, 'k'), 204, 'delete')
    eq(r.header('x-amz-delete-marker'), 'true', 'x-amz-delete-marker')
    marker = r.header('x-amz-version-id')
    true(bool(marker), 'delete marker version id missing')
    error(ctx.c.get_object(b, 'k'), 404, 'NoSuchKey')
    eq(ok(ctx.c.get_object(b, 'k', version_id=vid)).body, b'x', 'old version still readable')
    vs = _versions(ctx, b, 'k')
    dms = [v for v in vs if v['kind'] == 'DeleteMarker']
    eq([(d['VersionId'], d['IsLatest']) for d in dms], [(marker, True)], 'delete markers')


def test_delete_marker_removal_restores_object(ctx):
    """Deleting the delete marker version makes the object visible again."""
    b = ctx.versioned_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    marker = ctx.c.delete_object(b, 'k').header('x-amz-version-id')
    r = status(ctx.c.delete_object(b, 'k', version_id=marker), 204, 'delete the marker')
    eq(r.header('x-amz-delete-marker'), 'true', 'x-amz-delete-marker on marker removal')
    eq(ok(ctx.c.get_object(b, 'k')).body, b'x', 'restored body')


def test_delete_specific_version(ctx):
    """Deleting a specific version removes only that version permanently."""
    b = ctx.versioned_bucket()
    v1 = ok(ctx.c.put_object(b, 'k', b'one')).header('x-amz-version-id')
    v2 = ok(ctx.c.put_object(b, 'k', b'two')).header('x-amz-version-id')
    r = status(ctx.c.delete_object(b, 'k', version_id=v2), 204)
    eq(r.header('x-amz-version-id'), v2, 'deleted version id echo')
    eq(ok(ctx.c.get_object(b, 'k')).body, b'one', 'previous version is now latest')
    eq([v['VersionId'] for v in _versions(ctx, b, 'k')], [v1], 'remaining versions')


def test_suspended_versioning_null_version(ctx):
    """With versioning suspended, PUTs overwrite a single null version and keep older versions."""
    b = ctx.versioned_bucket()
    v1 = ok(ctx.c.put_object(b, 'k', b'one')).header('x-amz-version-id')
    ok(ctx.c.set_versioning(b, 'Suspended'))
    ok(ctx.c.put_object(b, 'k', b'two'))
    ok(ctx.c.put_object(b, 'k', b'three'))
    vs = _versions(ctx, b, 'k')
    eq(sorted(v['VersionId'] for v in vs), sorted([v1, 'null']), 'version ids')
    eq(ok(ctx.c.get_object(b, 'k', version_id='null')).body, b'three', 'null version body')
    eq(ok(ctx.c.get_object(b, 'k', version_id=v1)).body, b'one', 'v1 body')


def test_get_bad_version_id(ctx):
    """GET with a nonexistent versionId returns 400 or 404."""
    b = ctx.versioned_bucket()
    ok(ctx.c.put_object(b, 'k', b'x'))
    error(ctx.c.get_object(b, 'k', version_id='nonexistentversionid0000000000000'), (400, 404),
          ('NoSuchVersion', 'InvalidArgument', 'NoSuchKey'))


def test_list_versions_paging(ctx):
    """ListObjectVersions pages with max-keys and key/version-id markers."""
    b = ctx.versioned_bucket()
    expected = set()
    for k in ('a', 'b'):
        for i in range(2):
            expected.add((k, ok(ctx.c.put_object(b, k, str(i))).header('x-amz-version-id')))
    got, params = set(), {'max-keys': '1'}
    for _ in range(20):
        root = ok(ctx.c.list_object_versions(b, params)).xml()
        got |= set((v['Key'], v['VersionId']) for v in s3xml.versions(root))
        if not s3xml.truncated(root):
            break
        params['key-marker'] = root.findtext('NextKeyMarker')
        params['version-id-marker'] = root.findtext('NextVersionIdMarker')
    eq(got, expected, 'all versions')


def test_list_versions_prefix_delimiter(ctx):
    """ListObjectVersions honours prefix and delimiter."""
    b = ctx.versioned_bucket()
    for k in ('dir/a', 'dir/sub/b', 'top'):
        ok(ctx.c.put_object(b, k, b'x'))
    root = ok(ctx.c.list_object_versions(b, {'prefix': 'dir/', 'delimiter': '/'})).xml()
    eq([v['Key'] for v in s3xml.versions(root)], ['dir/a'], 'keys')
    eq(s3xml.common_prefixes(root), ['dir/sub/'], 'CommonPrefixes')


def test_copy_specific_version(ctx):
    """CopyObject with ?versionId on the source copies that version."""
    b = ctx.versioned_bucket()
    v1 = ok(ctx.c.put_object(b, 'k', b'one')).header('x-amz-version-id')
    ok(ctx.c.put_object(b, 'k', b'two'))
    r = ok(ctx.c.copy_object(b, 'dst', b, 'k', src_version=v1))
    eq(r.header('x-amz-copy-source-version-id'), v1, 'x-amz-copy-source-version-id')
    eq(ok(ctx.c.get_object(b, 'dst')).body, b'one', 'copied body')
