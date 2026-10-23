#!/usr/bin/env bash
# Install s3tests. Run by the self-extracting installer (s3tests-VERSION.run),
# which passes its arguments here. Works with bash 3.2 (macOS) and later.
#
#   --user            ~/.local/bin/s3tests, config ~/.s3tests.conf (no root needed)
#   (no option)       /usr/local/bin/s3tests, config /usr/local/etc/s3tests/s3tests.conf (root)
#   --prefix DIR      DIR/bin/s3tests, config DIR/etc/s3tests/s3tests.conf
#   --no-config       do not create a config file
#
# Needs only bash, coreutils, sed and find, plus Python 3.6+ at run time.

set -euo pipefail

PREFIX=/usr/local
USER_MODE=0
CREATE_CONFIG=1

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --user) USER_MODE=1; PREFIX="$HOME/.local"; shift ;;
        --no-config) CREATE_CONFIG=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

SRC=$(cd "$(dirname "$0")" && pwd -P)
LIBDIR="$PREFIX/share/s3tests"
BINDIR="$PREFIX/bin"
ETCDIR="$PREFIX/etc/s3tests"
DOCDIR="$PREFIX/share/doc/s3tests"

if ! { mkdir -p "$PREFIX" 2>/dev/null && [ -w "$PREFIX" ]; }; then
    echo "install: cannot write to $PREFIX; run as root, or use --user or --prefix DIR" >&2
    exit 1
fi

PY=""
for cand in python3 /usr/libexec/platform-python /usr/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' >/dev/null 2>&1; then
        PY=$(command -v "$cand"); break
    fi
done
if [ -z "$PY" ] && [ ! -f /opt/rh/rh-python38/enable ] && [ ! -f /opt/rh/rh-python36/enable ]; then
    echo "warning: no Python 3.6+ found. s3tests needs python3 (RHEL 7.7+ base repo, RHEL 9/10, macOS)," >&2
    echo "         platform-python (built into RHEL 8) or rh-python36/38 (older RHEL 7)." >&2
fi

version_of() {  # version and build of the library in $1
    local v
    v=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$1/s3tests/__init__.py")
    if [ -f "$1/s3tests/_build.py" ]; then
        v="$v (build $(sed -n "s/^BUILD = '\(.*\)'/\1/p" "$1/s3tests/_build.py"))"
    fi
    echo "$v"
}
PREVIOUS=""
[ -f "$LIBDIR/s3tests/__init__.py" ] && PREVIOUS=$(version_of "$LIBDIR")
NEW=$(version_of "$SRC/lib")

# Library: replaced wholesale so modules removed in a newer version do not linger.
rm -rf "$LIBDIR/s3tests"
(cd "$SRC/lib" && find s3tests -name '*.py' -type f) | sort | while read -r f; do
    mkdir -p "$LIBDIR/$(dirname "$f")"
    install -m 0644 "$SRC/lib/$f" "$LIBDIR/$f"
done
install -m 0755 "$SRC/uninstall.sh" "$LIBDIR/uninstall.sh"

mkdir -p "$BINDIR"
sed -e "s|@LIBDIR@|$LIBDIR|g" "$SRC/bin/s3tests" > "$BINDIR/s3tests"
chmod 0755 "$BINDIR/s3tests"

mkdir -p "$ETCDIR" "$DOCDIR/examples"
install -m 0644 "$SRC/etc/s3tests.conf.sample" "$ETCDIR/s3tests.conf.sample"
install -m 0644 "$SRC/README.md" "$SRC/LICENSE" "$SRC/COVERAGE.html" "$DOCDIR/"
install -m 0755 "$SRC"/examples/*.sh "$DOCDIR/examples/"

# A config to fill in, with ca_bundle pointing at this host's system CA bundle.
if [ "$CREATE_CONFIG" = 1 ]; then
    if [ "$USER_MODE" = 1 ]; then CONF="$HOME/.s3tests.conf"; else CONF="$ETCDIR/s3tests.conf"; fi
    if [ ! -e "$CONF" ]; then
        CA=/etc/ssl/cert.pem
        for c in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem /etc/ssl/certs/ca-certificates.crt; do
            if [ -f "$c" ]; then CA="$c"; break; fi
        done
        (umask 077; sed -e "s|^ca_bundle = /etc/ssl/cert.pem\$|ca_bundle = $CA|" \
            "$SRC/etc/s3tests.conf.sample" > "$CONF")
        echo "created $CONF (mode 600, ca_bundle = $CA); fill in endpoint, access_key, secret_key"
        if [ "$USER_MODE" = 0 ] && [ "$(id -u)" = 0 ]; then
            echo "  note: only root can read it. To run s3tests as yourself, either use sudo or copy it:"
            echo "        sudo install -m 600 -o \$USER $CONF ~/.s3tests.conf"
        fi
    else
        echo "kept existing $CONF"
    fi
fi

if [ -n "$PREVIOUS" ]; then
    echo "upgraded s3tests $PREVIOUS -> $NEW in $PREFIX"
else
    echo "installed s3tests $NEW to $PREFIX"
fi
echo "  check:      $BINDIR/s3tests --selftest"
echo "  uninstall:  $LIBDIR/uninstall.sh --prefix $PREFIX"

# Another s3tests earlier on PATH would run instead of this one.
FIRST=$(command -v s3tests 2>/dev/null || true)
if [ -n "$FIRST" ] && [ "$FIRST" != "$BINDIR/s3tests" ]; then
    echo "  WARNING: 's3tests' on your PATH is $FIRST, not $BINDIR/s3tests."
    echo "           Running 's3tests' starts that other copy. Remove it, or run $BINDIR/s3tests."
fi
for other in /usr/bin/s3tests /usr/local/bin/s3tests "$HOME/.local/bin/s3tests" /opt/s3tests/bin/s3tests; do
    if [ "$other" != "$BINDIR/s3tests" ] && [ -x "$other" ]; then
        echo "  note: another s3tests install exists at $other ($("$other" --version 2>/dev/null | head -1 || echo 'unknown version'))"
    fi
done
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo "  note: $BINDIR is not on PATH; add it, e.g. echo 'export PATH=\"$BINDIR:\$PATH\"' >> ~/.bashrc" ;;
esac
exit 0
