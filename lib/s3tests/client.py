"""Minimal S3 REST client on top of http.client. Standard library only."""

import base64
import collections
import hashlib
import http.client
import os
import random
import threading
import ssl
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from . import __version__, sigv4
from .md5compat import PyMD5

S3_NS = 'http://s3.amazonaws.com/doc/2006-03-01/'

# Process-wide network counters, printed at the end of a run.
NET_STATS = {'requests': 0, 'connections': 0, 'connect_retries': 0, 'stale_retries': 0, 'read_retries': 0,
             'slowdown_retries': 0}
_stats_lock = threading.Lock()


def _count(name):
    with _stats_lock:
        NET_STATS[name] += 1
_DEFAULT = object()
_UNSET = object()


def _md5(data=b''):
    # MD5 is an S3 integrity checksum here, not a security primitive.
    # usedforsecurity=False keeps it working on FIPS-enabled RHEL 8+; on a
    # FIPS host whose Python lacks that flag (possible on RHEL 7) hashlib
    # refuses MD5 and the pure-Python implementation takes over.
    try:
        return hashlib.md5(data, usedforsecurity=False)
    except TypeError:
        pass
    except ValueError:
        return PyMD5(data)
    try:
        return hashlib.md5(data)
    except ValueError:
        return PyMD5(data)


def md5_hex(data):
    return _md5(data).hexdigest()


def md5_b64(data):
    return base64.b64encode(_md5(data).digest()).decode('ascii')


def md5_digest(data):
    return _md5(data).digest()


def strip_namespaces(root):
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith('{'):
            el.tag = el.tag.split('}', 1)[1]
    return root


def xml_doc(root_tag, inner):
    """Wrap an XML fragment string in a namespaced S3 root element."""
    return ('<%s xmlns="%s">%s</%s>' % (root_tag, S3_NS, inner, root_tag)).encode('utf-8')


def x(tag, text):
    return '<%s>%s</%s>' % (tag, xml_escape(str(text)), tag)


class DeadlineExceeded(Exception):
    """A test or cleanup ran past its time budget."""


class Credentials(object):
    def __init__(self, access_key, secret_key, session_token=None, label='main'):
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token or None
        self.label = label

    def __repr__(self):
        return '<Credentials %s %s...>' % (self.label, (self.access_key or '')[:4])


class Response(object):
    def __init__(self, method, url, status, reason, headers, body, elapsed, req_headers, label):
        self.method = method
        self.url = url
        self.status = status
        self.reason = reason
        self.headers = headers
        self.body = body
        self.elapsed = elapsed
        self.req_headers = req_headers
        self.label = label
        self._xml = _UNSET

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.body.decode('utf-8', 'replace')

    def _try_xml(self):
        if self._xml is _UNSET:
            self._xml = None
            if self.body and self.body.lstrip()[:1] == b'<':
                try:
                    self._xml = strip_namespaces(ET.fromstring(self.body))
                except ET.ParseError:
                    self._xml = None
        return self._xml

    def xml(self):
        root = self._try_xml()
        if root is None:
            raise AssertionError('response is not valid XML [%s]: %r' % (self.describe(), self.body[:300]))
        return root

    @property
    def error_code(self):
        root = self._try_xml()
        if root is not None and root.tag == 'Error':
            return root.findtext('Code')
        return None

    @property
    def error_message(self):
        root = self._try_xml()
        if root is not None and root.tag == 'Error':
            return root.findtext('Message')
        return None

    def describe(self):
        code = self.error_code
        return '%s %s -> %d%s' % (self.method, self.url, self.status, (' ' + code) if code else '')

    def trace(self, full=False):
        lines = ['[%s] %s (%.3fs)' % (self.label, self.describe(), self.elapsed)]
        if full:
            for k, v in sorted(self.req_headers.items()):
                if k.lower() == 'authorization':
                    v = v.split('Signature=')[0] + 'Signature=<redacted>'
                lines.append('    > %s: %s' % (k, v))
            for k, v in sorted(self.headers.items()):
                lines.append('    < %s: %s' % (k, v))
            if self.body and not self.ok:
                lines.append('    < ' + self.text[:800].replace('\n', ' '))
        return '\n'.join(lines)

    def __repr__(self):
        return '<Response %s>' % self.describe()


def _norm_query(query):
    if not query:
        return []
    if isinstance(query, dict):
        query = list(query.items())
    return [(str(k), None if v is None else str(v)) for k, v in query]


def _url_query(params):
    out = []
    for k, v in params:
        out.append(sigv4.uri_encode(k) if v is None else sigv4.uri_encode(k) + '=' + sigv4.uri_encode(v))
    return '&'.join(out)


def _has(headers, name):
    name = name.lower()
    return any(k.lower() == name for k in headers)


# System CA bundles tried when Python's own trust store is empty (python.org
# builds on macOS ship without one). An explicit ca_bundle always wins.
SYSTEM_CA_BUNDLES = (
    '/etc/pki/tls/certs/ca-bundle.crt',           # RHEL, Fedora
    '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
    '/etc/ssl/certs/ca-certificates.crt',         # Debian, Ubuntu
    '/etc/ssl/cert.pem',                          # macOS, Alpine
    '/usr/local/etc/openssl@3/cert.pem',          # Homebrew (Intel)
    '/opt/homebrew/etc/openssl@3/cert.pem',       # Homebrew (Apple Silicon)
)


def _dns_compatible(bucket):
    if not (3 <= len(bucket) <= 63) or bucket.lower() != bucket or '..' in bucket:
        return False
    return all(c.isalnum() or c in '-.' for c in bucket) and bucket[0].isalnum() and bucket[-1].isalnum()


class S3Client(object):
    def __init__(self, endpoint, region, creds, verify_ssl=True, ca_bundle=None,
                 addressing='path', timeout=60.0, service='s3', label='main',
                 connect_timeout=10.0, retries=3, keepalive=True):
        u = urlsplit(endpoint)
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise ValueError('endpoint must look like https://host[:port], got %r' % endpoint)
        self.endpoint = endpoint
        self.scheme = u.scheme
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == 'https' else 80)
        host_disp = '[%s]' % self.host if ':' in self.host else self.host
        default_port = 443 if u.scheme == 'https' else 80
        self.netloc = host_disp if self.port == default_port else '%s:%d' % (host_disp, self.port)
        self.region = region
        self.creds = creds
        self.verify_ssl = verify_ssl
        self.ca_bundle = ca_bundle or None
        self.addressing = addressing
        self.timeout = timeout
        self.service = service
        self.label = label
        self.history = collections.deque(maxlen=40)
        self._ssl_ctx = None
        self.connect_timeout = connect_timeout
        self.retries = retries
        self.keepalive = keepalive
        self._conns = {}
        self._conn_lock = threading.Lock()
        self.deadline = None  # epoch seconds; requests after it raise DeadlineExceeded
        self.abort_event = None  # threading.Event; once set, requests raise RunAborted
        self.bucket_tracker = None  # TestContext; records CreateBucket calls for cleanup
        self.ca_bundle_used = ca_bundle or None

    @property
    def secure(self):
        return self.scheme == 'https'

    # transport ---------------------------------------------------------

    def _ssl_context(self):
        if self._ssl_ctx is None:
            ctx = ssl.create_default_context(cafile=self.ca_bundle)
            if self.verify_ssl and not self.ca_bundle and ctx.cert_store_stats().get('x509_ca', 0) == 0:
                for path in SYSTEM_CA_BUNDLES:
                    if os.path.isfile(path):
                        try:
                            ctx.load_verify_locations(cafile=path)
                            self.ca_bundle_used = path
                            break
                        except (ssl.SSLError, OSError):
                            continue
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx
        return self._ssl_ctx

    def _connection(self, host, port, scheme):
        key = (scheme, host, port)
        conn = self._conns.get(key) if self.keepalive else None
        if conn is None:
            if scheme == 'https':
                conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout,
                                                   context=self._ssl_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
            if self.keepalive:
                self._conns[key] = conn
        return conn

    def _drop(self, scheme, host, port, conn):
        try:
            conn.close()
        finally:
            if self._conns.get((scheme, host, port)) is conn:
                del self._conns[(scheme, host, port)]

    def close(self):
        """Close kept-alive connections."""
        for conn in list(self._conns.values()):
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    def _sleep(self, seconds):
        """Back off, but never past the deadline or an abort."""
        if self.deadline is not None:
            seconds = min(seconds, max(0.0, self.deadline - time.time()))
        end = time.time() + seconds
        while time.time() < end:
            if self.abort_event is not None and self.abort_event.is_set():
                return
            time.sleep(min(0.2, end - time.time()))

    def _target(self, bucket, key):
        """Return (connect_host, host_header, canonical_uri)."""
        if bucket is None:
            return self.host, self.netloc, '/'
        if self.addressing == 'virtual' and _dns_compatible(bucket):
            uri = '/' + (sigv4.uri_encode(key, keep_slash=True) if key is not None else '')
            return bucket + '.' + self.host, bucket + '.' + self.netloc, uri
        uri = '/' + sigv4.uri_encode(bucket)
        if key is not None:
            uri += '/' + sigv4.uri_encode(key, keep_slash=True)
        return self.host, self.netloc, uri

    def _send(self, method, scheme, host, port, url, headers, body):
        if body is not None and len(body) == 0 and method in ('GET', 'HEAD', 'DELETE', 'OPTIONS'):
            body = None
        if not _has(headers, 'user-agent'):
            headers['User-Agent'] = 's3tests/%s' % __version__
        t0 = time.time()
        raw, data = self._exchange(method, scheme, host, port, url, headers, body)
        hdrs = {}
        for k, v in raw.getheaders():
            lk = k.lower()
            hdrs[lk] = hdrs[lk] + ', ' + v if lk in hdrs else v
        resp = Response(method, url, raw.status, raw.reason, hdrs, data, time.time() - t0, headers, self.label)
        resp.attempts = None
        resp.started = t0
        self.history.append(resp)
        return resp

    def _exchange(self, method, scheme, host, port, url, headers, body):
        """One HTTP exchange with keep-alive and safe retries.

        Retried, for any method, because the request never reached the server:
          * failures to connect (DNS, TCP connect timeout or refusal, TLS handshake)
          * a kept-alive connection the server had already closed (stale reuse)
        Retried for GET/HEAD/OPTIONS only: a connection reset mid-exchange (reads are
        safe to repeat; writes are not, e.g. a repeated DELETE adds a delete marker).
        Retried with backoff because S3 asks for it: 503 SlowDown / ServiceUnavailable.
        Nothing else is retried, so real server errors are reported as they happen.
        """
        attempt = 0
        while True:
            attempt += 1
            if self.abort_event is not None and self.abort_event.is_set():
                from .safety import RunAborted
                raise RunAborted('run aborted before %s %s' % (method, url[:120]))
            if self.deadline is not None and time.time() > self.deadline:
                raise DeadlineExceeded('time budget exhausted before %s %s (see [run] test_timeout)'
                                       % (method, url[:120]))
            conn = self._connection(host, port, scheme)
            reused = conn.sock is not None
            if not reused:
                try:
                    conn.connect()
                    conn.sock.settimeout(self.timeout)
                    _count('connections')
                except (OSError, http.client.HTTPException):
                    self._drop(scheme, host, port, conn)
                    if attempt > self.retries:
                        raise
                    _count('connect_retries')
                    self._sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) + random.random() * 0.25)
                    continue
            try:
                conn.request(method, url, body=body, headers=headers)
                raw = conn.getresponse()
                data = raw.read()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                    ConnectionAbortedError):
                self._drop(scheme, host, port, conn)
                if reused and attempt <= self.retries:
                    _count('stale_retries')   # the server closed an idle connection
                    continue
                if method in ('GET', 'HEAD', 'OPTIONS') and attempt <= self.retries:
                    _count('read_retries')    # connection dropped mid-read; reads are safe to repeat
                    self._sleep(min(4.0, 0.5 * 2 ** (attempt - 1)))
                    continue
                raise
            except Exception:
                self._drop(scheme, host, port, conn)
                raise
            if raw.will_close or not self.keepalive:
                self._drop(scheme, host, port, conn)
            _count('requests')
            if raw.status == 503 and attempt <= self.retries:  # SlowDown / ServiceUnavailable: not processed
                _count('slowdown_retries')
                try:
                    wait = float(raw.getheader('Retry-After') or 0)
                except ValueError:
                    wait = 0
                self._sleep(min(10.0, max(wait, 0.5 * 2 ** (attempt - 1))) + random.random() * 0.25)
                continue
            return raw, data

    def request(self, method, bucket=None, key=None, query=None, headers=None, body=None,
                creds=_DEFAULT, payload_hash=None, when=None):
        """Send a request. creds=None sends it anonymously (unsigned)."""
        creds = self.creds if creds is _DEFAULT else creds
        params = _norm_query(query)
        conn_host, host_header, uri = self._target(bucket, key)
        if isinstance(body, str):
            body = body.encode('utf-8')
        hdrs = dict((k, str(v)) for k, v in (headers or {}).items())
        if not _has(hdrs, 'host'):
            hdrs['Host'] = host_header
        if creds is not None:
            hdrs['x-amz-date'] = sigv4.amz_dates(when)[0]
            if not _has(hdrs, 'x-amz-content-sha256'):
                hdrs['x-amz-content-sha256'] = payload_hash or sigv4.sha256_hex(body or b'')
            if creds.session_token and not _has(hdrs, 'x-amz-security-token'):
                hdrs['x-amz-security-token'] = creds.session_token
            content_hash = [v for k, v in hdrs.items() if k.lower() == 'x-amz-content-sha256'][0]
            hdrs['Authorization'] = sigv4.authorization_header(
                method, uri, params, hdrs, content_hash,
                creds.access_key, creds.secret_key, self.region, self.service)
        url = uri + ('?' + _url_query(params) if params else '')
        return self._send(method, self.scheme, conn_host, self.port, url, hdrs, body)

    def presign(self, method, bucket=None, key=None, expires=300, query=None, when=None, creds=_DEFAULT):
        creds = self.creds if creds is _DEFAULT else creds
        conn_host, host_header, uri = self._target(bucket, key)
        q = sigv4.presign_params(method, uri, host_header, _norm_query(query), creds.access_key,
                                 creds.secret_key, self.region, expires, when, self.service,
                                 creds.session_token)
        return '%s://%s%s?%s' % (self.scheme, host_header, uri, _url_query(q))

    def fetch(self, method, url, body=None, headers=None):
        """Send an unsigned request to an absolute URL (for presigned URLs)."""
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == 'https' else 80)
        path = u.path + ('?' + u.query if u.query else '')
        hdrs = dict(headers or {})
        hdrs.setdefault('Host', u.netloc)
        if isinstance(body, str):
            body = body.encode('utf-8')
        return self._send(method, u.scheme, u.hostname, port, path, hdrs, body)

    # buckets -------------------------------------------------------------

    def list_buckets(self, **kw):
        return self.request('GET', **kw)

    def create_bucket(self, bucket, headers=None, location=None, **kw):
        """Create a bucket. When a tracker is attached (every test client has one), the name
        is recorded for cleanup *before* the request is sent, so a bucket whose create
        response is lost (timeout, reset) or whose test fails right after is still removed."""
        body = None
        if location:
            body = xml_doc('CreateBucketConfiguration', x('LocationConstraint', location))
        tracker = self.bucket_tracker
        token = tracker.pending(bucket, self) if tracker else None
        r = self.request('PUT', bucket, headers=headers, body=body, **kw)  # an exception keeps it pending
        if tracker:
            tracker.settle(token, bucket, self, r)
        return r

    def head_bucket(self, bucket, **kw):
        return self.request('HEAD', bucket, **kw)

    def delete_bucket(self, bucket, **kw):
        return self.request('DELETE', bucket, **kw)

    def get_bucket_sub(self, bucket, sub, query=None, **kw):
        return self.request('GET', bucket, query=[(sub, None)] + _norm_query(query), **kw)

    def put_bucket_sub(self, bucket, sub, body, headers=None, md5=True, **kw):
        if isinstance(body, str):
            body = body.encode('utf-8')
        hdrs = dict(headers or {})
        if md5:
            hdrs.setdefault('Content-MD5', md5_b64(body))
        return self.request('PUT', bucket, query=[(sub, None)], headers=hdrs, body=body, **kw)

    def delete_bucket_sub(self, bucket, sub, **kw):
        return self.request('DELETE', bucket, query=[(sub, None)], **kw)

    def set_versioning(self, bucket, status, **kw):
        return self.put_bucket_sub(bucket, 'versioning', xml_doc('VersioningConfiguration', x('Status', status)), **kw)

    def list_objects(self, bucket, params=None, **kw):
        return self.request('GET', bucket, query=_norm_query(params), **kw)

    def list_objects_v2(self, bucket, params=None, **kw):
        return self.request('GET', bucket, query=[('list-type', '2')] + _norm_query(params), **kw)

    def list_object_versions(self, bucket, params=None, **kw):
        return self.request('GET', bucket, query=[('versions', None)] + _norm_query(params), **kw)

    # objects -------------------------------------------------------------

    @staticmethod
    def _vq(version_id, query=None):
        q = _norm_query(query)
        if version_id is not None:
            q.append(('versionId', version_id))
        return q

    def put_object(self, bucket, key, body=b'', headers=None, query=None, **kw):
        return self.request('PUT', bucket, key, query=query, headers=headers, body=body, **kw)

    def get_object(self, bucket, key, headers=None, query=None, version_id=None, **kw):
        return self.request('GET', bucket, key, query=self._vq(version_id, query), headers=headers, **kw)

    def head_object(self, bucket, key, headers=None, query=None, version_id=None, **kw):
        return self.request('HEAD', bucket, key, query=self._vq(version_id, query), headers=headers, **kw)

    def delete_object(self, bucket, key, version_id=None, headers=None, **kw):
        return self.request('DELETE', bucket, key, query=self._vq(version_id), headers=headers, **kw)

    def copy_source(self, bucket, key, version_id=None):
        src = '/%s/%s' % (sigv4.uri_encode(bucket), sigv4.uri_encode(key, keep_slash=True))
        if version_id is not None:
            src += '?versionId=' + sigv4.uri_encode(version_id)
        return src

    def copy_object(self, bucket, key, src_bucket, src_key, headers=None, src_version=None, **kw):
        hdrs = dict(headers or {})
        hdrs['x-amz-copy-source'] = self.copy_source(src_bucket, src_key, src_version)
        return self.request('PUT', bucket, key, headers=hdrs, **kw)

    def get_object_sub(self, bucket, key, sub, version_id=None, **kw):
        return self.request('GET', bucket, key, query=[(sub, None)] + self._vq(version_id), **kw)

    def put_object_sub(self, bucket, key, sub, body, version_id=None, headers=None, md5=True, **kw):
        if isinstance(body, str):
            body = body.encode('utf-8')
        hdrs = dict(headers or {})
        if md5:
            hdrs.setdefault('Content-MD5', md5_b64(body))
        return self.request('PUT', bucket, key, query=[(sub, None)] + self._vq(version_id),
                            headers=hdrs, body=body, **kw)

    def delete_object_sub(self, bucket, key, sub, version_id=None, **kw):
        return self.request('DELETE', bucket, key, query=[(sub, None)] + self._vq(version_id), **kw)

    def delete_objects(self, bucket, objects, quiet=False, headers=None, **kw):
        """objects: iterable of key strings or (key, version_id) tuples."""
        parts = [x('Quiet', 'true')] if quiet else []
        for obj in objects:
            key, vid = (obj, None) if isinstance(obj, str) else obj
            parts.append('<Object>%s%s</Object>' % (x('Key', key), x('VersionId', vid) if vid else ''))
        body = xml_doc('Delete', ''.join(parts))
        hdrs = dict(headers or {})
        hdrs.setdefault('Content-MD5', md5_b64(body))
        return self.request('POST', bucket, query=[('delete', None)], headers=hdrs, body=body, **kw)

    # multipart -----------------------------------------------------------

    def create_multipart_upload(self, bucket, key, headers=None, **kw):
        return self.request('POST', bucket, key, query=[('uploads', None)], headers=headers, **kw)

    def upload_part(self, bucket, key, upload_id, part_number, body, headers=None, **kw):
        q = [('partNumber', str(part_number)), ('uploadId', upload_id)]
        return self.request('PUT', bucket, key, query=q, headers=headers, body=body, **kw)

    def upload_part_copy(self, bucket, key, upload_id, part_number, src_bucket, src_key,
                         byte_range=None, headers=None, **kw):
        hdrs = dict(headers or {})
        hdrs['x-amz-copy-source'] = self.copy_source(src_bucket, src_key)
        if byte_range:
            hdrs['x-amz-copy-source-range'] = byte_range
        q = [('partNumber', str(part_number)), ('uploadId', upload_id)]
        return self.request('PUT', bucket, key, query=q, headers=hdrs, **kw)

    def complete_multipart_upload(self, bucket, key, upload_id, parts, headers=None, **kw):
        inner = ''.join('<Part>%s%s</Part>' % (x('PartNumber', n), x('ETag', etag)) for n, etag in parts)
        body = xml_doc('CompleteMultipartUpload', inner)
        return self.request('POST', bucket, key, query=[('uploadId', upload_id)], headers=headers,
                            body=body, **kw)

    def abort_multipart_upload(self, bucket, key, upload_id, **kw):
        return self.request('DELETE', bucket, key, query=[('uploadId', upload_id)], **kw)

    def list_parts(self, bucket, key, upload_id, params=None, **kw):
        return self.request('GET', bucket, key, query=[('uploadId', upload_id)] + _norm_query(params), **kw)

    def list_multipart_uploads(self, bucket, params=None, **kw):
        return self.request('GET', bucket, query=[('uploads', None)] + _norm_query(params), **kw)
