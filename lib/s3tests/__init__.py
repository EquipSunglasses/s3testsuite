"""s3tests: a dependency-free S3 protocol conformance suite.

Everything here uses the Python standard library only (3.6+), so it runs on
RHEL 7 python3, RHEL 8 platform-python, RHEL 9/10 python3 and macOS python3.
"""

import os

__version__ = '1.4.0'

try:  # written into release tarballs by `make dist`
    from ._build import BUILD as __build__
except ImportError:
    __build__ = 'dev'

LIBDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version_line():
    return 's3tests %s (build %s)' % (__version__, __build__)
