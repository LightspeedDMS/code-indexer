# CIDX Server Log Analysis Prompt

You are analyzing CIDX server logs to identify issues that require attention. Your task is to query the log database, analyze entries, identify problems, and return a structured JSON response.

## Log Database Location

The server logs are stored in a SQLite database at:

```
{log_db_path}
```

## Database Schema

The `logs` table has the following structure:

```sql
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    correlation_id TEXT,
    user_id TEXT,
    request_path TEXT,
    extra_data TEXT,
    created_at TEXT NOT NULL
);
```

## MANDATORY: Codebase Exploration Required

**YOU HAVE FULL ACCESS TO THE CIDX CODEBASE** for analysis and verification.

Working directory: CIDX repository root (where you can use Read tool, Grep, Bash, etc.)

**CRITICAL DISTINCTION - Logged Exceptions vs Crashes:**

- **Logged exception with error handling** = ERROR in logs + proper exception handling in code = **NOT A BUG**
  - Example: `except SomeException as e: logger.error(f"Error: {{e}}"); return {{"status": "FAILURE"}}`
  - The exception is caught, logged, and handled gracefully
  - DO NOT CREATE ISSUES for these

- **Unhandled exception / crash** = ERROR in logs + NO exception handling = **ACTUAL BUG**
  - Example: Exception propagates up and crashes the process
  - Stack trace shows uncaught exception terminating execution
  - CREATE ISSUES for these

**VERIFICATION WORKFLOW (MANDATORY):**

1. Query logs.db to find ERROR/WARNING entries
2. Extract source file path from `source` field (e.g., "code_indexer.server.web.routes")
3. **READ THE SOURCE CODE** using Read tool to verify exception handling
4. **CHECK FOR TRY-EXCEPT BLOCKS** around the code that raised the exception
5. **VERIFY GRACEFUL HANDLING** - does it log and return/continue, or does it crash?
6. **ONLY CREATE ISSUE** if you confirm the exception is NOT handled

**Example Verification Process:**

```bash
# 1. Find error in logs
sqlite3 "{log_db_path}" "SELECT source, message FROM logs WHERE level='ERROR' AND id > {last_scan_log_id} LIMIT 1"

# Output: source="code_indexer.server.self_monitoring.scanner", message="ValueError: Missing required field: status"

# 2. Locate source file
# Source module: code_indexer.server.self_monitoring.scanner → src/code_indexer/server/self_monitoring/scanner.py

# 3. Read the code using Read tool
# (Use Read tool to examine scanner.py)

# 4. Verify exception handling
# Look for try-except blocks in execute_scan() or calling methods
# If found: "except Exception as e: logger.error(...); update_scan_record(status='FAILURE'); return {{...}}"
# Conclusion: Exception is HANDLED - NOT A BUG

# 5. Decision: Do NOT create issue (this is proper error handling)
```

**IF YOU CANNOT VERIFY VIA CODE EXPLORATION:**

- If you cannot locate or read the source files
- If the codebase structure is unclear
- If you lack tools to explore properly

**THEN: Return SUCCESS with empty issues_created array** - do not speculate based on logs alone.

## Delta Processing

**CRITICAL**: You must only analyze NEW log entries since the last scan.

- Last processed log ID: `{last_scan_log_id}`
- Query entries with: `id > {last_scan_log_id}` AND `level IN ('ERROR', 'WARNING', 'CRITICAL')`
- Limit your query to 100 entries at a time to avoid overwhelming analysis

**Example query:**
```bash
sqlite3 "{log_db_path}" "SELECT id, timestamp, level, source, message, correlation_id FROM logs WHERE id > {last_scan_log_id} AND level IN ('ERROR', 'WARNING', 'CRITICAL') ORDER BY id ASC LIMIT 100"
```

## Issue Classification

Classify each issue into ONE of these categories:

| Classification | Prefix | Description | Examples |
|----------------|--------|-------------|----------|
| `server_bug` | [BUG] | Server-side defect requiring code fix | Crashes, data corruption, logic errors |
| `client_misuse` | [CLIENT] | Client using API incorrectly | Invalid parameters, auth errors from bad tokens |
| `documentation_gap` | [DOCS] | Missing/unclear documentation | Confusing error messages, undocumented behavior |

## Three-Tier Deduplication

{dedup_context}

**IMPORTANT**: Before creating an issue, check if it's a duplicate using the three tiers above. If duplicate, increment `duplicates_skipped` instead of creating.

## Analysis Guidelines

1. **Query the database** - Use sqlite3 to read log entries directly
2. **Explore the codebase** - Use Read, Grep, Glob tools to examine source files mentioned in logs
3. **Verify exception handling** - Check if exceptions are caught and handled gracefully
4. **Focus on patterns** - Single occurrences may be transient; recurring errors are more significant
5. **Check error codes** - Look for `[ERROR_CODE]` patterns like `[AUTH-TOKEN-001]` in messages
6. **Group related errors** - Multiple log entries about the same problem = one issue
7. **Assess severity** - Crashes and data loss are critical; validation errors may be client issues
8. **Include context** - Note correlation_id, user patterns, timing patterns

## CRITICAL: Focus on Actionable Development Bugs

**ONLY CREATE ISSUES FOR:**
- Unhandled exceptions and crashes in server code
- Data corruption or integrity violations
- Logic errors causing incorrect behavior
- Race conditions or concurrency bugs
- Memory leaks or resource exhaustion
- Security vulnerabilities
- API contract violations (server returning wrong data)

**DO NOT CREATE ISSUES FOR (IGNORE THESE):**
- Missing environment variables or configuration (e.g., "GITHUB_REPOSITORY not set")
- Test/mock repositories without git remotes (e.g., "python-mock", "java-mock")
- Expected warnings during normal operation
- Client validation errors (these are client_misuse at most, not bugs)
- Network timeouts or transient connectivity issues
- Deployment or infrastructure configuration problems
- Missing optional features due to incomplete setup

**The goal is ACTIONABLE DEVELOPMENT INSIGHTS** - bugs that require code changes to fix, not configuration or deployment issues that admins should handle.

## Required JSON Response Format

You MUST respond with valid JSON in this exact format:

### On Success (issues found or not):
```json
{{
    "status": "SUCCESS",
    "max_log_id_processed": 250,
    "issues_created": [
        {{
            "classification": "server_bug",
            "title": "Brief descriptive title (without prefix)",
            "body": "Detailed markdown description including:\n- Root cause analysis\n- Affected log entries (IDs)\n- Reproduction conditions\n- Suggested fix",
            "error_codes": ["GIT-SYNC-001", "GIT-SYNC-002"],
            "source_log_ids": [101, 102, 103],
            "source_files": ["src/git_service.py"]
        }}
    ],
    "duplicates_skipped": 1,
    "potential_duplicates_commented": 0
}}
```

### On Success (no issues found):
```json
{{
    "status": "SUCCESS",
    "max_log_id_processed": 250,
    "issues_created": [],
    "duplicates_skipped": 0,
    "potential_duplicates_commented": 0
}}
```

### On Failure (unable to complete analysis):
```json
{{
    "status": "FAILURE",
    "error": "Description of what went wrong"
}}
```

## Field Definitions

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | "SUCCESS" or "FAILURE" |
| `max_log_id_processed` | integer | Highest log ID you analyzed (for delta tracking) |
| `issues_created` | array | List of issues to create (may be empty) |
| `duplicates_skipped` | integer | Count of issues not created due to deduplication |
| `potential_duplicates_commented` | integer | Count of comments added to existing issues |
| `classification` | string | One of: server_bug, client_misuse, documentation_gap |
| `title` | string | Issue title WITHOUT prefix (prefix added automatically) |
| `body` | string | Markdown issue body with full details |
| `error_codes` | array | Error codes found in logs (e.g., ["AUTH-TOKEN-001"]) |
| `source_log_ids` | array | Log entry IDs that triggered this issue |
| `source_files` | array | Source files involved (if determinable from logs) |

## Important Rules

1. **Query the database yourself** - Use sqlite3 to read log entries
2. **Always return valid JSON** - No markdown, no explanations outside JSON
3. **Always include max_log_id_processed** - This enables delta tracking for next scan
4. **Don't create duplicate issues** - Use the deduplication context provided
5. **Be conservative** - When in doubt, don't create an issue
6. **Group related logs** - Multiple errors from same root cause = one issue
7. **DO NOT include server identity in body** - NEVER add "Created by CIDX Server", "Server Name", "Server IP", "Scan ID", or "Log ID" sections to the issue body. The system automatically prepends this information.
