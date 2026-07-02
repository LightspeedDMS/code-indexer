# AC-V4-14 Manual E2E Test Plan — Story #885 Lifecycle Schema v4 Gate

## 1. Purpose

Verify that 7 fabricated local golden repos produce exact Lifecycle Schema v4 output per the
evidence-grounded expected-output table below. Each fixture directory under
`tests/fixtures/e2e_v4/` is registered as a local golden repo, its lifecycle analysis job
runs to completion, and the resulting cidx-meta frontmatter is validated field-by-field.

## 2. Prerequisites

- Local CIDX server running on localhost:8000 (admin/admin credentials)
- VoyageAI API key configured in server settings (required for semantic indexing)
- Claude CLI authenticated (`claude --version` succeeds, subprocess invocation works)
- Git available on PATH (`git --version` succeeds)
- `sqlite3` available for log audit step

## 3. Setup Procedure

For each of the 7 fixture directories listed in the table below, execute:

### Step 1: Copy fixture to a fresh temp location (avoids polluting the source tree)

```bash
ALIAS=<alias>
STAMP=$(date +%s)
TMPDIR="/tmp/e2e_v4_${ALIAS}_${STAMP}"
cp -r "tests/fixtures/e2e_v4/${ALIAS}/" "${TMPDIR}/"
```

### Step 2: Initialize a bare git repo so CIDX can clone it

```bash
cd "${TMPDIR}"
git init
git add .
git commit -m "fixture"
```

### Step 3: Register as a local golden repo

Via MCP `add_golden_repo` tool (preferred) or Web UI Add Golden Repo form:

```
repo_url:    file:///tmp/e2e_v4_<alias>_<stamp>/
alias:       <alias>
description: AC-V4-14 E2E fixture for <alias>
```

### Step 4: Poll job status until lifecycle analysis completes

Use `get_job_details(job_id=<job_id_from_registration>)` or the Jobs dashboard.
Wait until `status == "completed"` before proceeding to verification.

Repeat Steps 1-4 for all 7 aliases:
- test-kustomize-app
- test-terraform-workspace
- test-helm-ci
- k8s-test-platform
- test-cross-repo-app
- test-cross-repo-infra
- test-no-evidence

Note: test-cross-repo-app must be registered BEFORE test-cross-repo-infra so the cross-repo
lookup can resolve the app alias when the infra repo is analyzed.

## 4. Expected Output Table

After each job completes, the cidx-meta file for the alias is validated against:

| Alias | ci.environments (exact, order-insensitive) | branch_environment_map (exact) |
|---|---|---|
| test-kustomize-app | {"dev","stage","prod"} | {} or omitted |
| test-terraform-workspace | {"dev","stage","prod"} | {} or omitted |
| test-helm-ci | {"dev","stage","prod"} | {"dev":"dev","stage":"stage","prod":"prod"} |
| k8s-test-platform | {"dev","stage","prod"} | {} or omitted |
| test-cross-repo-app | {"dev","prod"} | {} or omitted |
| test-cross-repo-infra | {"dev","prod"} | {} or omitted |
| test-no-evidence | null | {} or omitted |

## 5. Verification Steps

For each alias after its registration job completes:

### Step 5a: Locate the cidx-meta file

The cidx-meta file is written to the server data directory. Default path:

```bash
META_FILE=~/.cidx-server/data/cidx-meta/<alias>.md
# If CIDX_DATA_DIR is set: $CIDX_DATA_DIR/cidx-meta/<alias>.md
```

### Step 5b: Parse and assert frontmatter

```python
import yaml

with open(meta_file) as f:
    raw = f.read()

# Extract YAML frontmatter between --- delimiters
fm_text = raw.split("---")[1]
fm = yaml.safe_load(fm_text)

# Assert schema version
assert fm["lifecycle_schema_version"] == 4, f"Expected schema v4, got {fm.get('lifecycle_schema_version')}"

# Assert ci.environments (order-insensitive set comparison)
expected_envs = <see table above>   # None for test-no-evidence
actual_envs = fm.get("ci", {}).get("environments")
if expected_envs is None:
    assert actual_envs is None, f"Expected null environments, got {actual_envs}"
else:
    assert set(actual_envs) == set(expected_envs), \
        f"environments mismatch: expected {expected_envs}, got {actual_envs}"

# Assert branch_environment_map
expected_bem = <see table above>    # {} or omitted for most; specific map for test-helm-ci
actual_bem = fm.get("branch_environment_map") or {}
assert actual_bem == expected_bem, \
    f"branch_environment_map mismatch: expected {expected_bem}, got {actual_bem}"
```

### Step 5c: Assert no SchemaValidationError in job output

Check job details via `get_job_details(job_id=...)` — the `result` field must NOT contain
"SchemaValidationError" or "validation_error".

## 6. Log Audit

Record the timestamp at the start of the test run (`TEST_START`). After all 7 jobs complete,
query the server log database for new errors and warnings:

```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs
   WHERE level IN ('ERROR','WARNING')
   AND timestamp > '${TEST_START}'
   AND (source LIKE '%lifecycle%' OR source LIKE '%global_repos%' OR source LIKE '%scanner%')
   ORDER BY timestamp DESC LIMIT 100;"
```

Expected result: zero rows attributable to the AC-V4-14 test batch.

Any ERROR or WARNING that names a lifecycle, global_repos, or scanner source and post-dates
TEST_START is a blocking failure — investigate root cause before marking the test passed.

## 7. Teardown

After all assertions pass, clean up:

```bash
# Remove golden repos (via MCP or Web UI)
for ALIAS in test-kustomize-app test-terraform-workspace test-helm-ci k8s-test-platform \
             test-cross-repo-app test-cross-repo-infra test-no-evidence; do
    # remove_golden_repo(alias=<alias>)
    echo "Remove: ${ALIAS}"
done

# Remove temp directories
rm -rf /tmp/e2e_v4_*
```

## 8. Implicit A10 Validation

AC-V4-2 (cross-repo discovery) passing for test-cross-repo-app implicitly validates that A10
MCP self-registration worked correctly. The lifecycle analyzer invokes a Claude CLI subprocess
which must be able to call the cidx-local MCP tool to resolve cross-repo references. If
test-cross-repo-app produces null or {"dev","stage","prod"} instead of {"dev","prod"}, suspect
A10 MCP registration first:

1. Confirm `cidx-local` MCP server is registered in Claude CLI's MCP config
2. Confirm the subprocess invocation path in `invoke_claude_cli` (A10 centralization) is active
3. Check server logs for "mcp" source errors around the time of the test-cross-repo-app job

Cross-repo lookup failing silently degrades to direct-evidence-only analysis (which would yield
null for test-cross-repo-app since it has no direct deploy wiring).

## 9. Pass/Fail Criteria

PASS: All 7 aliases produce the exact expected ci.environments and branch_environment_map,
lifecycle_schema_version == 4 in all 7 files, zero new ERROR/WARNING log entries attributable
to this batch, no SchemaValidationError in any job result.

FAIL: Any deviation from the expected output table, any new ERROR/WARNING log entry, any
SchemaValidationError, or any job that does not reach "completed" status within 10 minutes.
