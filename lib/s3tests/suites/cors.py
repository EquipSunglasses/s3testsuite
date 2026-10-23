"""Bucket CORS configuration and preflight handling."""

from ..assertions import eq, error, ok, status, true
from ..client import xml_doc

TAGS = ('cors',)
ORIGIN = 'https://allowed.example.com'

CORS = xml_doc('CORSConfiguration', (
    '<CORSRule><AllowedOrigin>%s</AllowedOrigin><AllowedMethod>GET</AllowedMethod>'
    '<AllowedMethod>PUT</AllowedMethod><AllowedHeader>*</AllowedHeader>'
    '<ExposeHeader>ETag</ExposeHeader><MaxAgeSeconds>300</MaxAgeSeconds></CORSRule>') % ORIGIN)


def _preflight(ctx, b, origin, method='GET'):
    return ctx.anon.request('OPTIONS', b, 'k', headers={'Origin': origin, 'Access-Control-Request-Method': method})


def test_cors_none_configured(ctx):
    """GET ?cors on a new bucket returns 404 NoSuchCORSConfiguration."""
    error(ctx.c.get_bucket_sub(ctx.new_bucket(), 'cors'), 404, 'NoSuchCORSConfiguration')


def test_cors_roundtrip(ctx):
    """PUT/GET/DELETE ?cors."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'cors', CORS), 'put cors')
    root = ok(ctx.c.get_bucket_sub(b, 'cors')).xml()
    rule = root.find('CORSRule')
    true(rule is not None, 'CORSRule missing')
    eq(rule.findtext('AllowedOrigin'), ORIGIN, 'AllowedOrigin')
    eq(sorted(m.text for m in rule.findall('AllowedMethod')), ['GET', 'PUT'], 'AllowedMethod')
    eq(rule.findtext('MaxAgeSeconds'), '300', 'MaxAgeSeconds')
    status(ctx.c.delete_bucket_sub(b, 'cors'), 204, 'delete cors')
    error(ctx.c.get_bucket_sub(b, 'cors'), 404, 'NoSuchCORSConfiguration')


def test_cors_preflight_allowed(ctx):
    """OPTIONS from an allowed origin returns 200 with Access-Control-Allow-Origin."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'cors', CORS))
    r = status(_preflight(ctx, b, ORIGIN), 200, 'preflight')
    true(r.header('access-control-allow-origin') in (ORIGIN, '*'),
         'Access-Control-Allow-Origin is %r' % r.header('access-control-allow-origin'))
    true('GET' in (r.header('access-control-allow-methods') or ''),
         'Access-Control-Allow-Methods is %r' % r.header('access-control-allow-methods'))


def test_cors_preflight_denied_origin(ctx):
    """OPTIONS from an origin not in the rules returns 403."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'cors', CORS))
    error(_preflight(ctx, b, 'https://evil.example.net'), 403)


def test_cors_preflight_denied_method(ctx):
    """OPTIONS for a method not in the rules returns 403."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'cors', CORS))
    error(_preflight(ctx, b, ORIGIN, 'DELETE'), 403)


def test_cors_preflight_without_config(ctx):
    """OPTIONS on a bucket without CORS configuration returns 403."""
    error(_preflight(ctx, ctx.new_bucket(), ORIGIN), 403)


def test_cors_headers_on_get(ctx):
    """A GET with an allowed Origin header gets Access-Control-Allow-Origin."""
    b = ctx.new_bucket()
    ok(ctx.c.put_bucket_sub(b, 'cors', CORS))
    ok(ctx.c.put_object(b, 'k', b'x'))
    r = ok(ctx.c.get_object(b, 'k', headers={'Origin': ORIGIN}))
    true(r.header('access-control-allow-origin') in (ORIGIN, '*'),
         'Access-Control-Allow-Origin is %r' % r.header('access-control-allow-origin'))
