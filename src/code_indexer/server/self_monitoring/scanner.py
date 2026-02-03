"""
Log Scanner for Self-Monitoring (Story #73).

Assembles Claude prompts for log analysis, handles issue classification,
and implements three-tier deduplication algorithm.
"""

import datetime
import json
import logging
import os
import sqlite3
import subprocess
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Constants for deduplication context
BODY_PREVIEW_MAX_LENGTH = 500
FINGERPRINT_RETENTION_DAYS = 90

# Constants for Claude CLI invocation
CLAUDE_CLI_TIMEOUT_SECONDS = 1800  # 30 minute timeout

# Constants for GitHub issue fetching (Bug #87)
GITHUB_ISSUE_FETCH_LIMIT = 100  # Fetch last 100 open issues for deduplication
GITHUB_CLI_TIMEOUT_SECONDS = 30  # Reasonable timeout for GitHub API calls

# JSON Schema for structured Claude output (forces valid JSON response)
CLAUDE_RESPONSE_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["SUCCESS", "FAILURE"]},
            "max_log_id_processed": {"type": "integer"},
            "issues_created": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "classification": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "error_codes": {"type": "array", "items": {"type": "string"}},
                        "source_log_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "source_files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["classification", "title", "body"],
                },
            },
            "duplicates_skipped": {"type": "integer"},
            "potential_duplicates_commented": {"type": "integer"},
            "error": {"type": "string"},
        },
        "required": ["status"],
    }
)


class LogScanner:
    """
    Scanner for analyzing server logs with Claude CLI.

    Assembles prompts with log database context, existing issues for deduplication,
    and coordinates issue creation through IssueManager.

    Args:
        db_path: Path to self-monitoring SQLite database
        scan_id: Unique identifier for current scan
        github_repo: GitHub repository in format "owner/repo"
        log_db_path: Path to logs.db containing server logs
        prompt_template: Template string for Claude prompt
    """

    def __init__(
        self,
        db_path: str,
        scan_id: str,
        github_repo: str,
        log_db_path: str,
        prompt_template: str,
        model: str = "opus",
        repo_root: Optional[str] = None,
        github_token: Optional[str] = None,
        server_name: Optional[str] = None,
    ):
        """
        Initialize LogScanner.

        Args:
            db_path: Path to self-monitoring database
            scan_id: Current scan identifier
            github_repo: GitHub repository (owner/repo)
            log_db_path: Path to logs database
            prompt_template: Claude prompt template
            model: Claude model to use (opus or sonnet, default: opus) - Story #76 AC5
            repo_root: Path to repo root for Claude working directory
            github_token: GitHub token for authentication (optional, Bug #87)
            server_name: Server display name for issue identification (optional, Bug #87)
        """
        self.db_path = db_path
        self.scan_id = scan_id
        self.github_repo = github_repo
        self.log_db_path = log_db_path
        self.prompt_template = prompt_template
        self.model = model
        self.repo_root = repo_root
        self.github_token = github_token
        self.server_name = server_name

    def assemble_prompt(
        self, last_scan_log_id: int, existing_issues: List[Dict]
    ) -> str:
        """
        Assemble Claude prompt for log analysis (AC2).

        The prompt tells Claude where the log database is located so Claude
        can query it directly using sqlite3. This keeps prompts small and
        lets Claude read only what it needs.

        Args:
            last_scan_log_id: Last processed log ID for delta tracking
            existing_issues: List of existing GitHub issues for deduplication

        Returns:
            Complete Claude prompt string
        """
        # Assemble deduplication context
        dedup_context = self.assemble_dedup_context(existing_issues=existing_issues)

        # Format template - pass database path so Claude can query directly
        prompt = self.prompt_template.format(
            log_db_path=self.log_db_path,
            last_scan_log_id=last_scan_log_id,
            dedup_context=dedup_context,
        )

        return prompt

    def assemble_dedup_context(self, existing_issues: List[Dict]) -> str:
        """
        Assemble deduplication context for Claude (AC5b).

        Includes:
        - Open GitHub issues with [BUG], [CLIENT], [DOCS] prefixes
        - Stored fingerprints from self_monitoring_issues (last 90 days)
        - Three-tier deduplication instructions

        Args:
            existing_issues: List of open GitHub issues

        Returns:
            Deduplication context string
        """
        context_parts = []

        # Add three-tier algorithm instructions
        context_parts.extend(self._build_dedup_instructions())

        # Add existing open issues
        if existing_issues:
            context_parts.extend(self._format_existing_issues(existing_issues))

        # Add stored fingerprints from database
        context_parts.extend(self._fetch_stored_fingerprints())

        return "\n".join(context_parts)

    def _build_dedup_instructions(self) -> List[str]:
        """
        Build three-tier deduplication algorithm instructions.

        Returns:
            List of instruction lines
        """
        return [
            "# Deduplication Algorithm (Three-Tier)",
            "",
            "## Tier 1: Error Code Match (Exact)",
            "- Extract [ERROR_CODE] from log entries (e.g., [GIT-SYNC-001])",
            "- Check if ANY existing open issue title contains the same code",
            "- If match found: SKIP creation, increment duplicates_skipped",
            "",
            "## Tier 2: Fingerprint Match (Structural)",
            "- If no Tier 1 match, compute: hash(classification + source_file + error_type)",
            "- Check against stored fingerprints below",
            "- If match found: SKIP creation, increment duplicates_skipped",
            "",
            "## Tier 3: Semantic Similarity (Fallback)",
            "- If no Tier 1/2 match, compare against existing issues",
            "- Normalize messages (remove IDs, timestamps, paths)",
            "- If >85% similar on 3+ attributes: ADD COMMENT instead of creating",
            "- Increment potential_duplicates_commented",
            "",
        ]

    def _format_existing_issues(self, existing_issues: List[Dict]) -> List[str]:
        """
        Format existing GitHub issues for deduplication context.

        Args:
            existing_issues: List of open GitHub issues

        Returns:
            List of formatted issue lines
        """
        lines = ["# Existing Open Issues", ""]

        for issue in existing_issues:
            title = issue.get("title", "")
            number = issue.get("number", "")
            body = issue.get("body", "")
            labels = issue.get("labels", [])
            created_at = issue.get("created_at", "")

            # Truncate body to avoid context overflow
            body_preview = body[:BODY_PREVIEW_MAX_LENGTH] if body else ""

            lines.append(f"Issue #{number}: {title}")
            lines.append(f"  Labels: {', '.join(labels)}")
            lines.append(f"  Created: {created_at}")
            if body_preview:
                lines.append(f"  Body: {body_preview}...")
            lines.append("")

        return lines

    def _fetch_stored_fingerprints(self) -> List[str]:
        """
        Fetch and format stored fingerprints from database.

        Returns:
            List of formatted fingerprint lines
        """
        lines = []

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT fingerprint, classification, error_codes, title, created_at "
                "FROM self_monitoring_issues "
                "WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days') "
                "ORDER BY created_at DESC",
                (FINGERPRINT_RETENTION_DAYS,),
            )

            fingerprints = cursor.fetchall()

            if fingerprints:
                lines.append(
                    f"# Stored Fingerprints (Last {FINGERPRINT_RETENTION_DAYS} Days)"
                )
                lines.append("")

                for fp_row in fingerprints:
                    fingerprint, classification, error_codes, title, created_at = fp_row
                    lines.append(f"Fingerprint: {fingerprint}")
                    lines.append(f"  Classification: {classification}")
                    if error_codes:
                        lines.append(f"  Error Codes: {error_codes}")
                    lines.append(f"  Title: {title}")
                    lines.append(f"  Created: {created_at}")
                    lines.append("")

        finally:
            conn.close()

        return lines

    def create_scan_record(self, log_id_start: int) -> None:
        """
        Create initial scan record in database (Bug #87 issue #5).

        Args:
            log_id_start: Starting log ID for this scan
        """
        logger.debug(
            f"[SELF-MON-DEBUG] create_scan_record: Entry - scan_id={self.scan_id}, log_id_start={log_id_start}, db_path={self.db_path}"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            logger.debug(
                "[SELF-MON-DEBUG] create_scan_record: Executing INSERT into self_monitoring_scans"
            )
            # Include log_id_end with initial value to handle databases with NOT NULL constraint
            # (schema migration issue - older databases may have NOT NULL on log_id_end)
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.scan_id,
                    datetime.datetime.utcnow().isoformat(),
                    "RUNNING",
                    log_id_start,
                    log_id_start,
                    0,
                ),
            )
            conn.commit()
            logger.debug(
                "[SELF-MON-DEBUG] create_scan_record: INSERT committed successfully"
            )
        finally:
            conn.close()

    def get_last_scan_log_id(self) -> int:
        """
        Get the last successfully processed log ID for delta tracking (AC3).

        Returns the log_id_end from the most recent SUCCESS scan, or 0 if no
        successful scans exist. Failed scans are ignored to ensure retry from
        the same position.

        Returns:
            Last processed log ID, or 0 if no previous scans
        """
        logger.debug(
            f"[SELF-MON-DEBUG] get_last_scan_log_id: Entry - db_path={self.db_path}"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            logger.debug(
                "[SELF-MON-DEBUG] get_last_scan_log_id: Executing SELECT query for last successful scan"
            )
            cursor = conn.execute(
                "SELECT log_id_end FROM self_monitoring_scans "
                "WHERE status = 'SUCCESS' AND log_id_end IS NOT NULL "
                "ORDER BY started_at DESC "
                "LIMIT 1"
            )
            row = cursor.fetchone()
            result = row[0] if row else 0
            logger.debug(
                f"[SELF-MON-DEBUG] get_last_scan_log_id: Query complete - result={result}, has_row={row is not None}"
            )
            return result
        finally:
            conn.close()

    def update_scan_record(
        self,
        status: str,
        log_id_end: Optional[int] = None,
        issues_created: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Update scan record with completion status and metrics (AC3).

        On SUCCESS: Updates status, log_id_end, and issues_created.
        On FAILURE: Updates status and error_message, preserves log_id_end
                   as None to allow retry from same position.

        Args:
            status: Scan completion status ("SUCCESS" or "FAILURE")
            log_id_end: Last processed log ID (None for FAILURE)
            issues_created: Number of issues created (None for FAILURE)
            error_message: Error description (only for FAILURE)
        """
        completed_at = datetime.datetime.utcnow().isoformat()

        conn = sqlite3.connect(self.db_path)
        try:
            # Build dynamic UPDATE based on provided fields
            update_fields = ["status = ?", "completed_at = ?"]
            update_values = [status, completed_at]

            if log_id_end is not None:
                update_fields.append("log_id_end = ?")
                update_values.append(log_id_end)

            if issues_created is not None:
                update_fields.append("issues_created = ?")
                update_values.append(issues_created)

            if error_message is not None:
                update_fields.append("error_message = ?")
                update_values.append(error_message)

            update_values.append(self.scan_id)

            query = (
                f"UPDATE self_monitoring_scans "
                f"SET {', '.join(update_fields)} "
                f"WHERE scan_id = ?"
            )

            conn.execute(query, update_values)
            conn.commit()
        finally:
            conn.close()

    def parse_claude_response(self, response_str: str) -> Dict:
        """
        Parse Claude JSON response for log analysis (AC6).

        The Claude CLI with --output-format json returns a wrapper structure:
        {
            "type": "result",
            "result": "<actual Claude response as string or object>",
            ...
        }

        The actual response (in "result" field) should be:
        {
            "status": "SUCCESS",
            "max_log_id_processed": 250,
            "issues_created": [...],
            "duplicates_skipped": 1,
            "potential_duplicates_commented": 0
        }

        Or for failures:
        {
            "status": "FAILURE",
            "error": "Error description"
        }

        Args:
            response_str: JSON string from Claude CLI

        Returns:
            Parsed response dictionary

        Raises:
            ValueError: If JSON is invalid or missing required fields
        """
        try:
            cli_response = json.loads(response_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response from Claude: {e}")

        # Handle CLI wrapper format (--output-format json returns wrapper)
        if isinstance(cli_response, dict):
            # When using --json-schema, the structured response is in "structured_output"
            if "structured_output" in cli_response:
                response = cli_response["structured_output"]
            elif "result" in cli_response:
                # Fallback to "result" field for non-schema responses
                result_content = cli_response["result"]

                # The result might be a string (needs parsing) or already a dict
                if isinstance(result_content, str):
                    if not result_content.strip():
                        raise ValueError(
                            "Empty result from Claude - check if --json-schema "
                            "is being used (response would be in structured_output)"
                        )
                    try:
                        response = json.loads(result_content)
                    except json.JSONDecodeError:
                        # If result is not valid JSON, it's Claude's text response
                        # This happens when Claude doesn't follow JSON format instructions
                        raise ValueError(
                            f"Claude response is not valid JSON: {result_content[:200]}..."
                        )
                elif isinstance(result_content, dict):
                    response = result_content
                else:
                    raise ValueError(
                        f"Unexpected result type in CLI response: {type(result_content)}"
                    )
            else:
                # Direct JSON response (for testing or alternative invocation)
                response = cli_response
        else:
            # Direct JSON response (for testing or alternative invocation)
            response = cli_response

        # Validate required field
        if "status" not in response:
            raise ValueError("Missing required field: status")

        return response

    def get_issue_prefix(self, classification: str) -> str:
        """
        Get issue title prefix for classification type (AC4).

        Args:
            classification: Issue classification type

        Returns:
            Prefix string for issue title

        Raises:
            ValueError: If classification is unknown
        """
        prefixes = {
            "server_bug": "[BUG]",
            "client_misuse": "[CLIENT]",
            "documentation_gap": "[DOCS]",
        }

        if classification not in prefixes:
            raise ValueError(f"Unknown classification: {classification}")

        return prefixes[classification]

    def execute_scan(self) -> Dict:
        """
        Execute complete scan workflow (orchestration method).

        Workflow:
        1. Get last_scan_log_id for delta tracking
        2. Fetch existing GitHub issues for deduplication
        3. Assemble Claude prompt with context
        4. Invoke Claude CLI with prompt
        5. Parse Claude response
        6. Create issues via IssueManager (if SUCCESS)
        7. Update scan record with results

        Returns:
            Scan result dictionary with status and metrics

        Note:
            This method handles both SUCCESS and FAILURE cases.
            On FAILURE, log_id_end is NOT advanced to allow retry from same position.
        """
        logger.debug(
            f"[SELF-MON-DEBUG] execute_scan: Entry - scan_id={self.scan_id}, db_path={self.db_path}, log_db_path={self.log_db_path}"
        )

        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        try:
            # Step 1: Get last processed log ID for delta tracking
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 1 - Calling get_last_scan_log_id()"
            )
            last_log_id = self.get_last_scan_log_id()
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 1 complete - last_log_id={last_log_id}"
            )

            # Step 1b: Create initial scan record (Bug #87 issue #6)
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 1b - Calling create_scan_record(log_id_start={last_log_id})"
            )
            self.create_scan_record(log_id_start=last_log_id)
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 1b complete - scan record created"
            )

            # Step 2: Fetch existing GitHub issues for deduplication
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 2 - Calling _fetch_existing_github_issues()"
            )
            existing_issues = self._fetch_existing_github_issues()
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 2 complete - fetched {len(existing_issues)} existing issues"
            )

            # Step 3: Assemble Claude prompt
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 3 - Calling assemble_prompt()"
            )
            prompt = self.assemble_prompt(
                last_scan_log_id=last_log_id, existing_issues=existing_issues
            )
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 3 complete - prompt length={len(prompt)}"
            )

            # Step 4: Invoke Claude CLI
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 4 - Calling _invoke_claude_cli() with model={self.model}, repo_root={self.repo_root}"
            )
            claude_response_str = self._invoke_claude_cli(prompt)
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 4 complete - response length={len(claude_response_str)}"
            )

            # Step 5: Parse Claude response
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 5 - Calling parse_claude_response()"
            )
            response = self.parse_claude_response(claude_response_str)
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 5 complete - response status={response.get('status')}"
            )

            # Handle FAILURE response from Claude
            if response["status"] == "FAILURE":
                error_msg = response.get("error", "Unknown Claude error")
                logger.debug(
                    f"[SELF-MON-DEBUG] execute_scan: Claude returned FAILURE - error={error_msg}"
                )
                self.update_scan_record(status="FAILURE", error_message=error_msg)
                return {"status": "FAILURE", "error": error_msg}

            # Step 6: Create issues via IssueManager (SUCCESS case)
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 6 - Creating IssueManager"
            )
            issue_manager = IssueManager(
                db_path=self.db_path,
                scan_id=self.scan_id,
                github_repo=self.github_repo,
                github_token=self.github_token,
                server_name=self.server_name,
            )
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 6 - Calling _create_issues_from_response() with {len(response.get('issues_created', []))} issues"
            )
            issues_created_count = self._create_issues_from_response(
                response, issue_manager
            )
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 6 complete - created {issues_created_count} issues"
            )

            # Step 7: Update scan record with SUCCESS
            max_log_id = response.get("max_log_id_processed", last_log_id)
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Step 7 - Updating scan record with SUCCESS, max_log_id={max_log_id}"
            )
            self.update_scan_record(
                status="SUCCESS",
                log_id_end=max_log_id,
                issues_created=issues_created_count,
            )
            logger.debug(
                "[SELF-MON-DEBUG] execute_scan: Step 7 complete - scan record updated"
            )

            result = {
                "status": "SUCCESS",
                "issues_created": issues_created_count,
                "duplicates_skipped": response.get("duplicates_skipped", 0),
                "potential_duplicates_commented": response.get(
                    "potential_duplicates_commented", 0
                ),
                "max_log_id_processed": max_log_id,
            }
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Returning SUCCESS result - {result}"
            )
            return result

        except Exception as e:
            # Handle unexpected errors
            error_msg = f"Scan failed: {str(e)}"
            logger.debug(
                f"[SELF-MON-DEBUG] execute_scan: Exception caught - {type(e).__name__}: {e}"
            )
            logger.error("[SELF-MON-DEBUG] execute_scan: Full exception", exc_info=True)
            self.update_scan_record(status="FAILURE", error_message=error_msg)
            return {"status": "FAILURE", "error": error_msg}

    def _create_issues_from_response(self, response: Dict, issue_manager) -> int:
        """
        Create GitHub issues from parsed Claude response.

        Applies classification prefixes ([BUG], [CLIENT], [DOCS]) and delegates
        to IssueManager for creation and metadata storage.

        Args:
            response: Parsed Claude response dictionary
            issue_manager: IssueManager instance

        Returns:
            Count of issues successfully created
        """
        issues_created_count = 0

        for issue_data in response.get("issues_created", []):
            classification = issue_data["classification"]
            prefix = self.get_issue_prefix(classification)
            title = f"{prefix} {issue_data['title']}"

            issue_result = issue_manager.create_issue(
                title=title,
                body=issue_data["body"],
                classification=classification,
                error_codes=issue_data.get("error_codes", []),
                source_log_ids=issue_data.get("source_log_ids", []),
                source_files=issue_data.get("source_files", []),
            )

            if issue_result and issue_result.get("github_issue_number"):
                issues_created_count += 1

        return issues_created_count

    def _fetch_existing_github_issues(self) -> List[Dict]:
        """
        Fetch existing open GitHub issues for deduplication (Bug #87 issue #7).

        Uses GitHub REST API v3 to fetch open issues from the repository.

        Returns:
            List of issue dictionaries with keys: number, title, body, labels, created_at
            Returns empty list if github_token is not provided or on error.
        """
        logger.debug(
            f"[SELF-MON-DEBUG] _fetch_existing_github_issues: Entry - github_repo={self.github_repo}, has_token={self.github_token is not None}"
        )

        if not self.github_token:
            logger.debug(
                "[SELF-MON-DEBUG] _fetch_existing_github_issues: No github_token - returning empty list"
            )
            return []

        try:
            # Parse github_repo to extract owner and repo
            parts = self.github_repo.split("/")
            if len(parts) != 2:
                logger.warning(
                    f"Invalid github_repo format: {self.github_repo} (expected owner/repo)"
                )
                return []

            owner, repo = parts

            # GitHub REST API endpoint
            url = f"https://api.github.com/repos/{owner}/{repo}/issues"
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            params = {
                "state": "open",
                "per_page": GITHUB_ISSUE_FETCH_LIMIT,
            }

            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: Calling GitHub API - url={url}, limit={GITHUB_ISSUE_FETCH_LIMIT}, timeout={GITHUB_CLI_TIMEOUT_SECONDS}s"
            )

            response = httpx.get(
                url,
                headers=headers,
                params=params,
                timeout=GITHUB_CLI_TIMEOUT_SECONDS,
            )

            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: GitHub API completed - status_code={response.status_code}, response_length={len(response.text)}"
            )

            # Check rate limiting
            if response.status_code == 403:
                remaining = response.headers.get("X-RateLimit-Remaining", "")
                if remaining == "0":
                    logger.warning("GitHub API rate limit exceeded")
                    return []

            response.raise_for_status()

            issues = response.json()
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: Parsed {len(issues)} issues from JSON"
            )

            # Convert to expected format
            converted = [
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "body": issue.get("body", ""),
                    "labels": [label["name"] for label in issue.get("labels", [])],
                    "created_at": issue.get("created_at", ""),
                }
                for issue in issues
            ]
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: Returning {len(converted)} converted issues"
            )
            return converted

        except httpx.TimeoutException:
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: GitHub API timed out after {GITHUB_CLI_TIMEOUT_SECONDS} seconds"
            )
            logger.warning(
                f"GitHub API request timed out after {GITHUB_CLI_TIMEOUT_SECONDS} seconds"
            )
            return []
        except httpx.HTTPStatusError as e:
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: GitHub API HTTP error - status={e.response.status_code}"
            )
            logger.warning(f"GitHub API error: {e.response.status_code}")
            return []
        except httpx.RequestError as e:
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: GitHub API request error - {type(e).__name__}: {e}"
            )
            logger.warning(f"GitHub API request failed: {e}")
            return []
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug(
                f"[SELF-MON-DEBUG] _fetch_existing_github_issues: Exception - {type(e).__name__}: {e}"
            )
            logger.warning(f"Failed to parse GitHub issues response: {e}")
            return []

    def _invoke_claude_cli(self, prompt: str) -> str:
        """
        Invoke Claude CLI with prompt and return response.

        Args:
            prompt: Complete Claude prompt string

        Returns:
            Claude CLI stdout response

        Raises:
            RuntimeError: If Claude CLI invocation fails
        """
        try:
            # Story #76 AC5: Include --model parameter from config
            # Use -p for print mode (non-interactive), --output-format json for JSON response
            # Use --json-schema to enforce structured JSON output matching our expected format
            # Use --allowedTools Bash so Claude can query the log database via sqlite3
            # Use cwd=repo_root so Claude runs in repo context (can access git, create issues)
            result = subprocess.run(
                [
                    "claude",
                    "--model",
                    self.model,
                    "-p",
                    "--output-format",
                    "json",
                    "--json-schema",
                    CLAUDE_RESPONSE_SCHEMA,
                    "--allowedTools",
                    "Bash",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT_SECONDS,
                cwd=self.repo_root,  # Run in repo context
            )

            if result.returncode != 0:
                raise RuntimeError(f"Claude CLI failed: {result.stderr}")

            return result.stdout

        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Claude CLI timeout after {CLAUDE_CLI_TIMEOUT_SECONDS} seconds"
            )
        except FileNotFoundError:
            raise RuntimeError("Claude CLI not found - ensure 'claude' is in PATH")
