"""
Description Refresh Scheduler (Story #190).

Manages periodic description regeneration for golden repositories using
hash-based bucket scheduling with jitter to distribute load evenly.
"""

import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
    GoldenRepoMetadataSqliteBackend,
)

logger = logging.getLogger(__name__)


class DescriptionRefreshScheduler:
    """
    Scheduler for periodic repository description refresh.

    Implements hash-based bucket scheduling with jitter to distribute
    refresh jobs evenly across time intervals, preventing thundering herd.
    """

    def __init__(
        self, db_path: str, config_manager, claude_cli_manager=None, meta_dir: Optional[Path] = None, analysis_model: str = "opus"
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            db_path: Path to SQLite database
            config_manager: ServerConfigManager instance
            claude_cli_manager: Optional ClaudeCliManager instance (for submitting work)
            meta_dir: Path to cidx-meta directory (for reading existing .md files)
            analysis_model: Claude model to use ("opus" or "sonnet", default: "opus")
        """
        self._db_path = db_path
        self._config_manager = config_manager
        self._claude_cli_manager = claude_cli_manager
        self._meta_dir = meta_dir
        self._analysis_model = analysis_model
        self._tracking_backend = DescriptionRefreshTrackingBackend(db_path)
        self._golden_backend = GoldenRepoMetadataSqliteBackend(db_path)
        self._shutdown_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start daemon thread if enabled in config."""
        config = self._config_manager.load_config()
        if not config or not config.claude_integration_config:
            logger.debug("Skipping description refresh: config not initialized")
            return

        if not config.claude_integration_config.description_refresh_enabled:
            logger.debug("Skipping description refresh: description_refresh_enabled is false")
            return

        interval_hours = config.claude_integration_config.description_refresh_interval_hours

        # Calculate number of buckets (one per hour in interval)
        buckets = interval_hours

        logger.info(
            f"Description refresh scheduler started (interval: {interval_hours}h, {buckets} buckets)"
        )

        # Start daemon thread
        self._shutdown_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop daemon thread."""
        logger.info("Stopping description refresh scheduler")
        self._shutdown_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def calculate_next_run(
        self, alias: str, interval_hours: Optional[int] = None
    ) -> str:
        """
        Calculate next run time using hash-based bucket scheduling with jitter.

        Args:
            alias: Repository alias (used for consistent bucketing)
            interval_hours: Interval in hours (defaults to config value)

        Returns:
            ISO 8601 timestamp for next run
        """
        if interval_hours is None:
            interval_hours = self._get_interval_hours()

        # Hash-based bucket assignment (deterministic for same alias)
        bucket = int(hashlib.md5(alias.encode()).hexdigest(), 16) % interval_hours

        # Calculate next hour boundary
        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        # Base time = next_hour + bucket offset
        base_time = next_hour + timedelta(hours=bucket)

        # Add jitter (0-30% of bucket size = 0-18 minutes for 1-hour buckets)
        jitter_seconds = random.uniform(0, 3600 * 0.3)

        final_time = base_time + timedelta(seconds=jitter_seconds)

        logger.debug(f"Repo {alias} assigned to bucket {bucket}/{interval_hours}")

        return final_time.isoformat()

    def has_changes_since_last_run(
        self, repo_path: str, tracking_record: Dict[str, Any]
    ) -> bool:
        """
        Check if repository has changes since last refresh.

        Args:
            repo_path: Path to repository
            tracking_record: Tracking record from database

        Returns:
            True if repository has changes (or no metadata), False if unchanged
        """
        metadata_path = Path(repo_path) / ".code-indexer" / "metadata.json"

        if not metadata_path.exists():
            logger.debug(f"No metadata.json in {repo_path}, assuming changes")
            return True

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            # Git repository: compare current_commit
            if "current_commit" in metadata:
                last_known_commit = tracking_record.get("last_known_commit")
                current_commit = metadata["current_commit"]

                if last_known_commit == current_commit:
                    logger.debug(
                        f"Skipping {repo_path}: no changes since last run (commit: {current_commit})"
                    )
                    return False
                else:
                    logger.debug(
                        f"Changes detected in {repo_path}: {last_known_commit} -> {current_commit}"
                    )
                    return True

            # Langfuse repository: compare files_processed
            if "files_processed" in metadata:
                last_known_files = tracking_record.get("last_known_files_processed")
                current_files = metadata["files_processed"]

                if last_known_files == current_files:
                    logger.debug(
                        f"Skipping {repo_path}: no changes since last run (files: {current_files})"
                    )
                    return False
                else:
                    logger.debug(
                        f"Changes detected in {repo_path}: {last_known_files} -> {current_files} files"
                    )
                    return True

            # Unknown metadata format - assume changes
            logger.debug(
                f"Unknown metadata format in {repo_path}, assuming changes"
            )
            return True

        except Exception as e:
            logger.warning(
                f"Failed to read metadata from {repo_path}: {e}", exc_info=True
            )
            # Safe default: assume changes
            return True

    def get_stale_repos(self) -> List[Dict[str, Any]]:
        """
        Query repos where next_run <= now AND status != 'queued'.

        Returns:
            List of stale repo records with path info from golden_repos_metadata
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Get stale tracking records
        stale_tracking = self._tracking_backend.get_stale_repos(now_iso)

        # Join with golden repos to get clone_path
        result = []
        for tracking in stale_tracking:
            alias = tracking["repo_alias"]
            golden_repo = self._golden_backend.get_repo(alias)

            if golden_repo:
                # Merge tracking and golden repo data
                merged = {**tracking, "clone_path": golden_repo["clone_path"]}
                result.append(merged)
            else:
                logger.warning(
                    f"Tracking record exists for {alias} but golden repo not found"
                )

        return result

    def on_refresh_complete(
        self,
        repo_alias: str,
        repo_path: str,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Callback for ClaudeCliManager when refresh completes.

        Updates tracking record with success/failure status and change markers.

        Args:
            repo_alias: Repository alias
            repo_path: Path to repository
            success: Whether refresh succeeded
            result: Result data from Claude CLI (may contain error info)
        """
        now = datetime.now(timezone.utc).isoformat()

        # Read current metadata to save change markers
        metadata_path = Path(repo_path) / ".code-indexer" / "metadata.json"
        change_markers = {}

        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)

                if "current_commit" in metadata:
                    change_markers["last_known_commit"] = metadata["current_commit"]

                if "files_processed" in metadata:
                    change_markers["last_known_files_processed"] = metadata[
                        "files_processed"
                    ]

                if "indexed_at" in metadata:
                    change_markers["last_known_indexed_at"] = metadata["indexed_at"]

            except Exception as e:
                logger.warning(
                    f"Failed to read metadata from {repo_path}: {e}", exc_info=True
                )

        # Update tracking record
        if success:
            self._tracking_backend.upsert_tracking(
                repo_alias=repo_alias,
                status="completed",
                last_run=now,
                next_run=self.calculate_next_run(repo_alias),
                error=None,
                updated_at=now,
                **change_markers,
            )
            logger.info(f"Description refresh completed for {repo_alias}")
        else:
            error_msg = result.get("error") if result else "Unknown error"
            self._tracking_backend.upsert_tracking(
                repo_alias=repo_alias,
                status="failed",
                last_run=now,
                next_run=self.calculate_next_run(repo_alias),
                error=error_msg,
                updated_at=now,
            )
            logger.warning(f"Description refresh failed for {repo_alias}: {error_msg}")

    def _run_loop(self) -> None:
        """
        Main scheduler loop (runs in daemon thread).

        Periodically checks for stale repos and submits refresh jobs.
        """
        while not self._shutdown_event.is_set():
            try:
                # Check if enabled (config may change while running)
                config = self._config_manager.load_config()
                if (
                    not config
                    or not config.claude_integration_config
                    or not config.claude_integration_config.description_refresh_enabled
                ):
                    logger.debug(
                        "Description refresh disabled, sleeping"
                    )
                    self._shutdown_event.wait(60)
                    continue

                # Get stale repos
                stale_repos = self.get_stale_repos()

                for repo in stale_repos:
                    alias = repo["repo_alias"]
                    clone_path = repo["clone_path"]

                    # Check for changes
                    if not self.has_changes_since_last_run(clone_path, repo):
                        # No changes - reschedule without submitting work
                        now = datetime.now(timezone.utc).isoformat()
                        self._tracking_backend.upsert_tracking(
                            repo_alias=alias,
                            next_run=self.calculate_next_run(alias),
                            updated_at=now,
                        )
                        continue

                    # Submit refresh job (if ClaudeCliManager available)
                    if self._claude_cli_manager:
                        logger.info(f"Submitting description refresh for {alias}")

                        # Get refresh prompt using RepoAnalyzer
                        prompt = self._get_refresh_prompt(alias, clone_path)
                        if prompt is None:
                            logger.warning(f"Cannot refresh {alias}: failed to generate prompt, rescheduling")
                            # Reschedule to next cycle to avoid infinite retry loop (Finding N3)
                            now = datetime.now(timezone.utc).isoformat()
                            self._tracking_backend.upsert_tracking(
                                repo_alias=alias,
                                next_run=self.calculate_next_run(alias),
                                updated_at=now,
                            )
                            continue

                        # Mark as queued before submitting
                        now = datetime.now(timezone.utc).isoformat()
                        self._tracking_backend.upsert_tracking(
                            repo_alias=alias,
                            status="queued",
                            updated_at=now,
                        )

                        # Invoke Claude CLI directly with the refresh prompt (Finding N2)
                        # Run in background thread to avoid blocking scheduler loop
                        def refresh_task(alias=alias, clone_path=clone_path, prompt=prompt):
                            success, result_str = self._invoke_claude_cli(clone_path, prompt)
                            if success:
                                # Update .md file with refreshed content
                                self._update_description_file(alias, result_str)
                            # Call completion callback
                            result_dict = {"error": result_str} if not success else None
                            self.on_refresh_complete(alias, clone_path, success, result_dict)

                        threading.Thread(target=refresh_task, daemon=True).start()
                        logger.debug(f"Spawned refresh task for {alias}")

                    else:
                        logger.debug(
                            f"ClaudeCliManager not available, skipping {alias}"
                        )

            except Exception as e:
                logger.error(
                    f"Error in description refresh scheduler loop: {e}", exc_info=True
                )

            # Sleep between checks
            self._shutdown_event.wait(60)

    def _get_interval_hours(self) -> int:
        """Get refresh interval from config."""
        config = self._config_manager.load_config()
        if not config or not config.claude_integration_config:
            return 24  # Default

        return config.claude_integration_config.description_refresh_interval_hours

    def _read_existing_description(self, repo_alias: str) -> Optional[Dict[str, str]]:
        """
        Read existing .md file from cidx-meta and extract description and last_analyzed.

        Args:
            repo_alias: Repository alias

        Returns:
            Dict with 'description' and 'last_analyzed' keys, or None if file not found
        """
        if not self._meta_dir:
            logger.warning("Meta directory not set, cannot read existing description")
            return None

        md_file = self._meta_dir / f"{repo_alias}.md"
        if not md_file.exists():
            logger.debug(f"No .md file found for {repo_alias}, cannot refresh")
            return None

        try:
            content = md_file.read_text()

            # Parse YAML frontmatter to extract last_analyzed
            # Format: ---\nfield: value\n---\n<body>
            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if not frontmatter_match:
                logger.warning(f"No YAML frontmatter found in {md_file}")
                return {"description": content, "last_analyzed": None}

            frontmatter_text = frontmatter_match.group(1)
            body = frontmatter_match.group(2)

            # Extract last_analyzed from frontmatter
            last_analyzed = None
            for line in frontmatter_text.split("\n"):
                if line.startswith("last_analyzed:"):
                    last_analyzed = line.split(":", 1)[1].strip()
                    break

            return {"description": content, "last_analyzed": last_analyzed}

        except Exception as e:
            logger.error(f"Failed to read existing description for {repo_alias}: {e}", exc_info=True)
            return None

    def _get_refresh_prompt(self, repo_alias: str, repo_path: str) -> Optional[str]:
        """
        Get refresh prompt for a repository using RepoAnalyzer.

        Args:
            repo_alias: Repository alias
            repo_path: Path to repository

        Returns:
            Refresh prompt string, or None if cannot generate
        """
        # Read existing description
        desc_data = self._read_existing_description(repo_alias)
        if not desc_data or not desc_data.get("last_analyzed"):
            logger.warning(f"Cannot generate refresh prompt for {repo_alias}: missing existing description or last_analyzed")
            return None

        try:
            from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

            analyzer = RepoAnalyzer(repo_path)
            prompt = analyzer.get_prompt(
                mode="refresh",
                last_analyzed=desc_data["last_analyzed"],
                existing_description=desc_data["description"],
            )
            return prompt

        except Exception as e:
            logger.error(f"Failed to generate refresh prompt for {repo_alias}: {e}", exc_info=True)
            return None

    def _invoke_claude_cli(self, repo_path: str, prompt: str) -> tuple[bool, str]:
        """
        Invoke Claude CLI with the given prompt.

        Args:
            repo_path: Path to repository
            prompt: Prompt to send to Claude

        Returns:
            Tuple of (success: bool, result: str) where result is the output or error message
        """
        import os
        import re
        import shlex
        import subprocess

        try:
            # Sync API key before invocation (if ClaudeCliManager available)
            if self._claude_cli_manager:
                try:
                    self._claude_cli_manager.sync_api_key()
                except Exception as e:
                    logger.warning(f"API key sync failed: {e}")
                    # Continue anyway - sync failure shouldn't block analysis

            # Use script to provide pseudo-TTY (required for Claude CLI in non-interactive environments)
            claude_cmd = f"timeout 90 claude --model {shlex.quote(self._analysis_model)} -p {shlex.quote(prompt)} --print --dangerously-skip-permissions"
            full_cmd = ["script", "-q", "-c", claude_cmd, "/dev/null"]

            result = subprocess.run(
                full_cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120,
                env={k: v for k, v in os.environ.items() if k not in (
                    ("CLAUDECODE", "ANTHROPIC_API_KEY") if "CLAUDECODE" in os.environ
                    else ("CLAUDECODE",)
                )},
            )

            if result.returncode != 0:
                error_msg = f"Claude CLI returned non-zero: {result.returncode}, stderr: {result.stderr}"
                logger.warning(error_msg)
                return False, error_msg

            # Clean output (remove ALL terminal control sequences)
            output = result.stdout
            # CSI sequences: ESC [ ... letter (colors, cursor, modes like [?2004l, [?25h)
            output = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", output)
            # OSC sequences: ESC ] ... BEL or ESC ] ... ST
            output = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", output)
            # Other ESC sequences (ESC followed by single char)
            output = re.sub(r"\x1b[^[\]()]", "", output)
            # Stray control artifacts from script command
            output = re.sub(r"\[<u", "", output)
            # Strip any remaining bare ESC bytes not part of recognized sequences
            output = output.replace("\x1b", "")
            # Normalize line endings
            output = output.replace("\r\n", "\n").replace("\r", "")
            output = output.strip()
            # Strip chain-of-thought text before YAML frontmatter
            # Claude may emit reasoning text before the actual description
            frontmatter_match = re.search(r'^---\s*$', output, re.MULTILINE)
            if frontmatter_match and frontmatter_match.start() > 0:
                output = output[frontmatter_match.start():]

            # Validate output quality (detect error messages masquerading as content)
            if not self._validate_cli_output(output):
                error_msg = f"Claude CLI output appears to be an error message (length={len(output)}): {output[:200]}"
                logger.warning(error_msg)
                return False, error_msg

            return True, output

        except subprocess.TimeoutExpired:
            error_msg = "Claude CLI timed out after 120s"
            logger.warning(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"Unexpected error during Claude CLI execution: {e}"
            logger.error(error_msg, exc_info=True)
            return False, error_msg

    def _validate_cli_output(self, output: str) -> bool:
        """
        Validate that Claude CLI output is a real description, not an error message.

        Args:
            output: Cleaned output from Claude CLI

        Returns:
            True if output looks like a valid description, False if it appears to be an error
        """
        # Empty or very short output is invalid
        if not output or len(output) < 100:
            output_len = len(output) if output else 0
            logger.warning(f"CLI output too short ({output_len} chars), likely an error")
            return False

        # Known error patterns from Claude CLI
        error_patterns = [
            "Invalid API key",
            "Fix external API key",
            "cannot be launched inside another",
            "Nested sessions share runtime",
            "CLAUDECODE environment variable",
            "Error:",
            "Authentication failed",
            "rate limit",
            "quota exceeded",
        ]

        output_lower = output.lower()
        for pattern in error_patterns:
            if pattern.lower() in output_lower:
                logger.warning(f"CLI output contains error pattern: '{pattern}'")
                return False

        return True

    def _update_description_file(self, repo_alias: str, content: str) -> None:
        """
        Update the .md description file for a repository.

        Args:
            repo_alias: Repository alias
            content: New content for the .md file (YAML frontmatter + markdown)
        """
        if not self._meta_dir:
            logger.warning(f"Meta directory not set, cannot update description for {repo_alias}")
            return

        md_file = self._meta_dir / f"{repo_alias}.md"

        try:
            md_file.write_text(content)
            logger.info(f"Updated description file: {md_file}")

        except Exception as e:
            logger.error(f"Failed to update description file for {repo_alias}: {e}", exc_info=True)

    def close(self) -> None:
        """Clean up resources."""
        self.stop()
        self._tracking_backend.close()
        self._golden_backend.close()
