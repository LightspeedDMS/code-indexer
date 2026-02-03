"""
Tests for self-monitoring scanner (Story #73).

Tests Claude prompt assembly, log delta tracking, issue classification,
and three-tier deduplication algorithm.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from code_indexer.server.self_monitoring.scanner import LogScanner
from code_indexer.server.storage.database_manager import DatabaseSchema


# SQL for logs table schema (used in fixtures)
LOGS_TABLE_SCHEMA = """
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
)
"""


# Module-level shared fixtures
@pytest.fixture
def temp_logs_db():
    """Create temporary logs database with schema for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.execute(LOGS_TABLE_SCHEMA)
    conn.commit()
    conn.close()

    yield db_path

    # Guaranteed cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_db():
    """Create temporary database with schema (shared across all tests)."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test.db"

    try:
        schema = DatabaseSchema(db_path=str(db_path))
        schema.initialize_database()
        yield str(db_path)
    finally:
        db_path.unlink(missing_ok=True)
        Path(temp_dir).rmdir()


@pytest.fixture
def scanner(temp_db):
    """Create scanner instance (shared across all tests)."""
    return LogScanner(
        db_path=temp_db,
        scan_id="scan-123",
        github_repo="org/repo",
        log_db_path="/path/to/logs.db",
        prompt_template="Analyze logs from {log_db_path} where id > {last_scan_log_id}. Context: {dedup_context}",
    )


class TestLogScannerInit:
    """Test scanner initialization and configuration."""

    def test_scanner_initialization(self, temp_db):
        """Test that scanner initializes with required parameters."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Analyze logs: {context}",
        )

        assert scanner.db_path == temp_db
        assert scanner.scan_id == "scan-123"
        assert scanner.github_repo == "org/repo"
        assert scanner.log_db_path == "/path/to/logs.db"
        assert scanner.prompt_template == "Analyze logs: {context}"

    def test_scanner_accepts_github_token_and_server_name(self, temp_db):
        """Test scanner accepts github_token and server_name parameters (Bug #87 - Issue #2)."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Analyze logs: {context}",
            github_token="ghp_test_token",
            server_name="Test Server",
        )

        assert scanner.github_token == "ghp_test_token"
        assert scanner.server_name == "Test Server"

    def test_scanner_passes_github_token_to_issue_manager(self, temp_db):
        """Test scanner stores github_token and server_name for passing to IssueManager (Bug #87 - Issue #2)."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-test",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token="ghp_token_123",
            server_name="Production Server",
        )

        # Verify scanner stores the parameters that will be passed to IssueManager
        assert scanner.github_token == "ghp_token_123"
        assert scanner.server_name == "Production Server"

        # Full integration test (service -> scanner -> issue_manager) is in test_service.py


class TestPromptAssembly:
    """Test Claude prompt assembly (AC2).

    The prompt tells Claude where the database is located so Claude can query
    it directly. This keeps prompts small and lets Claude read only what it needs.
    """

    def test_assemble_prompt_includes_log_db_path(self, scanner):
        """Test that prompt includes the log database path for Claude to query."""
        prompt = scanner.assemble_prompt(last_scan_log_id=0, existing_issues=[])

        # Prompt should include the database path so Claude can query it directly
        assert "/path/to/logs.db" in prompt

    def test_assemble_prompt_includes_last_scan_log_id(self, scanner):
        """Test that prompt includes last_scan_log_id for delta tracking."""
        prompt = scanner.assemble_prompt(last_scan_log_id=100, existing_issues=[])

        assert "100" in prompt

    def test_assemble_prompt_includes_existing_issues(self, scanner):
        """Test that prompt includes existing issues for duplicate checking."""
        existing_issues = [
            {"number": 101, "title": "[BUG] Auth failure", "labels": ["bug"]},
            {"number": 102, "title": "[CLIENT] Invalid request", "labels": ["client"]},
        ]

        prompt = scanner.assemble_prompt(
            last_scan_log_id=100, existing_issues=existing_issues
        )

        assert "101" in prompt
        assert "[BUG] Auth failure" in prompt
        assert "102" in prompt
        assert "[CLIENT] Invalid request" in prompt

    def test_assemble_prompt_uses_template(self, scanner):
        """Test that prompt uses the configured template."""
        prompt = scanner.assemble_prompt(last_scan_log_id=100, existing_issues=[])

        # Template includes "Analyze logs from"
        assert "Analyze logs" in prompt


class TestDeduplicationContext:
    """Test deduplication context assembly (AC5b)."""

    def test_assemble_dedup_context_includes_open_issues(self, scanner):
        """Test that deduplication context includes open GitHub issues."""
        existing_issues = [
            {
                "number": 101,
                "title": "[BUG] Auth failure",
                "body": "Error during authentication...",
                "labels": ["bug"],
                "created_at": "2026-01-20T10:00:00Z",
            }
        ]

        context = scanner.assemble_dedup_context(existing_issues=existing_issues)

        assert "101" in context
        assert "[BUG] Auth failure" in context
        assert "Error during authentication" in context

    def test_assemble_dedup_context_includes_fingerprints(self, scanner, temp_db):
        """Test that deduplication context includes stored fingerprints."""
        # Store a scan and issue with fingerprint
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-previous", "2026-01-20T10:00:00", "SUCCESS", 1, 50),
            )
            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "title, error_codes, fingerprint, source_log_ids, source_files, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "scan-previous",
                    101,
                    "https://github.com/org/repo/issues/101",
                    "server_bug",
                    "[BUG] Test issue",
                    "GIT-SYNC-001",
                    "abc123def456",
                    "1,2,3",
                    "src/auth.py",
                    "2026-01-20T10:05:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        context = scanner.assemble_dedup_context(existing_issues=[])

        # Should include fingerprint from database
        assert "abc123def456" in context

    def test_assemble_dedup_context_instructions(self, scanner):
        """Test that deduplication context includes tier instructions."""
        context = scanner.assemble_dedup_context(existing_issues=[])

        # Should include instructions for three-tier algorithm
        assert "Tier 1" in context or "error code" in context.lower()
        assert "Tier 2" in context or "fingerprint" in context.lower()
        assert "Tier 3" in context or "semantic" in context.lower()


class TestScanRecordManagement:
    """Test scan record creation and management (Bug #87)."""

    def test_create_scan_record_inserts_initial_record(self, scanner, temp_db):
        """Test create_scan_record creates initial scan record with RUNNING status (Bug #87 - Issue #5)."""
        # Call create_scan_record
        scanner.create_scan_record(log_id_start=100)

        # Verify record was created
        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT scan_id, status, log_id_start, log_id_end, issues_created FROM self_monitoring_scans WHERE scan_id = ?",
                (scanner.scan_id,),
            ).fetchone()

            assert row is not None
            assert row[0] == scanner.scan_id
            assert row[1] == "RUNNING"
            assert row[2] == 100  # log_id_start
            assert row[3] is None  # log_id_end (not set yet)
            assert row[4] == 0  # issues_created
        finally:
            conn.close()

    def test_create_scan_record_sets_timestamp(self, scanner, temp_db):
        """Test create_scan_record sets started_at timestamp (Bug #87 - Issue #5)."""
        import datetime

        before = datetime.datetime.utcnow()
        scanner.create_scan_record(log_id_start=50)
        after = datetime.datetime.utcnow()

        # Verify timestamp is within reasonable range
        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT started_at FROM self_monitoring_scans WHERE scan_id = ?",
                (scanner.scan_id,),
            ).fetchone()

            assert row is not None
            started_at = datetime.datetime.fromisoformat(row[0])
            assert before <= started_at <= after
        finally:
            conn.close()


class TestLogDeltaTracking:
    """Test log delta tracking (AC3)."""

    def test_get_last_scan_log_id_no_previous_scans(self, scanner):
        """Test get_last_scan_log_id returns 0 when no previous scans."""
        last_log_id = scanner.get_last_scan_log_id()
        assert last_log_id == 0

    def test_get_last_scan_log_id_returns_previous_end(self, scanner, temp_db):
        """Test get_last_scan_log_id returns log_id_end from last SUCCESS."""
        # Insert successful scan
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-001", "2026-01-20T10:00:00", "SUCCESS", 0, 100),
            )
            conn.commit()
        finally:
            conn.close()

        last_log_id = scanner.get_last_scan_log_id()
        assert last_log_id == 100

    def test_get_last_scan_log_id_ignores_failed_scans(self, scanner, temp_db):
        """Test get_last_scan_log_id ignores FAILURE scans."""
        # Insert successful and failed scans
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-001", "2026-01-20T10:00:00", "SUCCESS", 0, 100),
            )
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-002", "2026-01-20T11:00:00", "FAILURE", 100, None),
            )
            conn.commit()
        finally:
            conn.close()

        # Should return 100 from last SUCCESS, not None from FAILURE
        last_log_id = scanner.get_last_scan_log_id()
        assert last_log_id == 100

    def test_update_scan_record_success(self, scanner, temp_db):
        """Test update_scan_record sets log_id_end on SUCCESS."""
        # Insert running scan
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start) "
                "VALUES (?, ?, ?, ?)",
                ("scan-123", "2026-01-20T10:00:00", "running", 100),
            )
            conn.commit()
        finally:
            conn.close()

        # Update to SUCCESS with log_id_end
        scanner.update_scan_record(status="SUCCESS", log_id_end=200, issues_created=3)

        # Verify update
        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT status, log_id_end, issues_created FROM self_monitoring_scans WHERE scan_id = ?",
                ("scan-123",),
            ).fetchone()
            assert row[0] == "SUCCESS"
            assert row[1] == 200
            assert row[2] == 3
        finally:
            conn.close()

    def test_update_scan_record_failure_preserves_log_id_end(self, scanner, temp_db):
        """Test update_scan_record does NOT advance log_id_end on FAILURE."""
        # Insert running scan
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-123", "2026-01-20T10:00:00", "running", 100, None),
            )
            conn.commit()
        finally:
            conn.close()

        # Update to FAILURE without log_id_end
        scanner.update_scan_record(status="FAILURE", error_message="Claude CLI error")

        # Verify log_id_end is still None
        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT status, log_id_end FROM self_monitoring_scans WHERE scan_id = ?",
                ("scan-123",),
            ).fetchone()
            assert row[0] == "FAILURE"
            assert row[1] is None
        finally:
            conn.close()


class TestClaudeResponseParsing:
    """Test Claude response parsing (AC6)."""

    def test_parse_claude_response_success(self, scanner):
        """Test parsing valid SUCCESS response."""
        response_json = {
            "status": "SUCCESS",
            "max_log_id_processed": 250,
            "issues_created": [
                {"number": 101, "classification": "server_bug"},
                {"number": 102, "classification": "client_misuse"},
            ],
            "duplicates_skipped": 1,
            "potential_duplicates_commented": 0,
        }

        result = scanner.parse_claude_response(json.dumps(response_json))

        assert result["status"] == "SUCCESS"
        assert result["max_log_id_processed"] == 250
        assert len(result["issues_created"]) == 2
        assert result["duplicates_skipped"] == 1
        assert result["potential_duplicates_commented"] == 0

    def test_parse_claude_response_failure(self, scanner):
        """Test parsing FAILURE response."""
        response_json = {"status": "FAILURE", "error": "Database connection timeout"}

        result = scanner.parse_claude_response(json.dumps(response_json))

        assert result["status"] == "FAILURE"
        assert "timeout" in result["error"].lower()

    def test_parse_claude_response_invalid_json(self, scanner):
        """Test parsing invalid JSON raises ValueError."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            scanner.parse_claude_response("not valid json {{{")

    def test_parse_claude_response_missing_status(self, scanner):
        """Test parsing response without status raises ValueError."""
        response_json = {"max_log_id_processed": 100}

        with pytest.raises(ValueError, match="Missing required field: status"):
            scanner.parse_claude_response(json.dumps(response_json))


class TestIssueClassification:
    """Test issue classification prefixes (AC4)."""

    def test_get_issue_prefix_server_bug(self, scanner):
        """Test server_bug gets [BUG] prefix."""
        prefix = scanner.get_issue_prefix("server_bug")
        assert prefix == "[BUG]"

    def test_get_issue_prefix_client_misuse(self, scanner):
        """Test client_misuse gets [CLIENT] prefix."""
        prefix = scanner.get_issue_prefix("client_misuse")
        assert prefix == "[CLIENT]"

    def test_get_issue_prefix_documentation_gap(self, scanner):
        """Test documentation_gap gets [DOCS] prefix."""
        prefix = scanner.get_issue_prefix("documentation_gap")
        assert prefix == "[DOCS]"

    def test_get_issue_prefix_unknown_raises_error(self, scanner):
        """Test unknown classification raises ValueError."""
        with pytest.raises(ValueError, match="Unknown classification"):
            scanner.get_issue_prefix("unknown_type")


class TestFetchExistingIssues:
    """Test _fetch_existing_github_issues implementation (Bug #87)."""

    def test_fetch_existing_github_issues_calls_github_api(self, temp_db):
        """Test _fetch_existing_github_issues uses GitHub REST API to fetch open issues (Bug #87 - Issue #7)."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token="ghp_token_123",
        )

        # Mock GitHub API response
        api_response = [
            {
                "number": 101,
                "title": "[BUG] Test issue",
                "body": "Test body",
                "labels": [{"name": "bug"}, {"name": "self-monitoring"}],
                "created_at": "2026-01-20T10:00:00Z",
            },
            {
                "number": 102,
                "title": "[CLIENT] Client error",
                "body": "Client body",
                "labels": [{"name": "client"}],
                "created_at": "2026-01-21T10:00:00Z",
            },
        ]

        with patch("code_indexer.server.self_monitoring.scanner.httpx.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: api_response,
                headers={},
                text=json.dumps(api_response),
                raise_for_status=lambda: None,
            )

            issues = scanner._fetch_existing_github_issues()

            # Verify GitHub API was called correctly
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args[1]

            # Verify URL
            call_url = mock_get.call_args[0][0]
            assert "https://api.github.com/repos/org/repo/issues" == call_url

            # Verify headers include Bearer token
            assert "headers" in call_kwargs
            assert "Authorization" in call_kwargs["headers"]
            assert call_kwargs["headers"]["Authorization"] == "Bearer ghp_token_123"

            # Verify params include state=open
            assert "params" in call_kwargs
            assert call_kwargs["params"]["state"] == "open"

            # Verify returned issues are correctly formatted
            assert len(issues) == 2
            assert issues[0]["number"] == 101
            assert issues[0]["title"] == "[BUG] Test issue"
            assert issues[0]["labels"] == ["bug", "self-monitoring"]
            assert issues[1]["number"] == 102

    def test_fetch_existing_github_issues_returns_empty_on_http_error(self, temp_db):
        """Test _fetch_existing_github_issues returns empty list on HTTP error (Bug #87 - Issue #7)."""
        import httpx

        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token="ghp_token_123",
        )

        with patch("code_indexer.server.self_monitoring.scanner.httpx.get") as mock_get:
            # Create mock response object for HTTPStatusError
            mock_response = MagicMock()
            mock_response.status_code = 500

            # Create proper HTTPStatusError
            def raise_http_error():
                raise httpx.HTTPStatusError(
                    "Server error", request=MagicMock(), response=mock_response
                )

            mock_response.raise_for_status = raise_http_error
            mock_get.return_value = mock_response

            issues = scanner._fetch_existing_github_issues()

            # Should return empty list on error
            assert issues == []

    def test_fetch_existing_github_issues_returns_empty_without_token(self, temp_db):
        """Test _fetch_existing_github_issues returns empty list when no token (Bug #87 - Issue #7)."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token=None,  # No token
        )

        issues = scanner._fetch_existing_github_issues()

        # Should return empty list without token
        assert issues == []

    def test_fetch_existing_github_issues_handles_rate_limiting(self, temp_db):
        """Test _fetch_existing_github_issues handles GitHub API rate limiting (Bug #87 - Issue #7)."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token="ghp_token_123",
        )

        with patch("code_indexer.server.self_monitoring.scanner.httpx.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=403,
                headers={"X-RateLimit-Remaining": "0"},
                raise_for_status=lambda: None,
            )

            issues = scanner._fetch_existing_github_issues()

            # Should return empty list on rate limit
            assert issues == []

    def test_fetch_existing_github_issues_handles_timeout(self, temp_db):
        """Test _fetch_existing_github_issues handles request timeout (Bug #87 - Issue #7)."""
        import httpx

        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/path/to/logs.db",
            prompt_template="Test",
            github_token="ghp_token_123",
        )

        with patch("code_indexer.server.self_monitoring.scanner.httpx.get") as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Request timed out")

            issues = scanner._fetch_existing_github_issues()

            # Should return empty list on timeout
            assert issues == []


class TestExecuteScan:
    """Test execute_scan orchestration method."""

    def test_execute_scan_success_creates_issues(self, temp_db, temp_logs_db):
        """Test successful scan creates issues via IssueManager."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Create scanner with real logs database
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-test",
            github_repo="org/repo",
            log_db_path=temp_logs_db,
            prompt_template="Analyze logs from {log_db_path} where id > {last_scan_log_id}. Context: {dedup_context}",
        )

        # No need to manually insert scan record - execute_scan creates it automatically (Bug #87 issue #6)

        # Mock Claude CLI response
        claude_response = {
            "status": "SUCCESS",
            "max_log_id_processed": 150,
            "issues_created": [
                {
                    "number": 101,
                    "classification": "server_bug",
                    "title": "Auth failure",
                    "body": "...",
                    "error_codes": ["AUTH-001"],
                    "source_log_ids": [101, 102],
                    "source_files": ["src/auth.py"],
                }
            ],
            "duplicates_skipped": 0,
            "potential_duplicates_commented": 0,
        }

        # Mock subprocess call to Claude CLI
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(claude_response), stderr=""
            )

            # Mock IssueManager.create_issue - returns dict with github_issue_number
            with patch.object(IssueManager, "create_issue") as mock_create:
                mock_create.return_value = {
                    "github_issue_number": 101,
                    "github_issue_url": "https://github.com/org/repo/issues/101",
                    "classification": "server_bug",
                }

                # Execute scan
                result = scanner.execute_scan()

                # Verify scan completed successfully
                assert result["status"] == "SUCCESS"
                assert result["issues_created"] == 1
                assert result["duplicates_skipped"] == 0

                # Verify scan record updated
                conn = sqlite3.connect(temp_db)
                try:
                    row = conn.execute(
                        "SELECT status, log_id_end, issues_created FROM self_monitoring_scans WHERE scan_id = ?",
                        ("scan-test",),
                    ).fetchone()
                    assert row[0] == "SUCCESS"
                    assert row[1] == 150
                    assert row[2] == 1
                finally:
                    conn.close()

    def test_execute_scan_failure_handles_claude_error(self, temp_db, temp_logs_db):
        """Test failed scan handles Claude CLI errors gracefully."""
        scanner = LogScanner(
            db_path=temp_db,
            scan_id="scan-fail",
            github_repo="org/repo",
            log_db_path=temp_logs_db,
            prompt_template="Analyze logs from {log_db_path} where id > {last_scan_log_id}. Context: {dedup_context}",
        )

        # No need to manually insert scan record - execute_scan creates it automatically (Bug #87 issue #6)

        # Mock subprocess failure
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Claude CLI error: timeout"
            )

            # Execute scan
            result = scanner.execute_scan()

            # Verify scan failed
            assert result["status"] == "FAILURE"
            assert "timeout" in result["error"].lower()

            # Verify scan record updated with FAILURE
            conn = sqlite3.connect(temp_db)
            try:
                row = conn.execute(
                    "SELECT status, log_id_end FROM self_monitoring_scans WHERE scan_id = ?",
                    ("scan-fail",),
                ).fetchone()
                assert row[0] == "FAILURE"
                assert row[1] is None  # log_id_end NOT advanced on failure
            finally:
                conn.close()
