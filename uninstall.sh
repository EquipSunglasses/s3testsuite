#!/usr/bin/env bash
# Remove an s3tests install. Keeps s3tests.conf (it holds credentials) and the
# run journals in ~/.local/state/s3tests.
#
#   ~/.local/share/s3tests/uninstall.sh --user               ~/.local
#   sudo /usr/local/share/s3tests/uninstall.sh               /usr/local
#   /opt/s3tests/share/s3tests/uninstall.sh --prefix /opt/s3tests

set -euo pipefail

PREFIX=/usr/local
case "${1:-}" in
    --user) PREFIX="$HOME/.local" ;;
    --prefix) PREFIX="${2:?--prefix needs a directory}" ;;
    --prefix=*) PREFIX="${1#*=}" ;;
    "") ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: uninstall.sh [--user | --prefix DIR]" >&2; exit 2 ;;
esac

BIN="$PREFIX/bin/s3tests"
if [ -f "$BIN" ] && ! grep -q 's3tests-launcher-v1' "$BIN"; then
    echo "refusing to remove $BIN: it is not the s3tests launcher" >&2
    exit 1
fi
if [ ! -e "$BIN" ] && [ ! -d "$PREFIX/share/s3tests" ]; then
    echo "no s3tests install found under $PREFIX"
    exit 0
fi
rm -f "$BIN"
rm -rf "$PREFIX/share/s3tests" "$PREFIX/share/doc/s3tests"
rm -f "$PREFIX/etc/s3tests/s3tests.conf.sample"
rmdir "$PREFIX/etc/s3tests" 2>/dev/null || true
echo "removed s3tests from $PREFIX"
for conf in "$PREFIX/etc/s3tests/s3tests.conf" "$HOME/.s3tests.conf"; do
    [ -f "$conf" ] && echo "kept $conf (delete it yourself if no longer needed)"
done
exit 0
