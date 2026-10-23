#!/usr/bin/env bash
# Scheduled runs for comparing S3 solutions. Runs s3tests once per config,
# files the results under RESULTS_DIR/<label>/, then writes a side-by-side
# comparison of the latest run of every label.
#
#   nightly.sh /etc/s3tests/vendor-a.conf /etc/s3tests/vendor-b.conf ...
#
# cron (Mon, Wed, Fri at 02:15):
#   15 2 * * 1,3,5  /usr/local/share/doc/s3tests/examples/nightly.sh /etc/s3tests/*.conf >> /var/log/s3tests-nightly.log 2>&1
#
# Each config should set [target] label (the name used in comparisons).
# Environment:
#   RESULTS_DIR  where results accumulate (default /var/lib/s3tests/results)
#   TIER         smoke, standard or full (default full)
#   KEEP_DAYS    delete run directories older than this (default 90; history.jsonl is kept)
#   JOBS         parallel workers (default from each config)
#
# Exit status: 0 no regressions anywhere, 5 at least one solution regressed
# since its previous run, 3 at least one solution could not be reached,
# 1 otherwise when any run reported failures (known failures included).

set -u

RESULTS_DIR="${RESULTS_DIR:-/var/lib/s3tests/results}"
TIER="${TIER:-full}"
KEEP_DAYS="${KEEP_DAYS:-90}"
mkdir -p "$RESULTS_DIR"

worst=0
note() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

for conf in "$@"; do
    args=(-c "$conf" --"$TIER" --quiet --no-color --results-dir "$RESULTS_DIR" --fail-on-regression)
    [ -n "${JOBS:-}" ] && args+=(-j "$JOBS")
    s3tests "${args[@]}" > /dev/null 2>&1
    rc=$?
    case $rc in
        0) note "$conf: no failures" ;;
        1) note "$conf: failures, none new" ;;
        5) note "$conf: REGRESSION since the previous run" ;;
        3) note "$conf: endpoint unreachable or credentials rejected; recorded as did-not-start" ;;
        4) note "$conf: run stopped early (endpoint outage or time limit)" ;;
        *) note "$conf: exit $rc" ;;
    esac
    # severity order for the job's own exit status: 5 > 3 > 4 > 1 > 0
    for level in 5 3 4 1; do
        if [ $rc = $level ] || [ $worst = $level ]; then worst=$level; break; fi
    done
    # catch buckets from any run that was killed (only ones older than 2 hours)
    s3tests -c "$conf" --cleanup --older-than 120 --yes > /dev/null 2>&1
done

# side-by-side comparison of the latest run of every label
s3tests --compare "$RESULTS_DIR" --format html -o "$RESULTS_DIR/compare.html" > /dev/null
s3tests --compare "$RESULTS_DIR" --format csv  -o "$RESULTS_DIR/compare.csv"  > /dev/null
s3tests --compare "$RESULTS_DIR" --format md   -o "$RESULTS_DIR/compare.md"   > /dev/null
note "comparison: $RESULTS_DIR/compare.html"

# prune old run directories; history.jsonl and latest.json stay
find "$RESULTS_DIR" -mindepth 2 -maxdepth 2 -type d -name '20*' -mtime "+$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null

exit $worst
