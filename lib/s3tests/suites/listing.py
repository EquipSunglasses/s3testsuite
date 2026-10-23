"""ListObjects (v1) and ListObjectsV2: prefixes, delimiters, paging, encoding."""

import random
from urllib.parse import unquote_plus

from .. import s3xml
from ..assertions import eq, error, ok, true
from ..runner import tags

TAGS = ('listing',)
APIS = ('v1', 'v2')


def _fill(ctx, keys):
    b = ctx.new_bucket()
    keys = list(keys)
    random.shuffle(keys)
    for k in keys:
        ok(ctx.c.put_object(b, k, b''), 'setup put %r' % k)
    return b


def _list(ctx, b, api, params=None):
    params = dict(params or {})
    if api == 'v1':
        return ok(ctx.c.list_objects(b, params), 'ListObjects %r' % params).xml()
    return ok(ctx.c.list_objects_v2(b, params), 'ListObjectsV2 %r' % params).xml()


def _list_all(ctx, b, api, params, page_size):
    """Page through a listing; returns (keys, prefixes, page_count)."""
    params = dict(params, **{'max-keys': str(page_size)})
    keys, prefixes, pages = [], [], 0
    while True:
        root = _list(ctx, b, api, params)
        pages += 1
        k, p = s3xml.keys(root), s3xml.common_prefixes(root)
        keys += k
        prefixes += p
        if not s3xml.truncated(root):
            return keys, prefixes, pages
        true(pages < 1000, 'listing never ends')
        if api == 'v2':
            token = root.findtext('NextContinuationToken')
            true(bool(token), 'IsTruncated but no NextContinuationToken')
            params['continuation-token'] = token
        else:
            marker = root.findtext('NextMarker') or max(k[-1:] + p[-1:] or [''])
            true(bool(marker), 'IsTruncated but no NextMarker and no entries')
            params['marker'] = marker


@tags('smoke')
def test_list_basic_sorted(ctx):
    """Both APIs return all keys in lexicographic order."""
    keys = ['foo', 'bar', 'baz', 'quux', 'a/b']
    b = _fill(ctx, keys)
    for api in APIS:
        eq(s3xml.keys(_list(ctx, b, api)), sorted(keys), '%s keys' % api)


def test_list_v2_keycount_and_name(ctx):
    """ListObjectsV2 reports KeyCount, Name and IsTruncated=false."""
    b = _fill(ctx, ['a', 'b', 'c'])
    root = _list(ctx, b, 'v2')
    eq(root.findtext('KeyCount'), '3', 'KeyCount')
    eq(root.findtext('Name'), b, 'Name')
    eq(s3xml.truncated(root), False, 'IsTruncated')


def test_list_entry_fields(ctx):
    """Each Contents entry has ETag, Size, LastModified and StorageClass."""
    b = ctx.new_bucket()
    ok(ctx.c.put_object(b, 'k', b'12345'))
    for api in APIS:
        c = _list(ctx, b, api).find('Contents')
        true(c is not None, '%s: no Contents' % api)
        eq(c.findtext('Size'), '5', '%s Size' % api)
        eq((c.findtext('ETag') or '').strip('"'), '827ccb0eea8a706c4c34a16891f84e7b', '%s ETag' % api)
        true(bool(c.findtext('LastModified')), '%s: LastModified missing' % api)
        true(bool(c.findtext('StorageClass')), '%s: StorageClass missing' % api)


def test_list_empty_bucket(ctx):
    """An empty bucket lists no keys and is not truncated."""
    b = ctx.new_bucket()
    for api in APIS:
        root = _list(ctx, b, api)
        eq(s3xml.keys(root), [], '%s keys' % api)
        eq(s3xml.truncated(root), False, '%s IsTruncated' % api)


def test_list_prefix(ctx):
    """prefix filters keys."""
    b = _fill(ctx, ['logs/a', 'logs/b', 'logsx', 'other'])
    for api in APIS:
        eq(s3xml.keys(_list(ctx, b, api, {'prefix': 'logs/'})), ['logs/a', 'logs/b'], '%s keys' % api)


def test_list_prefix_no_match(ctx):
    """A prefix matching nothing returns an empty listing."""
    b = _fill(ctx, ['a', 'b'])
    for api in APIS:
        eq(s3xml.keys(_list(ctx, b, api, {'prefix': 'zzz'})), [], '%s keys' % api)


@tags('smoke')
def test_list_delimiter_slash(ctx):
    """delimiter=/ rolls keys up into CommonPrefixes."""
    b = _fill(ctx, ['asdf', 'foo/bar', 'foo/baz/xyzzy', 'quux/thud'])
    for api in APIS:
        root = _list(ctx, b, api, {'delimiter': '/'})
        eq(s3xml.keys(root), ['asdf'], '%s keys' % api)
        eq(s3xml.common_prefixes(root), ['foo/', 'quux/'], '%s CommonPrefixes' % api)
        eq(root.findtext('Delimiter'), '/', '%s Delimiter echo' % api)


def test_list_prefix_and_delimiter(ctx):
    """prefix plus delimiter lists one level below the prefix."""
    b = _fill(ctx, ['asdf', 'foo/bar', 'foo/baz/xyzzy', 'quux/thud'])
    for api in APIS:
        root = _list(ctx, b, api, {'prefix': 'foo/', 'delimiter': '/'})
        eq(s3xml.keys(root), ['foo/bar'], '%s keys' % api)
        eq(s3xml.common_prefixes(root), ['foo/baz/'], '%s CommonPrefixes' % api)


def test_list_delimiter_not_slash(ctx):
    """Any character can be a delimiter (classic bar/baz/cab/foo case)."""
    b = _fill(ctx, ['bar', 'baz', 'cab', 'foo'])
    for api in APIS:
        root = _list(ctx, b, api, {'delimiter': 'a'})
        eq(s3xml.keys(root), ['foo'], '%s keys' % api)
        eq(s3xml.common_prefixes(root), ['ba', 'ca'], '%s CommonPrefixes' % api)


def test_list_delimiter_absent_from_keys(ctx):
    """A delimiter that appears in no key returns every key and no prefixes."""
    b = _fill(ctx, ['a', 'b/c'])
    for api in APIS:
        root = _list(ctx, b, api, {'delimiter': '%'})
        eq(s3xml.keys(root), ['a', 'b/c'], '%s keys' % api)
        eq(s3xml.common_prefixes(root), [], '%s CommonPrefixes' % api)


def test_list_v1_pagination(ctx):
    """ListObjects pages with max-keys and marker and returns every key once."""
    keys = ['k%02d' % i for i in range(7)]
    b = _fill(ctx, keys)
    root = _list(ctx, b, 'v1', {'max-keys': '2'})
    eq(len(s3xml.keys(root)), 2, 'first page size')
    eq(s3xml.truncated(root), True, 'first page IsTruncated')
    got, _, pages = _list_all(ctx, b, 'v1', {}, 2)
    eq(got, keys, 'all keys')
    eq(pages, 4, 'page count')


@tags('smoke')
def test_list_v2_pagination(ctx):
    """ListObjectsV2 pages with continuation tokens and returns every key once."""
    keys = ['k%02d' % i for i in range(7)]
    b = _fill(ctx, keys)
    got, _, pages = _list_all(ctx, b, 'v2', {}, 3)
    eq(got, keys, 'all keys')
    eq(pages, 3, 'page count')


def test_list_paged_common_prefixes(ctx):
    """With max-keys=1 and a delimiter, each common prefix appears exactly once across pages."""
    b = _fill(ctx, ['a/1', 'a/2', 'a/3', 'b/1', 'c', 'd/1/2'])
    for api in APIS:
        keys, prefixes, _ = _list_all(ctx, b, api, {'delimiter': '/'}, 1)
        eq(keys, ['c'], '%s keys' % api)
        eq(prefixes, ['a/', 'b/', 'd/'], '%s CommonPrefixes' % api)


def test_list_v1_marker(ctx):
    """marker starts the listing after the given key; a marker past the end is empty."""
    b = _fill(ctx, ['a', 'b', 'c', 'd'])
    eq(s3xml.keys(_list(ctx, b, 'v1', {'marker': 'b'})), ['c', 'd'], 'after b')
    eq(s3xml.keys(_list(ctx, b, 'v1', {'marker': 'zzz'})), [], 'after last')


def test_list_v2_start_after(ctx):
    """start-after starts the V2 listing after the given key."""
    b = _fill(ctx, ['a', 'b', 'c', 'd'])
    eq(s3xml.keys(_list(ctx, b, 'v2', {'start-after': 'b'})), ['c', 'd'], 'after b')


def test_list_max_keys_zero(ctx):
    """max-keys=0 returns no entries."""
    b = _fill(ctx, ['a', 'b'])
    for api in APIS:
        eq(s3xml.keys(_list(ctx, b, api, {'max-keys': '0'})), [], '%s keys' % api)


def test_list_encoding_type_url(ctx):
    """encoding-type=url returns URL-encoded keys that decode to the originals."""
    keys = ['space key', 'unicode-ü', 'plain']
    b = _fill(ctx, keys)
    for api in APIS:
        root = _list(ctx, b, api, {'encoding-type': 'url'})
        eq((root.findtext('EncodingType') or '').lower(), 'url', '%s EncodingType' % api)
        eq(sorted(unquote_plus(k) for k in s3xml.keys(root)), sorted(keys), '%s decoded keys' % api)


def test_list_utf8_binary_order(ctx):
    """Keys sort by UTF-8 byte order, not locale or case-insensitive order."""
    keys = ['a', 'B', 'b', 'A', '_', '~', '0', 'é', '中', 'Z']
    b = _fill(ctx, keys)
    expected = sorted(keys, key=lambda k: k.encode('utf-8'))
    for api in APIS:
        eq(s3xml.keys(_list(ctx, b, api)), expected, '%s order' % api)


def test_list_v2_fetch_owner(ctx):
    """V2 omits Owner by default and includes it with fetch-owner=true."""
    b = _fill(ctx, ['k'])
    eq(_list(ctx, b, 'v2').find('Contents/Owner'), None, 'Owner without fetch-owner')
    true(_list(ctx, b, 'v2', {'fetch-owner': 'true'}).findtext('Contents/Owner/ID'),
         'Owner/ID missing with fetch-owner=true')


def test_list_missing_bucket(ctx):
    """Listing a missing bucket returns 404 NoSuchBucket."""
    name = ctx.bucket_name()
    error(ctx.c.list_objects(name), 404, 'NoSuchBucket')
    error(ctx.c.list_objects_v2(name), 404, 'NoSuchBucket')
