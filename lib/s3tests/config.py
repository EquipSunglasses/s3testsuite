"""Configuration loading: INI file, environment variables, CLI overrides."""

import configparser
import os

from .client import Credentials, S3Client


class ConfigError(Exception):
    pass


def _bool(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _list(value):
    return [v.strip() for v in str(value or '').replace('\n', ',').split(',') if v.strip()]


def default_paths():
    libroot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = []
    if os.environ.get('S3TESTS_CONF'):
        paths.append(os.environ['S3TESTS_CONF'])
    paths += [
        os.path.join(os.getcwd(), 's3tests.conf'),
        os.path.expanduser('~/.s3tests.conf'),
        os.path.normpath(os.path.join(libroot, '..', '..', 'etc', 's3tests', 's3tests.conf')),
        '/etc/s3tests/s3tests.conf',
        '/usr/local/etc/s3tests/s3tests.conf',
        '/opt/homebrew/etc/s3tests/s3tests.conf',
    ]
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def unreadable_message(path):
    try:
        import pwd
        st = os.stat(path)
        owner = pwd.getpwuid(st.st_uid).pw_name
        mode = '%o' % (st.st_mode & 0o777)
    except (ImportError, KeyError, OSError):
        owner, mode = '?', '?'
    return ('cannot read %s: permission denied (owner %s, mode %s).\n'
            'It holds credentials, so a root install makes it readable by root only. Either:\n'
            '  sudo s3tests ...                                   run as root\n'
            '  sudo install -m 600 -o $USER %s ~/.s3tests.conf   your own copy (used first)\n'
            '  sh s3tests-VERSION.run --user                      a separate install for your user'
            % (path, owner, mode, path))


ENV_MAP = {
    'S3TESTS_ENDPOINT': ('target', 'endpoint'),
    'S3TESTS_LABEL': ('target', 'label'),
    'S3TESTS_REGION': ('target', 'region'),
    'S3TESTS_ADDRESSING': ('target', 'addressing_style'),
    'S3TESTS_VERIFY_SSL': ('target', 'verify_ssl'),
    'S3TESTS_CA_BUNDLE': ('target', 'ca_bundle'),
    'S3TESTS_ACCESS_KEY': ('main', 'access_key'),
    'S3TESTS_SECRET_KEY': ('main', 'secret_key'),
    'S3TESTS_SESSION_TOKEN': ('main', 'session_token'),
    'S3TESTS_ALT_ACCESS_KEY': ('alt', 'access_key'),
    'S3TESTS_ALT_SECRET_KEY': ('alt', 'secret_key'),
}


class Config(object):
    def __init__(self):
        self.source = None
        self.endpoint = None
        self.region = 'us-east-1'
        self.service = 's3'
        self.addressing = 'path'
        self.verify_ssl = True
        self.ca_bundle = None
        self.timeout = 60.0
        self.connect_timeout = 10.0
        self.retries = 3
        self.keepalive = True
        self.bucket_prefix = 's3tests-'
        self.label = None
        self.location_constraint = None
        self.allow_insecure_sse = False
        self.main = None
        self.main_user_id = None
        self.alt = None
        self.alt_user_id = None
        self.alt_principal_arn = None
        self.jobs = 1
        self.include_tags = []
        self.exclude_tags = []
        self.large_object_mb = 16
        self.part_size_mb = 5
        self.lifecycle_wait_seconds = 0
        self.test_timeout = 300
        self.cleanup_timeout = 180
        self.tier = 'full'
        self.hang_grace = 60
        self.propagation_timeout = 20
        self.bucket_ready_timeout = 30
        self.bucket_ready_checks = 3
        self.final_cleanup_window = 180
        self.sweep_stale_buckets = True
        self.stale_bucket_minutes = 60
        self.breaker = 5
        self.outage_wait = 120
        self.rerun_connection_errors = True
        self.max_runtime = 0
        self.state_dir = None
        self.recover = True
        self.expected_failures = {}
        self.warnings = []
        self.shadowed = []     # config files found after the one used (ignored)
        self.searched = []     # paths searched when no config file was found

    @property
    def secure(self):
        return bool(self.endpoint) and self.endpoint.lower().startswith('https://')

    def make_client(self, creds, label='main'):
        return S3Client(self.endpoint, self.region, creds, verify_ssl=self.verify_ssl,
                        ca_bundle=self.ca_bundle, addressing=self.addressing,
                        timeout=self.timeout, service=self.service, label=label,
                        connect_timeout=self.connect_timeout, retries=self.retries, keepalive=self.keepalive)

    def where(self):
        """Which config file was used, and which were found but ignored."""
        if self.source:
            out = ' in %s' % self.source
            if self.shadowed:
                out += ('\nalso found, but not used (only the first file found is read): %s'
                        '\nedit %s, remove it, or choose a file with -c or S3TESTS_CONF'
                        % (', '.join(self.shadowed), self.source))
            return out
        return ('\nno config file found; searched: %s' % ', '.join(self.searched)) if self.searched else ''

    def validate(self):
        if not self.endpoint:
            raise ConfigError('no endpoint configured' + self.where() +
                              '\nset [target] endpoint, S3TESTS_ENDPOINT or --endpoint')
        if not self.endpoint.lower().startswith(('http://', 'https://')):
            raise ConfigError('endpoint must start with http:// or https://: %r' % self.endpoint)
        if self.main is None:
            raise ConfigError('no credentials configured' + self.where() +
                              '\nset [main] access_key and secret_key, or S3TESTS_ACCESS_KEY/S3TESTS_SECRET_KEY')
        if self.addressing not in ('path', 'virtual'):
            raise ConfigError('addressing_style must be path or virtual')
        if self.tier not in ('smoke', 'standard', 'full'):
            raise ConfigError('tier must be smoke, standard or full')
        if len(self.bucket_prefix) < 3 or not self.bucket_prefix[0].isalnum():
            raise ConfigError('bucket_prefix must be at least 3 characters and start with a letter or digit; '
                              'it is what --cleanup uses to find test buckets')

    def describe(self):
        lines = [
            'config file : %s%s' % (self.source or '(none)',
                                    ('  (ignored: %s)' % ', '.join(self.shadowed)) if self.shadowed else ''),
            'label       : %s' % self.label,
            'endpoint    : %s' % self.endpoint,
            'region      : %s' % self.region,
            'addressing  : %s' % self.addressing,
            'verify_ssl  : %s%s' % (self.verify_ssl, (' (ca_bundle=%s)' % self.ca_bundle) if self.ca_bundle else
                                    ' (system trust store, fallback: first existing system CA bundle)'),
            'main user   : %s' % (self.main.access_key if self.main else '(none)'),
            'alt user    : %s' % (self.alt.access_key if self.alt else '(none, cross-user tests skip)'),
            'tier        : %s' % self.tier,
            'jobs        : %d' % self.jobs,
            'timeouts    : %ds per test, %ds cleanup, %ds connect, %ds read' % (
                self.test_timeout, self.cleanup_timeout, self.connect_timeout, self.timeout),
            'network     : keep-alive %s, %d retries for connect failures and 503' % (
                'on' if self.keepalive else 'off', self.retries),
            'bucket pfx  : %s' % self.bucket_prefix,
            'escape plan : hung after +%ds, pause after %d connection errors (wait %ds), max runtime %s, '
            'auto recover %s' % (self.hang_grace, self.breaker, self.outage_wait,
                                 ('%d min' % self.max_runtime) if self.max_runtime else 'none',
                                 'on' if self.recover else 'off'),
            'exclude_tags: %s' % (', '.join(self.exclude_tags) or '(none)'),
        ]
        return '\n'.join(lines)


def load(path=None, overrides=None):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cfg = Config()
    if path:
        if not os.path.isfile(path):
            raise ConfigError('config file not found: %s' % path)
        candidates = [path]
    else:
        candidates = [p for p in default_paths() if os.path.isfile(p)]
    if candidates:
        src = candidates[0]
        try:
            # read explicitly: ConfigParser.read() silently skips files it cannot open
            with open(src) as f:
                cp.read_file(f, src)
        except PermissionError:
            raise ConfigError(unreadable_message(src))
        except (configparser.Error, UnicodeDecodeError) as e:
            raise ConfigError('cannot parse %s: %s' % (src, e))
        cfg.source = src
        cfg.shadowed = candidates[1:]
    else:
        cfg.searched = default_paths()

    for env, (sec, opt) in ENV_MAP.items():
        if os.environ.get(env):
            if not cp.has_section(sec):
                cp.add_section(sec)
            cp.set(sec, opt, os.environ[env])

    def get(sec, opt, default=None):
        if cp.has_option(sec, opt):
            v = cp.get(sec, opt).strip()
            return v if v != '' else default
        return default

    try:
        cfg.endpoint = get('target', 'endpoint')
        cfg.region = get('target', 'region', cfg.region)
        cfg.service = get('target', 'service', cfg.service)
        cfg.addressing = get('target', 'addressing_style', cfg.addressing).lower()
        cfg.verify_ssl = _bool(get('target', 'verify_ssl', 'true'))
        cfg.ca_bundle = get('target', 'ca_bundle')
        cfg.timeout = float(get('target', 'timeout', cfg.timeout))
        cfg.connect_timeout = float(get('target', 'connect_timeout', cfg.connect_timeout))
        cfg.retries = int(get('target', 'retries', cfg.retries))
        cfg.keepalive = _bool(get('target', 'keepalive', 'true'))
        cfg.bucket_prefix = get('target', 'bucket_prefix', cfg.bucket_prefix).lower()
        cfg.label = get('target', 'label')
        cfg.location_constraint = get('target', 'location_constraint')
        cfg.allow_insecure_sse = _bool(get('target', 'allow_insecure_sse', 'false'))

        ak, sk = get('main', 'access_key'), get('main', 'secret_key')
        if ak and sk:
            cfg.main = Credentials(ak, sk, get('main', 'session_token'), 'main')
        cfg.main_user_id = get('main', 'user_id')
        ak, sk = get('alt', 'access_key'), get('alt', 'secret_key')
        if ak and sk:
            cfg.alt = Credentials(ak, sk, get('alt', 'session_token'), 'alt')
        cfg.alt_user_id = get('alt', 'user_id')
        cfg.alt_principal_arn = get('alt', 'principal_arn')

        cfg.jobs = int(get('run', 'jobs', cfg.jobs))
        cfg.include_tags = _list(get('run', 'include_tags', ''))
        cfg.exclude_tags = _list(get('run', 'exclude_tags', ''))
        cfg.large_object_mb = int(get('run', 'large_object_mb', cfg.large_object_mb))
        cfg.part_size_mb = int(get('run', 'part_size_mb', cfg.part_size_mb))
        cfg.lifecycle_wait_seconds = int(get('run', 'lifecycle_wait_seconds', 0))
        cfg.test_timeout = int(get('run', 'test_timeout', cfg.test_timeout))
        cfg.cleanup_timeout = int(get('run', 'cleanup_timeout', cfg.cleanup_timeout))
        cfg.tier = get('run', 'tier', cfg.tier).lower()
        cfg.hang_grace = int(get('run', 'hang_grace', cfg.hang_grace))
        cfg.propagation_timeout = int(get('run', 'propagation_timeout', cfg.propagation_timeout))
        cfg.bucket_ready_timeout = int(get('run', 'bucket_ready_timeout', cfg.bucket_ready_timeout))
        cfg.bucket_ready_checks = max(1, int(get('run', 'bucket_ready_checks', cfg.bucket_ready_checks)))
        cfg.final_cleanup_window = int(get('run', 'final_cleanup_window', cfg.final_cleanup_window))
        cfg.sweep_stale_buckets = _bool(get('run', 'sweep_stale_buckets', 'true'))
        cfg.stale_bucket_minutes = int(get('run', 'stale_bucket_minutes', cfg.stale_bucket_minutes))
        cfg.breaker = int(get('run', 'breaker', cfg.breaker))
        cfg.outage_wait = int(get('run', 'outage_wait', cfg.outage_wait))
        cfg.rerun_connection_errors = _bool(get('run', 'rerun_connection_errors', 'true'))
        cfg.max_runtime = int(get('run', 'max_runtime', cfg.max_runtime))
        cfg.state_dir = get('run', 'state_dir')
        cfg.recover = _bool(get('run', 'auto_recover', 'true'))
    except ValueError as e:
        raise ConfigError('invalid value in configuration: %s' % e)

    if cp.has_section('expected_failures'):
        for opt in cp.options('expected_failures'):
            cfg.expected_failures[opt] = cp.get('expected_failures', opt).strip() or 'expected failure'

    for k, v in (overrides or {}).items():
        if v is not None:
            setattr(cfg, k, v)
    if cfg.endpoint:
        cfg.endpoint = cfg.endpoint.rstrip('/')
        if not cfg.label:
            from urllib.parse import urlsplit
            cfg.label = urlsplit(cfg.endpoint).hostname or 'unnamed'
    if cfg.ca_bundle:
        cfg.ca_bundle = os.path.expanduser(cfg.ca_bundle)
        if not os.path.isfile(cfg.ca_bundle):
            cfg.warnings.append('ca_bundle %s does not exist; using the system trust store instead '
                                '(RHEL: /etc/pki/tls/certs/ca-bundle.crt, macOS: /etc/ssl/cert.pem)' % cfg.ca_bundle)
            cfg.ca_bundle = None
    return cfg
