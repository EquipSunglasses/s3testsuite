# s3tests

S3 protocol conformance tests for on-premises object storage and other
S3-compatible endpoints, built for environments where installing third-party
packages is hard.

- One installer file, `s3tests-VERSION.run`, for RHEL 7 to 10 and macOS. No
  package manager, network access or root needed.
- Python standard library only: no pip, tox, pytest, boto or virtualenv.
- 190 tests in three tiers (smoke, standard, full).
- Removes every bucket it creates and verifies this at the end of each run.
- Results files for comparing solutions and tracking them over time.
- Works on FIPS-enabled hosts.

## Contents

- [Quick start](#quick-start)
- [Install](#install)
- [Configure](#configure)
- [Run](#run)
- [Results and exit codes](#results-and-exit-codes)
- [Cleanup and recovery](#cleanup-and-recovery)
- [Comparing solutions and scheduled runs](#comparing-solutions-and-scheduled-runs)
- [Tracking known gaps](#tracking-known-gaps)
- [Test coverage](#test-coverage)
- [Platform notes](#platform-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Quick start

The installer is `release/s3tests-1.4.4.run` in this repository; copy it to
the host and run it with `sh`.

```
sh s3tests-1.4.4.run --user          # installs to ~/.local, creates ~/.s3tests.conf
vi ~/.s3tests.conf                   # fill in endpoint, access_key, secret_key
s3tests --check                      # TLS, DNS and credentials
s3tests --smoke                      # 21 core tests, about a minute
s3tests -j 8                         # all 190 tests
```

## Install

Copy `s3tests-1.4.4.run` to the host and run it with `sh`:

```
sh s3tests-1.4.4.run --user                 # just for you, no root needed
sudo sh s3tests-1.4.4.run                   # for everyone on the host
sudo sh s3tests-1.4.4.run --prefix /opt/s3tests
```

| install | command | config created |
|---|---|---|
| `--user` | `~/.local/bin/s3tests` | `~/.s3tests.conf` |
| root, no option | `/usr/local/bin/s3tests` | `/usr/local/etc/s3tests/s3tests.conf` |
| `--prefix DIR` | `DIR/bin/s3tests` | `DIR/etc/s3tests/s3tests.conf` |

Run without options, the installer uses `/usr/local` as root and `~/.local`
otherwise. It creates the config file with mode 600 and `ca_bundle` already set
to this host's CA bundle, and keeps an existing config.

The config holds credentials, so after a root install only root can read it.
To run s3tests as yourself, use `sudo s3tests ...` or give yourself a copy,
which is then used first:

```
sudo install -m 600 -o $USER /usr/local/etc/s3tests/s3tests.conf ~/.s3tests.conf
```
 It tells you when the
`bin` directory is not on your `PATH`, or when another s3tests install would
run instead of this one.

Two more options:

```
sh s3tests-1.4.4.run --verify               # check the file's SHA-256, install nothing
sh s3tests-1.4.4.run --extract /tmp/x       # unpack only, to inspect it first
```

The `.run` file is a short shell script followed by the install files. Their
SHA-256 is checked before anything is unpacked, so a damaged or altered file
is refused.

**Upgrade** by running a newer `.run` with the same options; the config is
kept and the output shows the old and new version. **Remove** with the
uninstaller installed next to the library; it also keeps the config:

```
~/.local/share/s3tests/uninstall.sh --user
sudo /usr/local/share/s3tests/uninstall.sh
/opt/s3tests/share/s3tests/uninstall.sh --prefix /opt/s3tests
```

`s3tests --version` shows the version, build, install path and Python in use.
Every run prints the same in its first line.

### Requirements

| | RHEL 7 | RHEL 8 | RHEL 9 / 10 | macOS |
|---|---|---|---|---|
| Python 3.6+ | `python3` (base repo from 7.7) or `rh-python38` / `rh-python36` | `platform-python`, always present | `python3`, always present | system `python3` (Xcode Command Line Tools), python.org or Homebrew |
| Shell | bash | bash | bash | bash 3.2 |

s3tests needs network access only to the S3 endpoint under test. The launcher
finds a suitable Python by itself, including `platform-python` on RHEL 8 and
the Software Collections builds on older RHEL 7. Set
`S3TESTS_PYTHON=/path/to/python3` to choose one.

**FIPS mode.** S3 uses MD5 as an integrity checksum. On a FIPS host whose
Python refuses MD5 (possible on RHEL 7), s3tests switches to a built-in MD5
that runs at about 1.5 MB/s; `s3tests --selftest` shows which is in use.
Lower `large_object_mb` on such hosts.

## Configure

The config is an INI file. The sample leaves a space after each `=` that needs
a value, so you only type the value. Three are required:

```ini
[target]
label = vendor-a-lab
endpoint = https://s3.lab.example.internal

[main]
access_key = AKIA...
secret_key = ...
```

Add a second user to run the cross-user permission tests; without one they
report SKIP:

```ini
[alt]
access_key = AKIA...
secret_key = ...
```

### Choosing a config file

Pass a file with `-c` (or `--config`):

```
s3tests -c /etc/s3tests/vendor-a.conf --smoke
sudo s3tests -c /usr/local/etc/s3tests/s3tests.conf --check    # root-only file
s3tests -c /etc/s3tests/vendor-a.conf --show-config             # confirm what it supplies
```

A path to an existing file given without `-c` (`s3tests vendor-a.conf --smoke`)
is used the same way, with a note saying so; without this, it would be taken as
a test name.

With `-c`, s3tests reads only that file and skips the search below, so a
leftover `~/.s3tests.conf` or `./s3tests.conf` has no effect. A wrong path
stops with `config file not found`.

To use one file for a whole shell session or a cron line, set
`S3TESTS_CONF` instead:

```
export S3TESTS_CONF=/etc/s3tests/vendor-a.conf
s3tests --smoke
```

`-c` takes priority over `S3TESTS_CONF`. If `S3TESTS_CONF` names a file that
does not exist, s3tests falls through to the next file in the search order
instead of stopping. Keep one config per solution under test, each with its own
`label`, and pass each with `-c`; [examples/nightly.sh](#scheduled-runs) does
this for every file given to it.

### Search order

Without `-c`, s3tests reads only the first file it finds, in this order:

1. `$S3TESTS_CONF`
2. `./s3tests.conf`
3. `~/.s3tests.conf`
4. `<prefix>/etc/s3tests/s3tests.conf`, for example `/usr/local/etc/s3tests/s3tests.conf`
5. `/etc/s3tests/s3tests.conf`

A leftover `~/.s3tests.conf` therefore takes priority over the system config.
`s3tests --show-config` prints which file is in use, any found but ignored, and
the settings in effect; `s3tests --check` tests the endpoint, TLS and both users.

| section | option | default | meaning |
|---|---|---|---|
| target | `label` | endpoint host | name of the system under test in results and comparisons |
| target | `endpoint` | | `http(s)://host[:port]` |
| target | `region` | `us-east-1` | region in the signature scope |
| target | `addressing_style` | `path` | `path` or `virtual` (needs wildcard DNS and certificate) |
| target | `verify_ssl` | `true` | TLS certificate verification |
| target | `ca_bundle` | set by the installer | PEM file of trusted CAs; if missing, s3tests warns and uses the system trust store |
| target | `timeout`, `connect_timeout` | `60`, `10` | read and connect timeouts per request, seconds |
| target | `retries` | `3` | retries for connect failures, dropped connections and 503 SlowDown |
| target | `keepalive` | `true` | reuse connections |
| target | `bucket_prefix` | `s3tests-` | prefix of every bucket the suite creates |
| target | `location_constraint` | | sent on CreateBucket when set |
| target | `allow_insecure_sse` | `false` | run SSE-C tests over plain http |
| main | `access_key`, `secret_key`, `session_token` | | primary user |
| alt | `access_key`, `secret_key`, `principal_arn` | | second user |
| run | `tier` | `full` | `smoke`, `standard` or `full` |
| run | `jobs` | `1` | parallel workers |
| run | `include_tags`, `exclude_tags` | | default tag filters |
| run | `large_object_mb`, `part_size_mb` | `16`, `5` | object sizes |
| run | `test_timeout`, `cleanup_timeout` | `300`, `180` | seconds per test, and per test's cleanup |
| run | `hang_grace` | `60` | seconds past its limit before a stuck test is abandoned |
| run | `breaker`, `outage_wait` | `5`, `120` | pause after this many connection errors in a row; stop if the endpoint is gone this many seconds |
| run | `rerun_connection_errors` | `true` | re-run once a test that failed only on a connection error |
| run | `final_cleanup_window` | `180` | seconds the end of a run keeps retrying buckets it could not remove |
| run | `sweep_stale_buckets`, `stale_bucket_minutes` | `true`, `60` | remove test buckets left by earlier runs once they are this old |
| run | `propagation_timeout` | `20` | seconds to wait for ACL and policy changes to take effect |
| run | `bucket_ready_timeout`, `bucket_ready_checks` | `30`, `3` | after CreateBucket, wait up to this many seconds until HEAD succeeds this many times in a row (0 disables); see [Platform notes](#platform-notes) |
| run | `max_runtime` | `0` | minutes before the run stops with cleanup (0 = no limit) |
| run | `state_dir`, `auto_recover` | `~/.local/state/s3tests`, `true` | crash-recovery journals |
| run | `lifecycle_wait_seconds` | `0` | enables the real lifecycle expiration test |
| expected_failures | `test.id = reason` | | see [Tracking known gaps](#tracking-known-gaps) |

Environment variables override the file: `S3TESTS_ENDPOINT`, `S3TESTS_LABEL`,
`S3TESTS_REGION`, `S3TESTS_ADDRESSING`, `S3TESTS_VERIFY_SSL`,
`S3TESTS_CA_BUNDLE`, `S3TESTS_ACCESS_KEY`, `S3TESTS_SECRET_KEY`,
`S3TESTS_SESSION_TOKEN`, `S3TESTS_ALT_ACCESS_KEY`, `S3TESTS_ALT_SECRET_KEY`.
Command line flags (`--endpoint`, `--region`, `--addressing`, `--ca-bundle`,
`--label`, `-k`) override both.

## Run

```
s3tests                                  the configured tier (default full)
s3tests --smoke | --standard | --full    pick a tier
s3tests multipart listing                tests whose id contains a word
s3tests 'object.test_range_*'            glob on test ids
s3tests -t versioning  /  -x slow,worm   only, or skip, tests with these tags
s3tests -j 8                             parallel workers
s3tests -q                               print only failures and the summary
s3tests -v object.test_tricky_key_names  traceback and HTTP history on failure
s3tests -vv ...                          also request and response headers (signatures redacted)
s3tests --list  /  --list-tags           what would run, and the tags
```

Test ids are `module.test_name`. Naming a test or module on the command line
searches all 190 tests, so `s3tests object.test_checksum_crc32` always works.

| tier | tests | use it for |
|---|---|---|
| smoke | 21 | after deploys, upgrades, load balancer or certificate changes; monitoring probes |
| standard | 140 | release qualification, scheduled runs |
| full | 190 | platform evaluation, vendor comparison, before enabling a feature |

Smoke covers the core data path: bucket create, list and delete; object put,
get, head, overwrite, delete, metadata and range reads; listing with
delimiters and paging; multipart complete and abort; copy; batch delete;
versioning; signature rejection; and a presigned GET. Standard adds the rest of
those areas plus tagging, lifecycle configuration and authentication, without
slow, large, limit-probing or newer optional features. Full adds ACLs, bucket
policy, CORS, server-side encryption and Object Lock.

## Results and exit codes

| status | meaning |
|---|---|
| PASS | behaved as S3 specifies |
| FAIL | wrong status, error code, header or data |
| ERROR | exception: timeout, connection reset, unreadable response |
| SKIP | not run, with a reason (see [skip categories](#what-makes-runs-comparable)) |
| XFAIL | failed, and listed in `[expected_failures]` |
| XPASS | passed, but listed in `[expected_failures]`, so the entry can go |

A run ends with a summary like this:

```
Results: 172 pass, 1 fail, 3 error, 14 skip (190 tests in 233.0s)
Pass rate: 97.7% (172 of 176 attempted; expected failures count as failures)
Skipped: 9 not-configured, 5 disabled-by-policy
Cleanup: 248 test bucket(s) created, 248 removed, 0 remain (verified against the endpoint)
network: 2112 requests over 203 connections; retries: 2 connect, 0 stale keep-alive, 0 dropped reads, 0 SlowDown
bucket visibility: all 248 new bucket(s) readable immediately after CreateBucket
```

Each failure shows the expected and actual result and the request:

```
FAIL    0.18s  multipart.test_multipart_part_too_small
              request: expected HTTP 400 EntityTooSmall, got HTTP 200  [POST /s3tests-1a2b3c4d-1/mp?uploadId=... -> 200]
```

`--junit FILE` writes JUnit XML for CI dashboards and `--json FILE` writes the
[results file](#results-file-format).

| exit | meaning |
|---|---|
| 0 | no FAIL or ERROR |
| 1 | at least one FAIL or ERROR, or test buckets that could not be removed |
| 2 | usage or configuration error |
| 3 | endpoint unreachable, or credentials rejected |
| 4 | run stopped early: endpoint gone longer than `outage_wait`, or `--max-runtime` |
| 5 | with `--fail-on-regression`: a test that passed in the previous run fails now |
| 130 | interrupted (Ctrl-C, SIGTERM, SIGHUP) |

## Cleanup and recovery

Each test creates buckets named `<bucket_prefix><random>-<n>`. All of them are
removed automatically, whether the run succeeds, fails, hangs or is
interrupted:

1. Each CreateBucket is recorded before it is sent, so a bucket whose response
   is lost, or whose test fails right after creating it, is still removed.
2. Each test removes its buckets when it finishes: incomplete uploads, every
   object version and delete marker, and objects under GOVERNANCE retention or
   legal hold. Tests never use COMPLIANCE mode, so everything can be deleted.
3. Buckets that could not be removed are retried until `final_cleanup_window`
   ends.
4. The run checks against the endpoint that every bucket it created is gone,
   and reports the result in the `Cleanup:` line.
5. Test buckets left by earlier runs are removed once they are older than
   `stale_bucket_minutes` (60), so a run in progress elsewhere is never touched.

Problems during a run are handled without intervention:

| what happens | what s3tests does |
|---|---|
| a test runs past `test_timeout` | stops it at its next request (ERROR); the run continues |
| a test is stuck inside one request | abandons it after `hang_grace`, cleans its buckets, continues with a new worker |
| a connection drops | retries on a new connection when safe; counted in the `network:` line |
| a test fails only on a connection error | re-runs it once |
| the endpoint stops answering | pauses after `breaker` errors, resumes when it answers, re-runs the affected tests |
| the endpoint stays down past `outage_wait` | marks the remaining tests not run, stops (exit 4) |
| cleanup is refused by a bucket policy or Object Lock | removes the policy, uses governance bypass, clears legal hold |
| Ctrl-C, SIGTERM, SIGHUP | first: stops and cleans up within `hang_grace`; second: exits at once (exit 130) |
| `kill -9`, crash, power loss | buckets are already in the journal; the next run removes them first |

The journal is a small file per run in `~/.local/state/s3tests` (bucket names,
never credentials), deleted when a run ends cleanly.

To check or clean by hand:

```
s3tests --cleanup --dry-run              list test buckets (names starting with bucket_prefix)
s3tests --cleanup --yes                  delete them
s3tests --cleanup --older-than 60 --yes  only those older than an hour
s3tests --recover                        remove buckets from runs that were killed
s3tests --max-runtime 30                 stop a run after 30 minutes, with cleanup
```

## Comparing solutions and scheduled runs

Give each system under test a `label` in its config and point every run at one
results directory:

```
s3tests --results-dir /var/lib/s3tests/results --fail-on-regression
```

```
/var/lib/s3tests/results/
  vendor-a-lab/
    20261013T020001Z-6f111e5fc539/
      results.json      the run (format below)
      junit.xml         for CI dashboards
      console.log       what the run printed
      changes.txt       differences from the previous run
    latest.json         the newest run that actually ran
    history.jsonl       one line per run, including runs that could not start
  vendor-b-lab/
    ...
```

After each run, s3tests compares it with the previous run of the same label
and prints regressions, fixes and changed failures. With
`--fail-on-regression` the exit status is 5 when something regressed, so a
scheduler can alert on new problems while known failures (exit 1) stay quiet.

These commands work on saved results and need no endpoint:

```
s3tests --compare /var/lib/s3tests/results                    latest run of every label, side by side
s3tests --compare /var/lib/s3tests/results --format html -o compare.html
s3tests --compare /var/lib/s3tests/results --format csv -o compare.csv
s3tests --diff OLD/results.json NEW/results.json              regressions and fixes; exit 1 on regressions
s3tests --trend /var/lib/s3tests/results/vendor-a-lab         pass rate per run and flaky tests
```

`--compare` shows each solution's pass rate overall and per module, and every
test whose outcome differs, with the HTTP status and S3 error code of each
failure. Formats: `text`, `md`, `csv` (one row per solution and test), `html`
and `json`. `--trend` flags tests whose outcome changes between runs of the
same build, which points to an unstable platform or network.

### What makes runs comparable

- Use the same s3tests version and tier on every solution; `--compare` warns
  when they differ.
- Configure every solution the same way: a second user, https, and the same
  `exclude_tags`.
- Pass rate is `pass / (pass + fail + error)`. Expected failures count as
  failures, so one platform's list of known gaps cannot raise its score.
- Skips are excluded from the pass rate and reported by category:

| skip category | meaning |
|---|---|
| `not-implemented` | the server answered NotImplemented or 501: a missing feature |
| `disabled-by-policy` | supported but switched off (Block Public Access, ACLs disabled, SSE-C blocked) |
| `not-configured` | the run lacked something (second user, https, lifecycle wait): fix the config |
| `not-run` | the run stopped before the test finished |

### Scheduled runs

`nightly.sh` (installed in `<prefix>/share/doc/s3tests/examples/`) runs each
config it is given, files the results, removes leftover buckets and writes
`compare.html`, `compare.csv` and `compare.md` into the results directory:

```
# Mon, Wed, Fri at 02:15
15 2 * * 1,3,5  /usr/local/share/doc/s3tests/examples/nightly.sh /etc/s3tests/*.conf >> /var/log/s3tests-nightly.log 2>&1
```

It exits 5 if any solution regressed, 3 if one could not be reached, 4 if a
run stopped early, 1 for failures that are not new, and 0 otherwise.
`RESULTS_DIR`, `TIER`, `JOBS` and `KEEP_DAYS` (default 90) adjust it.

### Results file format

`results.json` (also written by `--json FILE`) has a versioned schema. New
fields may be added; a change that breaks readers changes the `schema` value.
Tests are sorted by id and times are UTC.

```
{
  "schema": "s3tests.results/1",
  "run":     {"id", "label", "started", "finished", "duration_s", "tier", "selection", "jobs",
              "aborted", "abort_reason", "exit_code", "leftover_buckets",
              "cleanup": {"buckets_created", "buckets_remaining", "verified", "stale_removed", "stale_failed"}},
  "tool":    {"name", "version", "build", "python", "os", "host"},
  "target":  {"label", "endpoint", "region", "addressing", "second_user", "tls"},
  "test_set":{"catalog_size", "catalog_fingerprint", "selected", "selected_fingerprint"},
  "summary": {"tests", "pass", "fail", "error", "skip", "pass_rate", "skip_categories", "by_module"},
  "network": {"requests", "connections", "connect_retries", "stale_retries", "read_retries", "slowdown_retries"},
  "bucket_visibility": {"checked", "delayed", "never_ready", "max_wait_seconds", "mean_wait_seconds"},
  "tests": [
    {"id": "object.test_put_get_roundtrip", "module", "tier", "tags",
     "status": "FAIL",                     PASS FAIL ERROR SKIP XFAIL XPASS
     "outcome": "fail",                    pass fail error skip (XFAIL counts as fail)
     "skip_category", "duration_s",
     "failure_kind": "403 AccessDenied",   stable key for grouping failures
     "http_status": 403, "s3_code": "AccessDenied", "request": "GET /<bucket>/key",
     "message", "expected_failure", "rerun", "hung", "warnings", "http_trace"}
  ]
}
```

Messages are normalized so the same failure reads the same in every run:
bucket names become `<bucket>`, upload and version ids `<id>`, and random test
data `<hex>`, `<uuid>` or `<base64>`. Group and join on `failure_kind` rather
than the message. Two runs with the same `selected_fingerprint` ran the same
tests.

## Tracking known gaps

Record a platform's known gaps in its config instead of editing tests:

```ini
[expected_failures]
object.test_checksum_* = flexible checksums not supported until the next release
policy.test_policy_wrong_resource_bucket = gateway accepts it, ticket STOR-1234
```

Keys are test ids or globs. A matching failure reports XFAIL and leaves the
exit status at 0; once fixed, it reports XPASS so you can remove the entry. For
features a platform does not offer at all, use `exclude_tags` (for example
`sse_s3, worm`).

## Test coverage

`COVERAGE.html` (installed with the docs) lists every test with its
description, tier and tags, with search and filters.

| module | tests | covers |
|---|---|---|
| bucket | 14 | create, head, delete, naming rules, ownership, cross-user isolation |
| object | 39 | PUT/GET/HEAD/DELETE, metadata, 33 awkward key names, key length, ranges, conditional GET and PUT, Content-MD5, payload hash, CRC32/SHA256 checksums |
| listing | 20 | ListObjects v1 and v2: prefix, delimiter, paging, markers, start-after, encoding-type, UTF-8 byte order, fetch-owner |
| multipart | 17 | complete, ETag format, list parts and uploads, abort, part size and order errors, UploadPartCopy, GET by partNumber |
| copy | 12 | same and cross bucket, COPY vs REPLACE metadata, copy to self, copy-source conditions |
| versioning | 13 | enable and suspend, versions, delete markers, null version, version paging, copy by versionId |
| multidelete | 6 | DeleteObjects, quiet mode, special keys, 1000-key limit, delete markers |
| tagging | 7 | object and bucket tags, x-amz-tagging header, limits |
| lifecycle | 11 | expiration, noncurrent versions, abort incomplete uploads, filters, validation, optional real expiration |
| cors | 7 | configuration, preflight allow and deny, CORS headers on GET |
| policy | 8 | storage, validation, public read, explicit deny, cross-user grants |
| acl | 9 | canned ACLs, grants, anonymous access, AccessControlPolicy round trip |
| auth | 13 | wrong secret, unknown key, clock skew, tampered headers, presigned URLs |
| sse | 7 | SSE-C (including multipart), SSE-S3, default bucket encryption |
| objectlock | 7 | lock-enabled buckets, GOVERNANCE retention, legal hold |

`s3tests --list-tags` lists the tags, for example `multiuser` (needs `[alt]`),
`presign`, `sse_c`, `sse_s3`, `worm`, `slow` and `large`.

## Platform notes

**S3-compatible gateways**

- `region` must match the region the gateway uses in the signature scope.
- SSE-C needs https; if the gateway allows SSE-C over http, set
  `allow_insecure_sse = true`.
- SSE-S3 and default bucket encryption need a key management backend on the
  gateway; without one, exclude tag `sse_s3`.
- `bucket.test_bucket_name_invalid_forms` fails by design when the gateway
  allows relaxed bucket names.
- For the real lifecycle expiration test, use a lab gateway with a shortened
  lifecycle interval and set `lifecycle_wait_seconds` (for example 180).
- Where users live in tenants or accounts, set `[alt] principal_arn`, for
  example `arn:aws:iam::TENANT:user/UID`.

**Subresources the platform does not know**

S3 operations such as `DELETE /bucket?publicAccessBlock` name a subresource
after the `?`. Some platforms ignore a subresource they do not support and run
the plain bucket operation instead, so that request deletes an empty bucket
(seen on Dell ObjectScale; the response is 204). The ACL tests' setup removes
the public access block only after `GET ?publicAccessBlock` returns an actual
configuration, so it never sends that DELETE to such a platform.
`bucket.test_delete_public_access_block_keeps_bucket` checks for the behavior
directly and fails with `deleted the bucket itself` where it occurs.

**New buckets that return 404 NoSuchBucket**

Some clustered platforms acknowledge CreateBucket before every node knows the
bucket. A request that reaches another node in the next moment (often through
a load balancer) gets 404 NoSuchBucket, so a test fails in setup although
nothing is wrong with the operation it tests. To keep these failures out of
the results, after each successful CreateBucket s3tests sends HEAD until it
succeeds `bucket_ready_checks` times in a row (default 3), for up to
`bucket_ready_timeout` seconds (default 30). Each check uses a new connection,
so a load balancer can route it to a different node. Only 404 counts as not
ready; any other answer ends the wait.

The delay is measured, not hidden. Each affected test carries a warning such
as `bucket <bucket> returned 404 on HEAD for 2.7s after CreateBucket succeeded`.
The run summary prints a `bucket visibility:` line, the results file has a
`bucket_visibility` section, and `--compare` shows a `new bucket 404s` column,
so solutions can be compared on how soon a new bucket is usable.

If 404s continue after the wait:

- Raise `bucket_ready_checks` (to 5 or more) when the load balancer has many
  backend nodes, so more nodes are checked before a test starts.
- Raise `bucket_ready_timeout` when `bucket visibility:` reports buckets that
  were never readable.
- Check the platform's consistency setting for bucket metadata and whether the
  load balancer keeps a client on one node. A 404 on a later request after the
  wait passed means the nodes still disagree; that is a finding to raise with
  the vendor, not something the suite should retry around.
- Set `bucket_ready_timeout = 0` to measure the raw behavior: setup failures
  then show how often a new bucket is not yet usable.
- Run against each node behind the load balancer as well as the VIP to catch
  per-node configuration drift.

**AWS S3 as a reference**

Use `endpoint = https://s3.amazonaws.com` (or `https://s3.<region>.amazonaws.com`)
with the matching `region`. Some AWS defaults switch features off:

| AWS default | what s3tests does |
|---|---|
| ACLs disabled on new buckets | creates the ACL tests' buckets with ACLs enabled |
| Block Public Access on new buckets | removes the block on its own test buckets |
| Block Public Access for the whole account | cannot override: SKIP (disabled-by-policy) |
| SSE-C blocked on new buckets | SKIP (disabled-by-policy) |
| ACL and policy changes take seconds to apply | retries for up to `propagation_timeout` and reports the delay |

For the cross-user tests, add a second IAM user in `[alt]` with
`principal_arn = arn:aws:iam::<account>:user/<name>`.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `no endpoint configured in <file>` | that file is the one being read; the message lists any other config files that were ignored. Edit or remove it, or use `-c` |
| `cannot read ... permission denied` | a root install's config is root-only; use `sudo s3tests` or copy it to `~/.s3tests.conf` (see [Install](#install)) |
| exit 3, `CERTIFICATE_VERIFY_FAILED` | set `ca_bundle` to the CA that issued the endpoint's certificate; `-k` only to confirm |
| `warning: ca_bundle ... does not exist` | path for another OS: macOS `/etc/ssl/cert.pem`, RHEL `/etc/pki/tls/certs/ca-bundle.crt` |
| every request `SignatureDoesNotMatch` | wrong `region`, or a proxy or load balancer rewriting the Host header or path |
| `SignatureDoesNotMatch` only for keys with spaces, `+` or unicode | a proxy or load balancer re-encodes the path |
| `RequestTimeTooSkewed` | client clock off by more than 15 minutes |
| virtual-host tests fail, path style works | wildcard DNS or certificate missing for `*.endpoint` |
| many SKIPs in `multiuser` | no `[alt]` user configured |
| output looks like an older version | another copy runs first on PATH; `s3tests --version` shows which |
| `python3: command not found` | RHEL 7.7+: `yum install python3`; older RHEL 7: `rh-python38`; or set `S3TESTS_PYTHON` |
| `DeadlineExceeded` | the test ran past `test_timeout`; raise it for slow links or large objects |
| a test "hung ... abandoned" | a request never completed; check the gateway or load balancer logs |
| `Operation timed out` (Errno 60/110) | TCP connections to the endpoint are timing out (network, firewall connection limits, or the endpoint) |
| `404 NoSuchBucket` in the ACL tests, with `DELETE ...?publicAccessBlock -> 204` just before it in `-vv` output | the platform deleted the bucket on that request; fixed in 1.4.4 (see [Platform notes](#platform-notes)) |
| `404 NoSuchBucket` right after a bucket was created | the platform makes new buckets visible asynchronously; see [Platform notes](#platform-notes) and raise `bucket_ready_checks` or `bucket_ready_timeout` |
| "took Ns to take effect" | the platform applies ACL and policy changes asynchronously; raise `propagation_timeout` if tests fail |
| `Cleanup:` line shows buckets remaining | they are retried on the next run, or run `s3tests --recover` |

`s3tests -vv <test id>` prints the full request and response headers
(signatures redacted), usually enough to open a vendor ticket.

## Development

**What the .run file is.** One file: a short `/bin/sh` script followed by a
gzipped tarball after the line `__S3TESTS_PAYLOAD__`. The script checks the
tarball's SHA-256, unpacks it to a temporary directory, runs `install.sh` and
removes the temporary copy. The tarball holds the files of this repository
listed by `make print-sources`, plus a generated `lib/s3tests/_build.py`
containing the build id. The script part is in
`packaging/selfextract/build-run.sh`.

**Verifying the .run.** `release/s3tests-VERSION.run` is committed alongside
the source it was built from. To confirm the installer contains this source
and nothing else, without executing it:

```
make verify-run                                       # or: packaging/tools/verify-run.sh FILE.run
```

This checks the payload checksum, compares the installer script with
`build-run.sh`, compares every payload file byte for byte with the tree, and
checks the build id against the library checksum. CI
(`.github/workflows/ci.yml`) runs the same check on every push, then installs
the committed `.run` on Rocky Linux 8 and 9, AlmaLinux 10 and macOS and runs
the self-test. To inspect the payload by hand:

```
sh release/s3tests-1.4.4.run --extract /tmp/s3t       # unpacks only
tail -n +$(awk '/^__S3TESTS_PAYLOAD__$/{print NR+1; exit}' release/s3tests-1.4.4.run) \
    release/s3tests-1.4.4.run | tar tzf -              # lists files without running anything
```

**Build** the installer from the source tree:

```
make              # dist/s3tests-VERSION.run
make release      # copy it to release/, write release/SHA256SUMS, run verify-run
make check        # byte-compile and offline self-test
make coverage     # regenerate COVERAGE.html
```

After changing any source file, run `make release` and commit the new
`release/` files with the change, or `make verify-run` and CI fail.

Building needs only make, tar, gzip, sed, cksum and sha256sum (or shasum).
The version is in `lib/s3tests/__init__.py`; each build also gets a build id
(date and code checksum) shown by `s3tests --version`.

**Run from the source tree** without installing:

```
S3TESTS_HOME=lib bin/s3tests -c my.conf
```

**Add a test** as a `test_*` function in a module under `lib/s3tests/suites/`:

```python
from ..assertions import eq, error, ok
from ..runner import tags

@tags('mytag')            # add 'smoke' to put it in the smoke tier
def test_something(ctx):
    """One-line description shown by --list and in reports."""
    b = ctx.new_bucket()                        # removed automatically
    ok(ctx.c.put_object(b, 'k', b'data'))       # ctx.c main user, ctx.alt second user, ctx.anon unsigned
    eq(ok(ctx.c.get_object(b, 'k')).body, b'data', 'body')
    error(ctx.c.get_object(b, 'missing'), 404, 'NoSuchKey')
```

- Assertions: `ok`, `status`, `error`, `eq`, `true`, `contains`, `skip`,
  `skip_if_unimplemented`, `skip_if_disabled`.
- Context: `ctx.new_bucket()`, `ctx.versioned_bucket()`, `ctx.acl_bucket()`,
  `ctx.key()`, `ctx.data(n)`, `ctx.user_id(client)`, `ctx.eventually(...)`.
- `ctx.c.request(method, bucket, key, query=..., headers=..., body=...)` sends
  any signed S3 request; `creds=None` sends it unsigned.

Keep tests independent so they can run in parallel.

**Layout**

```
bin/s3tests                  launcher: finds the library and Python 3.6+
lib/s3tests/                 the Python package
  suites/                    the tests, one module per feature area
  client.py, sigv4.py        S3 client and request signing
  runner.py, context.py      test execution, time limits, bucket tracking and removal
  safety.py, cleanup.py      crash journal, recovery, --cleanup
  results.py, compare.py     results file, --compare, --diff, --trend
etc/s3tests.conf.sample      every option, documented
install.sh, uninstall.sh     run by the installer; uninstall.sh is installed with the library
examples/nightly.sh          scheduled runs and comparisons
packaging/selfextract/       builds the .run file
packaging/tools/             builds COVERAGE.html; verify-run.sh checks a .run against the tree
release/                     the built installer and SHA256SUMS, kept in git
.github/workflows/ci.yml     self-test, verify-run and install checks
```

`s3tests --selftest` checks the request signer against the published AWS
Signature Version 4 test vectors without contacting any endpoint.

## License

MIT. See `LICENSE`.
