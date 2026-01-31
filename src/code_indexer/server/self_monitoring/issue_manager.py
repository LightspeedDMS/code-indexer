"""
Issue Manager for Self-Monitoring (Story #73 - AC1, AC5c).

Handles GitHub issue creation with SQLite metadata storage for deduplication.
Extends the pattern from ~/.claude/scripts/utils/issue_manager.py with
database persistence for tracking issues created by self-monitoring.
"""

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class IssueManager:
    """
    Manages GitHub issue creation and metadata storage for self-monitoring.

    Creates issues via gh CLI and stores metadata in SQLite database
    to support intelligent deduplication across scan cycles.

    Args:
        db_path: Path to SQLite database containing self_monitoring_issues table
        scan_id: Unique identifier for current scan cycle
        github_repo: GitHub repository in format "owner/repo"
    """

    # Search first N characters of body to avoid performance issues on large bodies
    _ERROR_SEARCH_LIMIT = 500

    def __init__(
        self,
        db_path: str,
        scan_id: str,
        github_repo: str,
        github_token: Optional[str] = None,
        server_name: Optional[str] = None
    ):
        """
        Initialize IssueManager.

        Args:
            db_path: Path to SQLite database
            scan_id: Current scan identifier
            github_repo: GitHub repository (owner/repo)
            github_token: GitHub token for authentication (optional, Bug #87)
            server_name: Server display name for issue identification (optional, Bug #87)
        """
        self.db_path = db_path
        self.scan_id = scan_id
        self.github_repo = github_repo
        self.github_token = github_token
        self.server_name = server_name

    def create_issue(
        self,
        classification: str,
        title: str,
        body: str,
        source_log_ids: List[int],
        source_files: List[str],
        error_codes: List[str]
    ) -> Dict:
        """
        Create GitHub issue and store metadata in database.

        Args:
            classification: Issue type (server_bug, client_misuse, documentation_gap)
            title: Issue title (should include prefix like [BUG], [CLIENT], [DOCS])
            body: Issue body in markdown format
            source_log_ids: List of log entry IDs that triggered this issue
            source_files: List of source files involved
            error_codes: List of error codes found in logs

        Returns:
            Dict containing issue metadata:
                - github_issue_number: Issue number
                - github_issue_url: Issue URL
                - classification: Issue classification

        Raises:
            RuntimeError: If gh CLI fails to create issue
        """
        # Prepend server identity if server_name provided (Bug #87 issue #4)
        if self.server_name:
            try:
                server_ip = socket.gethostbyname(socket.gethostname())
            except Exception as e:
                logger.warning(f"Failed to resolve server IP: {e}")
                server_ip = "unknown"

            identity_section = (
                f"**Created by CIDX Server**\n"
                f"- Server Name: {self.server_name}\n"
                f"- Server IP: {server_ip}\n"
                f"- Scan ID: {self.scan_id}\n"
                f"\n"
                f"---\n"
                f"\n"
            )
            body = identity_section + body

        # Create issue via GitHub REST API
        github_issue_number, github_issue_url = self._create_github_issue_via_api(
            title=title,
            body=body
        )

        # Compute fingerprint for deduplication
        error_type = self._extract_error_type(title, body)
        fingerprint = self.compute_fingerprint(
            classification=classification,
            source_files=source_files,
            error_type=error_type
        )

        # Store metadata in database
        self._store_metadata(
            github_issue_number=github_issue_number,
            github_issue_url=github_issue_url,
            classification=classification,
            title=title,
            error_codes=error_codes,
            fingerprint=fingerprint,
            source_log_ids=source_log_ids,
            source_files=source_files
        )

        return {
            "github_issue_number": github_issue_number,
            "github_issue_url": github_issue_url,
            "classification": classification
        }

    def _create_github_issue_via_api(
        self,
        title: str,
        body: str
    ) -> tuple:
        """
        Create GitHub issue via REST API.

        Args:
            title: Issue title
            body: Issue body

        Returns:
            Tuple of (issue_number, issue_url)

        Raises:
            RuntimeError: If GitHub API request fails
        """
        if not self.github_token:
            raise RuntimeError("GitHub token not configured")

        # Parse github_repo to extract owner and repo
        parts = self.github_repo.split("/")
        if len(parts) != 2:
            raise RuntimeError(f"Invalid github_repo format: {self.github_repo} (expected owner/repo)")

        owner, repo = parts

        # GitHub REST API endpoint
        url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {
            "title": title,
            "body": body,
        }

        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=30.0,
            )

            # Check rate limiting
            if response.status_code == 403:
                remaining = response.headers.get("X-RateLimit-Remaining", "")
                if remaining == "0":
                    raise RuntimeError("GitHub API rate limit exceeded")

            response.raise_for_status()

            issue_data = response.json()
            issue_number = issue_data["number"]
            issue_url = issue_data["html_url"]

            return issue_number, issue_url

        except httpx.TimeoutException as e:
            raise RuntimeError(f"GitHub API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"GitHub API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"GitHub API request failed: {e}") from e
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Failed to parse GitHub API response: {e}") from e

    def _store_metadata(
        self,
        github_issue_number: int,
        github_issue_url: str,
        classification: str,
        title: str,
        error_codes: List[str],
        fingerprint: str,
        source_log_ids: List[int],
        source_files: List[str]
    ) -> None:
        """
        Store issue metadata in SQLite database.

        Args:
            github_issue_number: GitHub issue number
            github_issue_url: GitHub issue URL
            classification: Issue classification
            title: Issue title
            error_codes: List of error codes
            fingerprint: Computed fingerprint for deduplication
            source_log_ids: List of log IDs
            source_files: List of source files
        """
        conn = sqlite3.connect(self.db_path)
        try:
            # Convert lists to CSV strings
            error_codes_str = ",".join(error_codes) if error_codes else ""
            source_log_ids_str = ",".join(str(lid) for lid in source_log_ids)
            source_files_str = ",".join(source_files) if source_files else ""

            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.scan_id,
                    github_issue_number,
                    github_issue_url,
                    classification,
                    error_codes_str,
                    fingerprint,
                    source_log_ids_str,
                    source_files_str,
                    title,
                    datetime.utcnow().isoformat()
                )
            )
            conn.commit()
        finally:
            conn.close()

    def compute_fingerprint(
        self,
        classification: str,
        source_files: List[str],
        error_type: str
    ) -> str:
        """
        Compute deterministic fingerprint for Tier 2 deduplication.

        Fingerprint is hash(classification + source_files + error_type).

        Args:
            classification: Issue classification
            source_files: List of source files (sorted for determinism)
            error_type: Error type extracted from logs

        Returns:
            SHA256 hex digest fingerprint
        """
        # Sort source files for deterministic hashing
        sorted_files = sorted(source_files) if source_files else []

        # Concatenate components
        fingerprint_input = (
            f"{classification}|"
            f"{','.join(sorted_files)}|"
            f"{error_type}"
        )

        # Compute SHA256 hash
        return hashlib.sha256(fingerprint_input.encode()).hexdigest()

    def _extract_error_type(self, title: str, body: str) -> str:
        """
        Extract error type from issue title or body.

        Searches for common error patterns like "ValidationError", "ConnectionError".

        Args:
            title: Issue title
            body: Issue body

        Returns:
            Error type string (e.g., "ValidationError", "ConnectionError")
        """
        # Pattern for common error types (matches IOError, FileNotFoundError, etc.)
        error_pattern = r'\b(\w+Error|\w+Exception|\w+Failure)\b'

        # Try title first
        match = re.search(error_pattern, title)
        if match:
            return match.group(1)

        # Try body (limited search to avoid performance issues)
        match = re.search(error_pattern, body[:self._ERROR_SEARCH_LIMIT])
        if match:
            return match.group(1)

        # Default to generic type if no pattern found
        return "UnknownError"

    def extract_error_codes(self, text: str) -> List[str]:
        """
        Extract error codes from text for Tier 1 deduplication.

        Searches for error code pattern: [SUBSYSTEM-CATEGORY-NNN]

        Args:
            text: Text to search (title, body, logs)

        Returns:
            List of error codes found (may be empty)
        """
        # Error code pattern: [XXX-YYY-NNN]
        pattern = r'\[([A-Z]+-[A-Z]+-\d+)\]'
        matches = re.findall(pattern, text)
        return matches

    def get_existing_issues_metadata(self, days: int = 90) -> List[Dict]:
        """
        Retrieve metadata for existing issues from last N days.

        Used for deduplication context assembly.

        Args:
            days: Number of days to look back (default 90)

        Returns:
            List of issue metadata dicts containing:
                - github_issue_number
                - github_issue_url
                - classification
                - error_codes
                - fingerprint
                - title
                - created_at
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT github_issue_number, github_issue_url, classification, "
                "error_codes, fingerprint, title, created_at "
                "FROM self_monitoring_issues "
                "WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days') "
                "ORDER BY created_at DESC",
                (days,)
            )

            results = []
            for row in cursor.fetchall():
                results.append({
                    "github_issue_number": row[0],
                    "github_issue_url": row[1],
                    "classification": row[2],
                    "error_codes": row[3],  # CSV string
                    "fingerprint": row[4],
                    "title": row[5],
                    "created_at": row[6]
                })

            return results
        finally:
            conn.close()
