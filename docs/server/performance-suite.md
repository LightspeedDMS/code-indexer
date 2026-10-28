# Performance Regression Suite

An operator-run CLI that measures query latency and throughput on a target server through BOTH
front doors (REST and MCP) against a fixed set of public repositories. Measuring a target is an
operator task, not a CI gate; the suite's own correctness self-test runs in e2e Phase 4 (see
"Suite self-tests").

## Usage

    python3 scripts/analysis/perf_suite/run.py --url URL --username USER --password PASS

That is the whole normal case. The suite checks the target, runs every scenario, writes a report
and prints a summary. Without `--password` the password is read from `CIDX_PERF_PASSWORD`, then
prompted for. The password never appears in output, reports or error messages.

Options (all optional):

| Option | Meaning |
|--------|---------|
| `--quick` | one repetition at concurrency 1 (fast smoke) |
| `--repetitions N` | repetitions per scenario (default 5) |
| `--label TEXT` | free-text tag recorded in the report (for example a machine class) |
| `--include-cold-semantic` | also measure cache-cold semantic search; calls the paid embedding provider |
| `--baseline FILE` | compare against a baseline written earlier |
| `--write-baseline FILE` | write a sanitized baseline; refuses to overwrite without `--force` |
| `--tolerance-pct P` | regression tolerance in percent, a finite number >= 0 (default 10); NaN, infinity or a negative value exits 2 before login |
| `--corpus FILE` | advanced: a different corpus YAML |
| `--output-dir DIR` | advanced: report directory (default `.analysis/perf/`, gitignored) |
| `--http-timeout S` | advanced: per-request HTTP timeout (default none) |
| `--xray-poll-deadline S` | advanced: give up polling one X-Ray job after S seconds and count it as an error (default 300) |

Exit codes: 0 pass; 1 regression, wrong results, errors (warmup included), an aborted scenario,
a target that became unreachable during the run (including a login lost because a token
refresh was rejected), or a closing health check that is
`unavailable` or `unhealthy` (a `degraded` target is shown but does not fail the run); 2 guard
abort or configuration error (bad arguments, rejected login, missing corpus repositories, an
empty or missing password (also an empty prompt or a closed stdin), a baseline file that is
missing, unreadable or malformed, a baseline that does not match the run,
an unsafe `--label` with `--write-baseline`, `--write-baseline` with `--quick` or fewer than 3
`--repetitions`, or a baseline write refused after the run). Reports are written whenever
measurement started, including exit 1 (also after the target became unreachable), the
baseline-mismatch exit 2 and a baseline refused after the run. In that last case the verdict line
still reports the measurements (it can read `VERDICT: PASS`); the line printed just before it,
"baseline not written: ...; exit code 2 is this baseline refusal", explains the exit code.

## Repositories that must be registered on the target

The suite is READ-ONLY: it never registers, refreshes, configures or deletes anything. The
developer must register and index these as golden repositories before running it:

- https://github.com/pallets/click
- https://github.com/tiangolo/typer
- https://github.com/tqdm/tqdm
- https://github.com/pallets/flask
- https://github.com/pallets/markupsafe
- https://github.com/jhy/jsoup
- https://github.com/microsoft/TypeScript
- https://github.com/Homebrew/homebrew-core

They are matched by URL (case, trailing slash and `.git` ignored), so any alias works; the suite
queries `<alias>-global`. Before measuring, a startup guard lists the global repositories
(`list_global_repos`) and sends one FTS probe (`POST /api/query`) per corpus repository before
anything is measured; these probes appear in the request tally. A repository that is not
registered is not probed, but the guard still probes every repository it did match; then, if
any repository is missing or its probe failed or returned nothing, the suite exits 2 naming
every affected URL at once.

## What is measured

Corpus: `scripts/analysis/perf_suite/corpus.yaml`. Every per-repository query has an expected
hit (a path or path suffix that must be in the top N; for file reads, text the file must
contain), and every fan-out query has curated expected hits for several repositories.

| Operation | REST | MCP |
|-----------|------|-----|
| semantic (cache-warm) | `POST /api/query` | `search_code` |
| fts | `POST /api/query` | `search_code` |
| regex (ripgrep on both doors) | `POST /api/regex/search` | `regex_search` |
| xray (async job, polling included) | `POST /api/xray/search` + `GET /api/jobs/{job_id}` | `xray_search` + `get_job_details` |
| files_list | n/a | `list_files` |
| file_read | n/a | `get_file_content` |
| fanout semantic/fts (all corpus repos at once) | `POST /api/query/multi` | `search_code` with a list of aliases |
| fanout regex (all corpus repos at once) | `POST /api/regex/search` with a list of aliases | `regex_search` with a list of aliases |

File listing and reading are MCP-only and shown as `n/a` for REST: the REST file routes serve
activated repositories only and answer 404 for a global alias, and the suite never activates
anything. The FTS regex mode of `POST /api/query` (`regex=true`, a token-index feature) is
intentionally not measured; regex means the ripgrep search on both doors.

Fan-out requires a result group for every corpus repository before it checks curated hits; a
response missing a repository is a wrong result. X-Ray jobs are submitted without a timeout
override (the server's own job timeout applies) and polled every 0.1 s until a terminal status;
each poll has its own HTTP timeout and a failed poll aborts that scenario. As a safety net the
whole poll is bounded by `--xray-poll-deadline` (default 300 s: the server's 120 s default job
timeout plus 180 s for queueing and compilation); a job still running at the deadline counts as
an error ("poll deadline"), so X-Ray polling never hangs and never passes silently. The bound
applies to X-Ray polling only: every other request waits for the server unless `--http-timeout`
is set (default none).

Load model: closed loop, concurrency 1, 4 and 16 (X-Ray 1 and 2), a warmup pass excluded from the
numbers, then N repetitions. Each repetition reports req/s, p50, p95, p99, errors, wrong results
and n; the table shows the median of req/s, latencies and n across repetitions and the SUM of
errors and wrong results across repetitions (so a single error is never hidden by a median of
0). The report also keeps the spread (MAD).

- Served response: the server answered with a well-formed success (HTTP 2xx with a JSON body;
  for MCP, HTTP 200 with a JSON-RPC result whose tool payload is not an error). Only served
  responses are timed: req/s = served responses / wall time of the repetition, and p50/p95/p99
  are taken over served responses only. A wrong result is a served response (it is timed) but
  still fails the verdict.
- Failed request (`err`): a transport failure (connection refused/reset, server disconnected,
  timeout), an HTTP error status, a JSON-RPC error, an MCP tool error or a failed job. Failures
  are counted but never timed, so a dead or failing target can never look fast.
- `n`: served responses per repetition (median across repetitions). A cell with n = 0 shows
  `n/a` for req/s and latency, is never written to a baseline and is never compared (see
  Baselines). n can exceed the number of queries in a scenario: each worker sends at least 4
  requests per repetition and queries are reused round-robin (a 3-query fan-out at c=1 sends 4).
  When that happens in a run, the summary and the markdown add the footnote "n can exceed the
  query count".
- `wrong` (wrong_result): the request succeeded but the expected hit was not in the results.
  Up to 5 samples per scenario (query, expected path, top returned paths) are listed in the
  summary, the markdown and the JSON, so the missing query is identifiable.
- Errors are counted per class (`HTTP 503`, `JSON-RPC error`, `tool error`, `ConnectError`, ...).
  The summary lists every scenario with errors as "operation door c=N: class COUNTx: sample"
  (warmup included), and the JSON keeps `errors_by_class` per scenario. Samples pass the same
  sanitizer as the JSON error samples: the password, the target URL and host, any other URL and
  any IPv4 address are replaced.
- Warmup is excluded from timing but its errors and wrong results count toward the verdict; the
  verdict then says so (for example "2 wrong results incl. warmup").
- Percentiles are nearest-rank per repetition over served responses: with n below 20 p95 is the
  maximum sample, with n below 100 p99 is. Such cells are marked `*`; the footnote under the
  tables explains `n` and the marker. `--quick` and X-Ray scenarios usually show it.
- Target gone: 20 consecutive connection-level failures across the whole run (connection
  refused or reset, server disconnected, connect timeout; never an HTTP status or a slow-response
  timeout) abort the run. Nothing more is sent: the scenario in progress is aborted with
  "target unreachable: 20 consecutive connection failures", every remaining scenario is recorded
  as "not run (target unreachable)", the closing health check is attempted once, the reports are
  written, the verdict is FAIL and the exit code is 1. No baseline comparison is made.
- Login lost: the token is renewed after 8 minutes. If that renewal login is rejected during
  the run, it is never retried and the run ends exactly like a vanished target ("login lost:
  token refresh rejected; not retried"): remaining scenarios are "not run", the closing health
  is "unavailable", the reports are written, the verdict is FAIL and the exit code is 1. A
  rejected first login, before anything is measured, still exits 2 without a report.
- An unexpected response shape aborts the scenario with its message (never a silent zero).

## Reading the output

The terminal summary starts with the server version, the registered repository count (the volume
dimension), the duration and the label. One table per operation shows REST and MCP side by side,
one row per concurrency level, with the columns req/s, p50, p95, p99, err, wrong and n (and delta
with `--baseline`). The footnote explains n and the `*` marker; the "Errors" listing and the
wrong-result samples follow. Then comes the request tally: every endpoint or tool called and how
many requests were sent, including login, health and guard calls; it lists only read-only
allow-listed doors. Last are the verdict line and the report paths.

Reports are written to `.analysis/perf/perf_<timestamp>.json` and `.md`. The JSON holds every
repetition, the warmup (first-call, which for X-Ray includes compilation) latencies, error samples,
health before and after, the corpus with resolved aliases and the settings. No report carries the
target URL, a host name, an address or the username; the label identifies the machine class.
Every text that comes from the server (health status and version, error bodies, job errors,
returned paths in wrong-result samples) passes the same sanitizer before it is stored or
printed: the password, the username, the access token, the target URL and host, any other URL
and any IPv4 address are replaced (a credential shorter than 3 characters is not replaced, so it
cannot mangle every match). Reports name local files only by name or by a path relative to the
repository (the corpus setting, the report paths in the markdown, a corpus entry given as a
local path, shown by its basename); the terminal prints the full report paths.

## Baselines

    python3 scripts/analysis/perf_suite/run.py --url URL --username USER --label box-a \
        --write-baseline .analysis/perf/baseline-box-a.json
    python3 scripts/analysis/perf_suite/run.py --url URL --username USER --label box-a \
        --baseline .analysis/perf/baseline-box-a.json

Write baselines from a full run (default 5 repetitions; at least 3): `--write-baseline` together
with `--quick` or `--repetitions` below 3 is refused at argument validation, before login (exit
2, one message line, nothing sent, no report written). After a run, `--write-baseline` still
refuses (exit 2, no file written) when any scenario ended with fewer than 3 repetitions. A baseline is meaningful only against the same hardware class (same `--label`)
and a similar registered repository count; the count is shown but never blocks a comparison.

A baseline is a sanitized subset (label, server version, registered repository count, suite
version and per-scenario p50, spread, req/s and repetitions; no URL, host or username). A
baseline scenario from a single repetition (or an older file without `repetitions` whose spread
is 0) has no spread estimate: it is listed in `comparison.no_spread` and the summary says
"baseline has no spread estimate", so its threshold is `--tolerance-pct` alone. A scenario regresses
when its median p50 is worse than the baseline by more than max(`--tolerance-pct`, 3 x the
baseline spread in percent); faster beyond the same threshold is reported as an improvement.
The summary adds a delta column and both registered repository counts; a differing count never
blocks the comparison. The baseline and the run must cover the same scenarios: a baseline
scenario that is absent from the run, aborted in the run or has a non-positive p50, and a run
scenario the baseline lacks, make the run exit 2 naming every such key in one message (never a
silent skip). A baseline written by an older suite, by a `--quick` or otherwise partial run, or
without `--include-cold-semantic` when the run uses it must be rewritten from a matching full
run. A baseline that
shares no scenario with the run (for example `{"scenarios": {}}`) also exits 2 ("baseline shares
no scenario with this run") and still names every key of both sides: a comparison that compares
nothing never passes. `--write-baseline` refuses (exit 2, no file written) when
the run's verdict is FAIL, including any aborted scenario, and says why on the line before the
verdict line. The baseline file is
validated before anything is measured (readable, valid JSON, a `scenarios` object, numeric
`p50_ms`, `p50_mad` and `rps` per entry, and `repetitions`, when present, a positive integer;
older baselines without `repetitions` stay valid); a malformed file exits 2 naming the offending entries,
and a missing or unreadable file exits 2 with "FILE cannot be read as a perf baseline: file not
found" (or "permission denied", "is a directory"). A scenario with no served response in the
run (n = 0) is never compared: it is listed as "not compared (no successful response)" and in
`comparison.no_success`, never as an improvement, and its errors already fail the run. With
`--write-baseline`, `--label` must be letters, digits, `.`, `-` or `_` and at most 64 characters.

## Suite self-tests

- `tests/unit/perf_suite/` (unit, plus slow tests that start a real local server with no
  corpus repositories and no provider key: guard abort, rejected login, and a run whose server
  is killed mid-measurement, which must abort as unreachable within the 20-failure bound, write
  both reports and exit 1).
- `tests/e2e/cli_remote/test_12_perf_suite_full_pass.py`, run by `./e2e-automation.sh --phase 4`
  with the embedding key from `.e2e-automation` (the test fails, naming `E2E_VOYAGE_API_KEY` and
  `VOYAGE_API_KEY`, when no key is set). It registers two tiny fixture repositories on the
  Phase 4 server, runs `run.py --quick` against a fixture corpus, requires a PASS through every
  REST and MCP door, scans every regular file of the run's artifacts and of the server data
  directory for the key (no exclusions, read in bounded chunks with overlap) and the run's own
  artifacts for the password (the seeded password equals a username the server logs), and removes the fixture
  repositories again (`DELETE /api/admin/golden-repos/{alias}`; a failed removal is reported in
  the test output, never hidden).

On a host without an interactive Claude CLI login, the Phase 4 log-audit gate may report
ERROR/WARNING entries from the description-refresh hook that runs for the newly registered
fixture repositories ("Meta description hook failed for '<alias>'", "ClaudeInvoker: non-zero exit
1", "lifecycle-runner: failed to process alias '<alias>'"). Only a run through
`./e2e-automation.sh --phase 4` on the intended host shows whether the gate passes.
