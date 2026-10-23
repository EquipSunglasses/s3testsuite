#!/usr/bin/env bash
# Check that a .run installer is exactly this source tree, without running it.
#
#   packaging/tools/verify-run.sh [FILE.run]     (default: newest release/s3tests-*.run)
#   make verify-run
#
# Checks, in order:
#   1. the payload's SHA-256 matches the one recorded in the header
#   2. the header is the one packaging/selfextract/build-run.sh writes
#   3. every payload file is byte-identical to the file in this tree, and no
#      file is missing or extra (lib/s3tests/_build.py is the one generated file)
#   4. the build id's checksum matches the library in the payload
# Exit 0 when all pass, 1 otherwise.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
RUN="${1:-}"
if [ -z "$RUN" ]; then
    RUN=$(ls "$ROOT"/release/s3tests-*.run 2>/dev/null | sort -V | tail -1 || true)
    [ -n "$RUN" ] || { echo "verify-run: no release/s3tests-*.run; pass a file" >&2; exit 2; }
fi
[ -f "$RUN" ] || { echo "verify-run: $RUN not found" >&2; exit 2; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/s3tests-verify.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
bad=0
pass() { echo "  ok    $*"; }
fail() { echo "  FAIL  $*"; bad=1; }
sum256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1; fi
}

echo "verifying $RUN against $ROOT"

# 1. payload checksum
LINE=$(awk '/^__S3TESTS_PAYLOAD__$/ { print NR + 1; exit 0 }' "$RUN")
[ -n "$LINE" ] || { echo "  FAIL  no payload marker; not an s3tests .run file"; exit 1; }
head -n $((LINE - 1)) "$RUN" > "$TMP/header"
tail -n +"$LINE" "$RUN" > "$TMP/payload.tar.gz"
want=$(sed -n 's/^SHA=//p' "$TMP/header")
got=$(sum256 < "$TMP/payload.tar.gz")
if [ "$want" = "$got" ]; then pass "payload sha256 $got"; else fail "payload sha256 $got, header records $want"; fi

# 2. header matches build-run.sh
VERSION=$(sed -n 's/^NAME=s3tests-//p' "$TMP/header")
sed -n "/^cat > \"\$OUT\" <<'HEADER'\$/,/^HEADER\$/p" "$ROOT/packaging/selfextract/build-run.sh" \
    | sed '1d;$d' | sed -e "s/@VERSION@/$VERSION/g" -e "s/@SHA@/$want/" > "$TMP/header.expected"
if cmp -s "$TMP/header" "$TMP/header.expected"; then
    pass "installer script matches packaging/selfextract/build-run.sh (version $VERSION)"
else
    fail "installer script differs from packaging/selfextract/build-run.sh:"
    diff "$TMP/header.expected" "$TMP/header" | sed 's/^/          /' || true
fi

# 3. payload files against the tree
mkdir "$TMP/x"
tar xzf "$TMP/payload.tar.gz" -C "$TMP/x"
DIR="$TMP/x/s3tests-$VERSION"
[ -d "$DIR" ] || { fail "payload has no s3tests-$VERSION/ directory"; exit 1; }
SRC_VERSION=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$ROOT/lib/s3tests/__init__.py")
[ "$SRC_VERSION" = "$VERSION" ] || fail "installer is version $VERSION, source tree is $SRC_VERSION"

(cd "$ROOT" && make -s print-sources) | tr ' ' '\n' | sed '/^$/d' | LC_ALL=C sort > "$TMP/expected"
(cd "$DIR" && find . -type f | sed 's|^\./||') | grep -vx 'lib/s3tests/_build.py' | LC_ALL=C sort > "$TMP/actual"
missing=$(LC_ALL=C comm -23 "$TMP/expected" "$TMP/actual")
extra=$(LC_ALL=C comm -13 "$TMP/expected" "$TMP/actual")
[ -z "$missing" ] || fail "missing from payload: $(echo $missing)"
[ -z "$extra" ] || fail "in payload but not in the source list: $(echo $extra)"
n=0; differ=""
while read -r f; do
    if [ -f "$ROOT/$f" ] && ! cmp -s "$ROOT/$f" "$DIR/$f"; then differ="$differ $f"; fi
    n=$((n + 1))
done < "$TMP/actual"
if [ -z "$differ" ]; then pass "$n files identical to the source tree"
else
    fail "files differ from the source tree:$differ"
    for f in $differ; do diff -u "$ROOT/$f" "$DIR/$f" | head -20 | sed 's/^/          /' || true; done
fi

# 4. build id
build=$(sed -n "s/^BUILD = '\(.*\)'/\1/p" "$DIR/lib/s3tests/_build.py" 2>/dev/null || true)
libsum=$(cd "$DIR" && cat $(find lib -name '*.py' ! -name '_build.py' | LC_ALL=C sort) | cksum | cut -d' ' -f1)
if [ "${build#*-}" = "$libsum" ]; then pass "build id $build matches the library checksum"
else fail "build id '${build:-none}' does not match library checksum $libsum"; fi

if [ $bad = 0 ]; then echo "result: $RUN is this source tree"; else echo "result: MISMATCH"; fi
exit $bad
