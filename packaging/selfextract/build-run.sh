#!/usr/bin/env bash
# Build dist/s3tests-VERSION.run (called by `make`): one self-extracting file
# that installs s3tests on RHEL 7-10 or macOS. On the target host:
#
#   sh s3tests-VERSION.run                 root: /usr/local    otherwise: ~/.local
#   sh s3tests-VERSION.run --user          ~/.local, config ~/.s3tests.conf
#   sh s3tests-VERSION.run --prefix DIR
#   sh s3tests-VERSION.run --extract DIR   unpack only (inspect before installing)
#   sh s3tests-VERSION.run --verify        check the embedded SHA-256 and exit
#
# The payload is a tarball of the install files; its SHA-256 is checked before
# anything is unpacked.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
TARBALL="${1:?usage: build-run.sh dist/s3tests-VERSION.tar.gz}"
VERSION=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$ROOT/lib/s3tests/__init__.py")
OUT="$(dirname "$TARBALL")/s3tests-$VERSION.run"

if command -v sha256sum >/dev/null 2>&1; then
    SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)
else
    SHA=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)
fi

cat > "$OUT" <<'HEADER'
#!/bin/sh
# s3tests @VERSION@ self-extracting installer (no package manager, network or root needed)
#
#   sh s3tests-@VERSION@.run [--user | --prefix DIR] [install.sh options]
#   sh s3tests-@VERSION@.run --extract DIR     unpack only
#   sh s3tests-@VERSION@.run --verify          check the payload checksum
#
# Default: /usr/local when run as root, ~/.local otherwise.
set -eu
SHA=@SHA@
NAME=s3tests-@VERSION@
SELF="$0"
LINE=$(awk '/^__S3TESTS_PAYLOAD__$/ { print NR + 1; exit 0 }' "$SELF")

payload() { tail -n +"$LINE" "$SELF"; }
sum256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1; fi
}
verify() {
    got=$(payload | sum256)
    if [ "$got" != "$SHA" ]; then
        echo "s3tests installer: checksum mismatch (file damaged or altered); expected $SHA, got $got" >&2
        exit 1
    fi
}

case "${1:-}" in
    -h|--help) sed -n '2,8p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --verify) verify; echo "payload OK (sha256 $SHA)"; exit 0 ;;
    --extract)
        DEST="${2:?--extract needs a directory}"
        verify; mkdir -p "$DEST"; payload | tar xzf - -C "$DEST"
        echo "unpacked to $DEST/$NAME; install with: $DEST/$NAME/install.sh --user"
        exit 0 ;;
esac

verify
TMP=$(mktemp -d "${TMPDIR:-/tmp}/s3tests-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
payload | tar xzf - -C "$TMP"
if [ $# -eq 0 ]; then
    if [ "$(id -u)" = 0 ]; then set -- --prefix /usr/local; else set -- --user; fi
fi
bash "$TMP/$NAME/install.sh" "$@"
exit $?
__S3TESTS_PAYLOAD__
HEADER

sed -i.bak -e "s/@VERSION@/$VERSION/g" -e "s/@SHA@/$SHA/" "$OUT" && rm -f "$OUT.bak"
cat "$TARBALL" >> "$OUT"
chmod 0755 "$OUT"
echo "wrote $OUT (sha256 of payload $SHA)"
