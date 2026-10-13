# s3tests

S3 protocol conformance tests for on-premises object storage (MinIO,
StorageGRID, ECS and similar S3-compatible gateways), built for enterprises
where bringing in third-party packages is hard.

- Standard library only: no tox, pytest, nose, boto, requests, pip or
  virtualenv. SigV4 signing, the HTTP client, the test runner and the
  reporters ship in this package.
- Runs on the interpreter already on the host: RHEL 7 `python3` (3.6),
  RHEL 8 `platform-python` (3.6), RHEL 9/10 `python3`, macOS system `python3`.
- Noarch RPMs for RHEL 7 (yum) and RHEL 8, 9 and 10, a yum/dnf repository
  builder, and a no-RPM installer. On macOS, a `.pkg` or `install.sh` with
  the same layout.
- Works on FIPS-enabled hosts.
- Console, JUnit XML and JSON output, with exit codes suited to CI and cron.
- Every test cleans up its own buckets, versions and uploads.

## Contents

- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Install on RHEL](#install-on-rhel)
- [Install on RHEL without RPM](#install-on-rhel-without-rpm)
- [Install on macOS](#install-on-macos)
- [Configure](#configure)
- [Run](#run)
- [Tiers: smoke, standard, full](#tiers-smoke-standard-full)
- [Cleaning up leftover buckets](#cleaning-up-leftover-buckets)
- [When a run fails or hangs](#when-a-run-fails-or-hangs)
- [Results and exit codes](#results-and-exit-codes)
- [Tracking known gaps](#tracking-known-gaps)
- [Test coverage](#test-coverage)
- [CI and scheduled runs](#ci-and-scheduled-runs)
- [Gateway notes](#gateway-notes)
- [Running against AWS S3](#running-against-aws-s3)
- [Troubleshooting](#troubleshooting)
- [Writing tests](#writing-tests)
- [Building packages](#building-packages)
- [Project layout](#project-layout)
- [How the tool itself was validated](#how-the-tool-itself-was-validated)

## Quick start

```
sudo yum install ./s3tests-1.1.0-1.el8.noarch.rpm   # RHEL 7: the .el7 RPM
s3tests --selftest                     # offline check, no endpoint needed
sudoedit /etc/s3tests/s3tests.conf     # set endpoint and keys
s3tests --check                        # TLS, DNS and credentials
s3tests --smoke                        # 21 core tests, about a minute
s3tests -j 8 --standard                # 140 tests
s3tests -j 8 --full                    # all 190
s3tests --cleanup --dry-run            # confirm nothing was left behind
```

## Requirements

| | RHEL 7 | RHEL 8 | RHEL 9 / 10 | macOS |
|---|---|---|---|---|
| Package | `s3tests-*.el7.noarch.rpm` (yum, rpm 4.11) | `s3tests-*.el8.noarch.rpm` | same el8 RPM | `.pkg` or installer |
| Python | `python3` 3.6 (base repo from 7.7) or Software Collections `rh-python36`/`rh-python38` | `platform-python` 3.6 (always present) | `python3` 3.9+ | system `python3` (Xcode CLT) or python.org/Homebrew |
| Shell | bash 4.2 | bash 4.4 | bash 5 | bash 3.2 |
| Network | HTTP(S) to the S3 endpoint only | same | same | same |

Nothing is downloaded at install or run time.

## Install on RHEL

There are two RPMs with the same files; pick by release:

| release | RPM | install | depends on |
|---|---|---|---|
| RHEL 7 / CentOS 7 | `s3tests-1.1.0-1.el7.noarch.rpm` | `sudo yum install ./s3tests-1.1.0-1.el7.noarch.rpm` | `bash`, `python3` |
| RHEL 8, 9, 10 | `s3tests-1.1.0-1.el8.noarch.rpm` | `sudo yum install ./s3tests-1.1.0-1.el8.noarch.rpm` (or `dnf`) | `bash`, `(python3 or platform-python)` |

`rpm -ivh` works too. On RHEL 8 and later `yum` is `dnf`, so the same
commands apply. The el8 RPM uses a rich dependency that RHEL 7's rpm (4.11)
cannot read, which is why RHEL 7 has its own build. Both use a gzip payload
and SHA-256 digests, readable by every rpm from RHEL 7 on.

**FIPS mode.** On RHEL 8 and later, MD5 (used by S3 as an integrity checksum)
is requested with `usedforsecurity=False` and works in FIPS mode. If a FIPS
host's Python refuses MD5 outright (possible on RHEL 7), s3tests switches to a
built-in pure-Python MD5; `s3tests --selftest` reports which is in use. It
runs at about 1.5 MB/s, so lower `large_object_mb` and `part_size_mb` stay at
5 on such hosts.

**RHEL 7 and Python.** `yum install python3` provides Python 3.6 from the base
repository on RHEL 7.7 and later. On older RHEL 7 hosts install
`rh-python38` (or `rh-python36`) from Software Collections; the launcher
loads it automatically. Without either, use `S3TESTS_PYTHON=/path/to/python3`.

Files:

```
/usr/bin/s3tests                    bash launcher
/usr/share/s3tests/s3tests/         Python package (runs with -B, writes no .pyc)
/etc/s3tests/s3tests.conf           config, %config(noreplace), mode 0600 root
/etc/s3tests/s3tests.conf.sample    reference copy of every option
/usr/share/doc/s3tests-1.1.0/       README, examples/nightly.sh
```

Upgrades keep your edited `/etc/s3tests/s3tests.conf`. Remove with
`sudo yum remove s3tests` (or `dnf remove`).

### From a yum repository

To let hosts run `yum install s3tests` and `yum update s3tests`, publish the
RPMs as a repository on any internal web server:

```
make yum-repo BASEURL=https://repo.example.internal/s3tests
# copy dist/repo/ to the web server's /s3tests/ path, then on each host:
sudo curl -o /etc/yum.repos.d/s3tests.repo https://repo.example.internal/s3tests/s3tests-el7.repo   # RHEL 7
sudo curl -o /etc/yum.repos.d/s3tests.repo https://repo.example.internal/s3tests/s3tests-el8.repo   # RHEL 8+
sudo yum install s3tests
```

`dist/repo` holds one repository per release (`el7/`, `el8/`) with gzip
metadata that RHEL 7's yum can read, and a ready `.repo` file for each. They
must stay separate, or RHEL 7 yum would choose the el8 build. The RPMs are
unsigned, so the `.repo` files set `gpgcheck=0`; to sign, run
`rpm --addsign` on the RPMs, publish the public key as
`RPM-GPG-KEY-s3tests`, and build with `GPGKEY=1 make yum-repo ...`. Satellite,
Pulp or Artifactory can import the same RPMs instead.

The system config is readable by root only. Non-root users copy the sample to
`~/.s3tests.conf` and `chmod 600` it.

## Install on RHEL without RPM

For hosts where you cannot install packages, or do not have root, on RHEL 7
through 10. Both options below install the same files as the RPM, need only
bash, coreutils and the system Python, and touch nothing outside the chosen prefix (plus
`~/.s3tests.conf` for a user install).

### Option 1: self-extracting installer (one file)

Copy `s3tests-1.1.0.run` to the host and run it:

```
sh s3tests-1.1.0.run                     # as root: /usr/local; as a user: ~/.local
sh s3tests-1.1.0.run --user              # ~/.local/bin/s3tests, config ~/.s3tests.conf
sh s3tests-1.1.0.run --prefix /opt/s3tests
sh s3tests-1.1.0.run --verify            # check the embedded SHA-256, install nothing
sh s3tests-1.1.0.run --extract /tmp/x    # unpack only, to inspect before installing
```

The file is a short shell script followed by the release tarball. Its
SHA-256 is checked before anything is unpacked, and a damaged or altered file
is refused. Build it with `make run`.

### Option 2: tarball and install.sh

```
tar xzf s3tests-1.1.0.tar.gz && cd s3tests-1.1.0
./install.sh --user                      # or: sudo ./install.sh, or --prefix /opt/s3tests
```

### After either option

| install | command | config created |
|---|---|---|
| `--user` | `~/.local/bin/s3tests` | `~/.s3tests.conf` |
| root, default | `/usr/local/bin/s3tests` | `/usr/local/etc/s3tests/s3tests.conf` |
| `--prefix DIR` | `DIR/bin/s3tests` | `DIR/etc/s3tests/s3tests.conf` |

The config is created mode 600 with `ca_bundle` already pointing at this
host's CA bundle (`/etc/pki/tls/certs/ca-bundle.crt` on RHEL); fill in
`endpoint`, `access_key` and `secret_key`. s3tests finds it without `-c`.
Add the `bin` directory to `PATH` if the installer says it is not on it.

Upgrade by running a newer installer over the same prefix; your config is
kept. Remove with the uninstaller installed next to the library:

```
~/.local/share/s3tests/uninstall.sh --user
sudo /usr/local/share/s3tests/uninstall.sh
/opt/s3tests/share/s3tests/uninstall.sh --prefix /opt/s3tests
```

It keeps your config and refuses to touch an RPM-managed install. On RHEL 8
the launcher uses `/usr/libexec/platform-python` when `python3` is not
installed; on older RHEL 7 it loads `rh-python38`/`rh-python36` from Software
Collections.

## Install on macOS

From the source tarball:

```
tar xzf s3tests-1.1.0.tar.gz && cd s3tests-1.1.0

./install.sh --user          # ~/.local/bin/s3tests, creates ~/.s3tests.conf (mode 600)
sudo ./install.sh            # /usr/local, same layout as the RPM
make macpkg                  # dist/s3tests-1.1.0.pkg for other Macs (needs Xcode CLT)
sudo installer -pkg dist/s3tests-1.1.0.pkg -target /
```

Set `SIGN_IDENTITY="Developer ID Installer: ..."` before `make macpkg` to sign
the package. Uninstall with `sudo /usr/local/share/s3tests/uninstall.sh` (or
`~/.local/share/s3tests/uninstall.sh --user`); your config is left in place.
The self-extracting installer (`sh s3tests-1.1.0.run --user`) also works on
macOS.

To run straight from a checkout on any OS, without installing:

```
S3TESTS_HOME=lib bin/s3tests -c my.conf
```

## Configure

The config is an INI file. Without `-c`, the first one found wins:

1. `$S3TESTS_CONF`
2. `./s3tests.conf`
3. `~/.s3tests.conf`
4. `<prefix>/etc/s3tests/s3tests.conf` (`/usr/local/etc/...` on macOS)
5. `/etc/s3tests/s3tests.conf`

The sample config (`etc/s3tests.conf.sample`, installed as
`/etc/s3tests/s3tests.conf` by the RPM) leaves a space after each `=` that
needs a value, so you only type the value. Required: `endpoint`,
`access_key`, `secret_key`. Minimal example:

```ini
[target]
endpoint = https://s3.lab.example.internal
region = us-east-1
addressing_style = path
# macOS path; on RHEL use /etc/pki/tls/certs/ca-bundle.crt
ca_bundle = /etc/ssl/cert.pem

[main]
access_key = AKIA...
secret_key = ...

[alt]
access_key = AKIA...
secret_key = ...

[run]
jobs = 8
exclude_tags = sse_s3
```

| section | option | default | meaning |
|---|---|---|---|
| target | `endpoint` | | `http(s)://host[:port]` |
| target | `region` | `us-east-1` | region in the signature scope |
| target | `addressing_style` | `path` | `path` or `virtual` (needs wildcard DNS and cert) |
| target | `verify_ssl` | `true` | TLS certificate verification |
| target | `ca_bundle` | `/etc/ssl/cert.pem` in the sample (macOS); the RPM sets `/etc/pki/tls/certs/ca-bundle.crt` | PEM file of trusted CAs; if it does not exist s3tests warns and uses the system trust store |
| target | `timeout` | `60` | read timeout per request, seconds |
| target | `connect_timeout` | `10` | TCP and TLS connect timeout, seconds |
| target | `retries` | `3` | retries for connect failures, dropped connections and 503 SlowDown |
| target | `keepalive` | `true` | reuse connections (one per test instead of one per request) |
| target | `bucket_prefix` | `s3tests-` | prefix of every bucket the suite creates |
| target | `location_constraint` | | sent on CreateBucket when set |
| target | `allow_insecure_sse` | `false` | run SSE-C tests over plain http |
| main | `access_key`, `secret_key`, `session_token` | | primary user |
| alt | `access_key`, `secret_key`, `principal_arn` | | second user; cross-user tests skip without it |
| run | `tier` | `full` | `smoke`, `standard` or `full` |
| run | `jobs` | `1` | parallel workers |
| run | `test_timeout` | `300` | seconds per test before it is stopped (ERROR) |
| run | `cleanup_timeout` | `180` | seconds allowed to remove one test's buckets |
| run | `hang_grace` | `60` | seconds past its limit before a stuck test is abandoned |
| run | `breaker` | `5` | consecutive connection errors that pause the run (0 = off) |
| run | `outage_wait` | `120` | seconds a paused run waits for the endpoint before stopping (0 = stop at once) |
| run | `rerun_connection_errors` | `true` | re-run once a test that failed only on a connection error |
| run | `final_cleanup_window` | `180` | seconds the end of a run keeps retrying leftover buckets |
| run | `propagation_timeout` | `20` | seconds to wait for ACL/policy changes to take effect (0 = strict) |
| run | `max_runtime` | `0` | minutes before the run aborts with cleanup (0 = no limit) |
| run | `state_dir` | `~/.local/state/s3tests` | crash-recovery journals |
| run | `auto_recover` | `true` | clean up after dead runs before starting |
| run | `include_tags`, `exclude_tags` | | default tag filters |
| run | `large_object_mb`, `part_size_mb` | `16`, `5` | object sizes |
| run | `lifecycle_wait_seconds` | `0` | enables the real expiration test |
| expected_failures | `test.id = reason` | | see [Tracking known gaps](#tracking-known-gaps) |

Environment variables override the file, which suits CI secret stores:
`S3TESTS_ENDPOINT`, `S3TESTS_REGION`, `S3TESTS_ADDRESSING`,
`S3TESTS_VERIFY_SSL`, `S3TESTS_CA_BUNDLE`, `S3TESTS_ACCESS_KEY`,
`S3TESTS_SECRET_KEY`, `S3TESTS_SESSION_TOKEN`, `S3TESTS_ALT_ACCESS_KEY`,
`S3TESTS_ALT_SECRET_KEY`. Command line flags (`--endpoint`, `--region`,
`--addressing`, `--ca-bundle`, `-k`) override both.

`s3tests --show-config` prints the effective settings.

## Run

```
s3tests                                  the configured tier (default full)
s3tests --smoke | --standard | --full    pick a tier for this run
s3tests multipart listing                tests whose id contains a word
s3tests 'object.test_range_*'            glob on test ids
s3tests -t versioning                    only tests with a tag
s3tests -x slow,worm                     skip tags
s3tests --no-default-excludes            ignore exclude_tags from the config
s3tests -j 8 --junit out/junit.xml --json out/results.json
s3tests -v object.test_tricky_key_names  traceback and HTTP history on failure
s3tests -vv ...                          also request/response headers (signatures redacted)
s3tests --fail-fast                      stop at the first failure
s3tests --list                           selected tests and their tags
s3tests --list-tags                      tags with test counts
```

Test ids are `module.test_name`, for example `multipart.test_multipart_abort`.
Naming tests or modules on the command line searches all 190 tests unless a
tier flag is also given, so `s3tests object.test_checksum_crc32` always works.

## Tiers: smoke, standard, full

| tier | tests | flag | use it for |
|---|---|---|---|
| smoke | 21 | `--smoke` | post-deploy and post-upgrade checks, load balancer and certificate changes, monitoring probes |
| standard | 140 | `--standard` | release qualification of a cluster or gateway, nightly runs |
| full | 190 | `--full` | platform evaluation, vendor comparison, before enabling a feature |

Smoke covers the core data path: bucket create, head, list and delete;
object put, get, head, overwrite, delete, metadata and range reads; listing
with delimiters and paging; multipart complete and abort; copy; batch
delete; one versioning check; signature rejection; and a presigned GET.

Standard adds everything else in bucket, object, listing, multipart, copy,
multidelete, versioning, tagging, lifecycle configuration and auth, leaving
out tests tagged `slow`, `large`, `limits`, `checksum` and
`conditional_write`. Full adds ACLs, bucket policy, CORS, SSE, Object Lock
and those tags. Tiers are nested, and `--list` shows each test's tier.
Set a default with `[run] tier`.

## Cleaning up leftover buckets

Every test removes its own buckets. If cleanup fails, the run retries those
buckets once at the end and prints any that remain (exit code 1). A run
that is killed outright can leave buckets behind; sweep them with:

```
s3tests --cleanup --dry-run              list buckets starting with bucket_prefix
s3tests --cleanup                        delete them, after a y/N prompt
s3tests --cleanup --yes                  no prompt (cron, CI)
s3tests --cleanup --older-than 60 --yes  only buckets older than an hour
```

Only buckets whose names start with `[target] bucket_prefix` are touched, for
the main and the alt user. Each one is emptied completely: incomplete
multipart uploads, every object version and delete marker, and objects under
GOVERNANCE retention or legal hold. Use `--older-than` when another run may be
in progress against the same endpoint, since it shares the prefix.

Every test and every cleanup has a time limit (`test_timeout`,
`cleanup_timeout`). A test that exceeds it is stopped at its next request and
reported as ERROR; its buckets are still removed.

## Results and exit codes

| state | meaning |
|---|---|
| PASS | behaved as S3 specifies |
| FAIL | wrong status, error code, header or data |
| ERROR | exception such as a connection reset or unparsable response |
| SKIP | not configured (no alt user, SSE-C over http) or the server answered NotImplemented |
| XFAIL | failed, and listed in `[expected_failures]` |
| XPASS | passed, but listed in `[expected_failures]`, so the entry can go |

The run ends with the facts, per module and in total, followed by any
failures and the network counters:

```
Results: 172 pass, 1 fail, 3 error, 14 skip (190 tests in 233.0s)

Failures:
  FAIL  acl.test_canned_public_read_object: anonymous GET: expected 2xx, got HTTP 403 ...
network: 2112 requests over 203 connections; retries: 2 connect, 0 stale keep-alive, 0 dropped reads, 0 SlowDown
```

When a run stops early, a `Run stopped early: <reason>` line follows the
results. Pass or fail is carried by the exit status, not the wording.

| exit | meaning |
|---|---|
| 0 | no FAIL or ERROR |
| 1 | at least one FAIL or ERROR, or test buckets that could not be removed |
| 2 | usage or configuration error |
| 3 | endpoint unreachable, or credentials rejected by the preflight ListBuckets |
| 4 | run stopped: endpoint unreachable past `outage_wait`, or `--max-runtime` |
| 130 | interrupted (Ctrl-C, SIGTERM, SIGHUP) |

Failure lines show the expected and actual status, the S3 error code and the
request, for example:

```
FAIL    0.18s  multipart.test_multipart_part_too_small
              request: expected HTTP 400 EntityTooSmall, got HTTP 200  [POST /s3tests-1a2b3c4d-1/mp?uploadId=... -> 200]
```

JUnit XML groups results per module (`s3tests.<module>`) and attaches the
HTTP history of failed tests as `system-out`. JSON holds the same data in one
document.

Each test creates buckets named `<bucket_prefix><random>-<n>` and removes them
afterwards with every version, delete marker, incomplete upload and
GOVERNANCE-locked object. Tests never use COMPLIANCE mode, so nothing is left
that cannot be deleted. See [Cleaning up leftover buckets](#cleaning-up-leftover-buckets).

## When a run fails or hangs

Every failure mode has an automatic way out: the stuck test is bypassed, the
run continues or stops cleanly, and the test buckets are removed. Nothing
needs to be done by hand unless the endpoint stays unreachable.

Quick reference:

```
s3tests --cleanup --dry-run        anything left behind? (lists buckets starting with bucket_prefix)
s3tests --recover                  remove buckets recorded by runs that were killed or crashed
s3tests --cleanup --yes            remove every leftover test bucket, journal or not
s3tests --max-runtime 30           cap a run at 30 minutes, with cleanup
Ctrl-C once                        stop and clean up (at most hang_grace seconds)
Ctrl-C twice                       exit now; the next run cleans up automatically
```

After every run, before it exits, s3tests makes sure nothing is left: buckets
tests could not remove are retried until the cleanup window ends, and buckets
recorded by earlier runs that died are removed too. Only if the endpoint stays
unreachable for the whole window do they wait for the next run (or
`--recover`).

| what happens | automatic response | result |
|---|---|---|
| a test runs past `test_timeout` (default 300 s) | stopped at its next request; its buckets removed | that test ERROR, run continues |
| a test is stuck inside one request (no response, half-open connection) | the watchdog abandons it `hang_grace` seconds after its limit, removes its buckets from another thread and starts a replacement worker | that test ERROR "hung", run continues |
| cleanup is refused by a leftover bucket policy | removes the bucket policy and retries | bucket removed |
| cleanup is refused by Object Lock | retries with governance bypass, then clears legal hold | bucket removed (COMPLIANCE mode is never used) |
| a connection drops (idle keep-alive closed, read reset) | the request is retried on a new connection when it is safe: always if it never reached the server, and for GET/HEAD/OPTIONS otherwise | transparent; counted in the `network:` line |
| one test fails only on a connection error | the test is re-run once (after an outage, or at the end of the run); the result carries a warning | real result instead of a network ERROR |
| the endpoint stops answering | after `breaker` (default 5) consecutive connection errors the run pauses, probes the endpoint, and resumes when it answers, re-running the tests the outage broke | run completes normally |
| the endpoint stays down past `outage_wait` (default 120 s) | queued tests are marked not run and the run stops | exit 4 |
| test buckets cannot be removed (network down at the end) | retried with backoff for up to `final_cleanup_window` (default 180 s) inside the same run, then journaled | removed as soon as the endpoint answers |
| the run passes `max_runtime` / `--max-runtime` | same abort path | exit 4 |
| Ctrl-C, SIGTERM, SIGHUP | first signal: stop new tests, running tests stop at their next request and clean up (at most `hang_grace` s); second signal: exit at once | exit 130 |
| stdout closed (piped into `head`, SSH dropped) | output stops, run aborts and cleans up | |
| `kill -9`, crash, power loss, laptop asleep | nothing can run at that moment, but every created bucket is already in the journal | next run removes them first |

How a test bucket is removed, escalating only when the server refuses:

1. abort incomplete multipart uploads
2. batch-delete every object version and delete marker (DeleteObjects, 1000 at a time)
3. delete what remains one by one by version id
4. on 403: retry with `x-amz-bypass-governance-retention`, then switch legal hold off
5. still 403: remove the bucket policy (a leftover Deny) and retry
6. delete the bucket; on 403 remove the policy and retry; on 409 start again (3 attempts)
7. anything still stuck is reported with the key, version and HTTP error, and stays in the journal

The journal is one small file per run in `[run] state_dir` (default
`~/.local/state/s3tests`, or `$S3TESTS_STATE_DIR`). It lists bucket names and
which configured user created them, never credentials, and is deleted when a
run ends with nothing left. At startup each run looks for journals of runs
that are no longer alive on this machine for the same endpoint and removes
their buckets:

```
recovering 3 bucket(s) from interrupted run run-20261009T201853-8491.jsonl (pid 8491, 2026-10-09 20:18)
  s3tests-222afbab-1                                              removed
```

Run it on its own with `s3tests --recover`; disable it with `--no-recover` or
`[run] auto_recover = false`. If the endpoint is still down, the journal is
kept and recovery tries again next time. `--cleanup` remains the catch-all,
since it finds buckets by prefix even without a journal (another host, a
deleted state directory).

Each of these paths was exercised against a local S3 server: a test blocked
forever inside a socket read, one and two Ctrl-C, `kill -9`, the endpoint
killed mid-run, a stdout pipe closed mid-run, `--max-runtime`, and a bucket
whose policy denied deletes. All ended with zero test buckets left.

## Tracking known gaps

Record per-platform gaps in the config instead of editing tests:

```ini
[expected_failures]
object.test_checksum_* = flexible checksums not supported until the next gateway release
policy.test_policy_wrong_resource_bucket = gateway accepts it, ticket STOR-1234
```

Keys are test ids or globs. A matching failure reports XFAIL and leaves the
exit code at 0. When an upgrade fixes it, it reports XPASS. For features the
platform does not offer at all, use `exclude_tags` (for example
`sse_s3, worm`). Keep one config per platform or cluster and the run output
becomes a conformance record you can diff between releases.

## Test coverage

190 tests in 15 modules. `COVERAGE.html` lists every test with its description
tier and tags, with search, tier and tag filters; regenerate it with `make coverage`.

| module | tests | covers |
|---|---|---|
| bucket | 14 | create, head, delete, naming rules, ownership, cross-user isolation |
| object | 39 | PUT/GET/HEAD/DELETE, metadata, 33 awkward key names, key length, ranges, conditional GET and PUT, Content-MD5, payload hash, CRC32/SHA256 checksums |
| listing | 20 | ListObjects v1 and v2: prefix, delimiter, paging, markers, start-after, encoding-type, UTF-8 byte order, fetch-owner |
| multipart | 17 | complete, ETag format, list parts and uploads, abort, EntityTooSmall, InvalidPartOrder, InvalidPart, UploadPartCopy, GET by partNumber |
| copy | 12 | same and cross bucket, COPY vs REPLACE metadata, copy to self, copy-source conditions |
| versioning | 13 | enable and suspend, versions, delete markers, null version, version paging, copy by versionId |
| multidelete | 6 | DeleteObjects, quiet mode, special keys, 1000-key limit, delete markers |
| tagging | 7 | object and bucket tags, x-amz-tagging header, limits |
| lifecycle | 11 | expiration, noncurrent versions, abort incomplete MPU, And filters, validation, optional real expiration run |
| cors | 7 | configuration, preflight allow and deny, CORS headers on GET |
| policy | 8 | storage, validation, public read, explicit deny, cross-user grants |
| acl | 9 | canned ACLs, grants, anonymous access, AccessControlPolicy round trip |
| auth | 13 | wrong secret, unknown key, clock skew, tampered headers, presigned GET, PUT, expired and tampered URLs |
| sse | 7 | SSE-C (including multipart), SSE-S3, default bucket encryption |
| objectlock | 7 | lock-enabled buckets, GOVERNANCE retention, legal hold |

Useful tags: `multiuser` (needs `[alt]`), `anonymous`, `permissions`,
`integrity`, `limits`, `keys`, `range`, `conditional`, `conditional_write`,
`checksum`, `presign`, `sse_c`, `sse_s3`, `worm`, `slow`, `large`. Run
`s3tests --list-tags` for the full list.

## CI and scheduled runs

Jenkins or GitLab:

```
export S3TESTS_ENDPOINT=https://s3.lab.example.internal
export S3TESTS_ACCESS_KEY=... S3TESTS_SECRET_KEY=...
s3tests -c ci.conf -j 8 --no-color --junit results/junit.xml --json results/results.json
```

Publish `results/junit.xml` with the JUnit plugin. Exit code 1 fails the
stage on regressions while XFAIL entries keep known gaps from blocking it.

Cron, with dated results and 30-day retention:

```
0 2 * * * /usr/share/doc/s3tests-1.1.0/examples/nightly.sh /etc/s3tests/lab.conf /etc/s3tests/stage.conf
```

`RESULTS_DIR` (default `/var/tmp/s3tests`) and `KEEP_DAYS` adjust it.

## Gateway notes

- `region` must match the region the gateway uses in the SigV4 signature
  scope (`us-east-1` unless the platform is configured otherwise).
- SSE-C tests need https. If the gateway is configured to allow SSE-C over
  plain http, set `allow_insecure_sse = true`.
- SSE-S3 and default bucket encryption need a key management backend on the
  gateway; without one, exclude tag `sse_s3`.
- `bucket.test_bucket_name_invalid_forms` fails by design when the gateway
  allows relaxed (non-DNS) bucket names.
- To exercise lifecycle expiration for real, use a lab gateway with a
  shortened lifecycle interval (many platforms have a debug or test setting
  that makes one lifecycle day last seconds) and set
  `lifecycle_wait_seconds`, for example 180. The test is tagged `slow` and
  `lifecycle_expiration`.
- Where users live in tenants or accounts, set
  `[alt] principal_arn` to the ARN form the gateway expects, for example
  `arn:aws:iam::TENANT:user/UID`.
- Run against each gateway node behind the load balancer as well as the VIP
  to catch per-node configuration drift.

## Running against AWS S3

AWS makes a useful reference before testing an on-prem platform. Use
`endpoint = https://s3.amazonaws.com` (or `https://s3.<region>.amazonaws.com`)
with the matching `region`. Several AWS defaults turn features off. The suite
works around them where an account allows it, and reports SKIP with the reason
where it does not:

| AWS default | affects | what the suite does |
|---|---|---|
| Object Ownership = BucketOwnerEnforced on new buckets (ACLs disabled) | ACL tests | creates those buckets with ObjectWriter ownership |
| Block Public Access on new buckets | public ACL and public policy tests | removes the bucket-level block on its own test buckets |
| Block Public Access at account level | same | cannot override; SKIP "blocked by S3 Block Public Access" |
| SSE-C blocked on new buckets | `sse_c` tests | SKIP "SSE-C is blocked for this bucket" |
| anonymous `GET /` redirects to the S3 web page | `auth.test_anonymous_list_buckets` | a redirect counts as not listing |
| ACL, policy and public access changes take a few seconds to apply | ACL and policy tests | checks are repeated for up to `propagation_timeout` seconds; any delay is reported as a warning |

Expect ACL, public policy and SSE-C results to be skipped on most AWS
accounts. Run the cross-user tests with a second IAM user in `[alt]` and set
`principal_arn = arn:aws:iam::<account>:user/<name>`; the tests create real
buckets, so `--cleanup --dry-run` afterwards is a good habit.

## Troubleshooting

| symptom | likely cause |
|---|---|
| exit 3, `CERTIFICATE_VERIFY_FAILED` | set `ca_bundle` to your internal CA; `-k` only for a quick check |
| `warning: ca_bundle ... does not exist` | the path is for another OS; macOS `/etc/ssl/cert.pem`, RHEL `/etc/pki/tls/certs/ca-bundle.crt` |
| every request `SignatureDoesNotMatch` | wrong `region`, or a proxy or load balancer rewriting the Host header or path |
| `SignatureDoesNotMatch` only for keys with spaces, `+` or unicode | a proxy or load balancer normalizes or decodes the path before the gateway sees it |
| `RequestTimeTooSkewed` everywhere | client clock off by more than 15 minutes; check chronyd |
| `object.test_last_modified_is_recent` fails | cluster clock skew relative to the client |
| virtual-host tests fail, path style works | wildcard DNS or wildcard certificate missing for `*.endpoint` |
| many SKIPs in `multiuser` | no `[alt]` user configured |
| SSE-C tests all SKIP | endpoint is http; see Gateway notes |
| results look like an older build (old header, fixed failures returning) | another copy runs first on PATH. `s3tests --version` shows version, build and install path; every run prints them in its first line. The installer warns when another copy shadows the one it installed |
| `python3: command not found` | RHEL 7.7+: `yum install python3`; older RHEL 7: install `rh-python38`; or set `S3TESTS_PYTHON=/path/to/python3` |
| RHEL 7 `yum install` reports `rpmlib(RichDependencies)` | the el8 RPM was used; install the `.el7` RPM |
| a test reports `DeadlineExceeded` | the test ran past `test_timeout`; raise it for slow links or large `large_object_mb` |
| "test bucket(s) remain" at the end of a run | read the reason printed per bucket; they are journaled and retried on the next run, or run `s3tests --recover` |
| a test reported "hung ... abandoned" | a request never completed (load balancer or gateway holding connections open); check the gateway logs for that time |
| run paused, then stopped (exit 4) | the endpoint stayed unreachable longer than `outage_wait`; check `s3tests --check`, then rerun |
| `[Errno 60] Operation timed out` (macOS) or `[Errno 110]` (Linux) | TCP connections to the endpoint timed out: network, NAT/firewall connection limits, or the endpoint itself. Keep-alive keeps connection churn low; the run pauses and resumes on its own |
| a warning "took Ns to take effect" | the platform applies ACL/policy changes asynchronously (normal on AWS); raise `propagation_timeout` if tests fail on it |
| the `network:` line shows many retries | an unstable path to the endpoint; tests still pass, but investigate before trusting performance numbers |
| SKIP mentioning Block Public Access or Object Ownership | the platform or account disables that feature; see AWS section |

`s3tests -vv <test id>` prints the full request and response headers
(signatures redacted) for the failing exchange, which is usually enough to
open a vendor ticket.

## Writing tests

Add a function to any module in `lib/s3tests/suites/`, or a new module there.
Functions named `test_*` take a context and are discovered automatically.

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

Helpers:

- `ok(resp)`, `status(resp, 206)`, `error(resp, 404, 'NoSuchKey')`,
  `eq(a, b, what)`, `true(cond, msg)`, `contains(...)`, `skip(reason)`,
  `skip_if_unimplemented(resp, feature)`.
- `ctx.new_bucket()`, `ctx.versioned_bucket()`, `ctx.acl_bucket()` (ACLs and
  public access allowed where the platform permits), `ctx.key()`, `ctx.data(n)`,
  `ctx.user_id(client)`, `ctx.custom_client(access, secret)`.
- `ctx.c.request(method, bucket, key, query=..., headers=..., body=...)` sends
  any S3 request with SigV4. `creds=None` sends it unsigned, `when=` backdates
  the signature, `payload_hash=` overrides `x-amz-content-sha256`.
- `ctx.c.presign(...)` and `ctx.anon.fetch(method, url)` for presigned URLs.

Keep tests independent so they can run in parallel. Then
`make check` and `make rpm`.

## Building packages

| target | output | needs |
|---|---|---|
| `make coverage` | `COVERAGE.html` from the test code | python3 |
| `make check` | byte-compile check and offline self-test | python3 |
| `make dist` | `dist/s3tests-1.1.0.tar.gz` with the rendered spec inside | tar, gzip, sed |
| `make rpm` | RPM and SRPM for this host's release in `dist/` | rpm-build, make |
| `make rpm-el7`, `make rpm-el8`, `make rpms` | the RHEL 7 build, the RHEL 8+ build, or both, from any build host | rpm-build, make |
| `make yum-repo BASEURL=...` | both RPMs as yum repositories plus `.repo` files in `dist/repo` | createrepo_c or createrepo |
| `make run` | `dist/s3tests-1.1.0.run`, self-extracting installer | tar, gzip, sha256sum or shasum |
| `make srpm` | SRPM only, for an internal build system | rpm-build |
| `make macpkg` | `dist/s3tests-1.1.0.pkg` | macOS with Xcode CLT |
| `make install PREFIX=...` | direct install | bash |

`rpmbuild -ta dist/s3tests-1.1.0.tar.gz` also works. The spec's `%check` runs
the self-test, and byte compilation is skipped so one noarch RPM serves RHEL 8,
9 and 10. The `.el8` or `.el9` dist tag comes from the build host; the payload
is identical. The version lives in `lib/s3tests/__init__.py`.

## Project layout

```
bin/s3tests                    bash launcher: finds the library and a Python >= 3.6
lib/s3tests/
  sigv4.py                     AWS Signature Version 4, header and presigned forms
  client.py                    S3 REST client on http.client
  config.py                    INI, environment and CLI configuration
  context.py                   per-test clients, naming, time limits, bucket removal
  cleanup.py                   --cleanup sweep of leftover test buckets
  safety.py                    run journal, abort control, crash recovery
  runner.py                    discovery, tiers, workers, watchdog, circuit breaker
  report.py                    console, JUnit XML, JSON
  assertions.py                S3-aware assertion helpers
  selftest.py                  offline checks against AWS reference vectors
  md5compat.py                 pure-Python MD5 for FIPS hosts whose hashlib refuses it
  suites/                      the tests, one module per feature area
etc/s3tests.conf.sample        every option, documented
install.sh                     shared installer (RPM, macOS, direct)
packaging/rpm/                 spec template and build script
packaging/macos/               .pkg builder
packaging/selfextract/         self-extracting .run builder
install.sh, uninstall.sh       install into / remove from any prefix
examples/nightly.sh            cron wrapper with dated results
```

## How the tool itself was validated

- The signer reproduces the published AWS SigV4 test vectors
  (`s3tests --selftest`, also run in the RPM `%check`).
- Canonical URIs and query strings match botocore's S3 signer for encoded
  keys, unicode and `/` in query values.
- The installed RPM payload was run against moto's S3 server, where the suite
  flags moto's known gaps (no authorization enforcement, missing validation)
  and leaves no buckets behind.

Before treating a failure as a platform defect, run the disputed test once
against a reference you trust, such as AWS S3 or a known-good cluster.

## License

MIT. See `LICENSE`.
