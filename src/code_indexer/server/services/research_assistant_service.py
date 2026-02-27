"""
Research Assistant Service for CIDX Server.

Story #141: Research Assistant - Basic Chatbot Working

Manages research sessions and chat messages with SQLite storage.
"""

import logging
import os
import socket
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import bleach
import markdown

logger = logging.getLogger(__name__)


# AC5: Security Guardrails Constant
SECURITY_GUARDRAILS = """## MANDATORY SECURITY CONSTRAINTS

You are a Research Assistant for investigating CIDX server anomalies.

### ABSOLUTE PROHIBITIONS:
1. NO system destruction (rm -rf, format, delete system files)
2. NO credential exposure (SSH keys, API keys, passwords, .env files)
3. NO network exfiltration (curl/wget to external servers)
4. NO privilege escalation (sudo without explicit confirmation)
5. NO persistence mechanisms (crontab, systemd, bashrc)

### ALLOWED OPERATIONS:
- Read CIDX logs, configs, and source code
- Follow the `code-indexer` symlink in your working directory to access the CIDX repository source code - this is EXPLICITLY PERMITTED
- Run cidx CLI commands for diagnostics
- Read server database for investigation
- Analyze Python source files in the CIDX codebase
- Write analysis reports to the session folder ONLY

### SYMLINK ACCESS:
Your working directory contains a `code-indexer` symlink pointing to the CIDX repository.
You have FULL READ ACCESS to all files through this symlink. Use it to:
- Browse source code: `ls code-indexer/src/`
- Read Python files: `cat code-indexer/src/code_indexer/*.py`
- Search code: `grep -r "pattern" code-indexer/`

---
"""

# AC6: File Upload Restrictions (Story #144)
ALLOWED_EXTENSIONS = {
    ".txt",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".py",
    ".md",
    ".csv",
    ".xml",
    ".html",
    ".cfg",
    ".conf",
    ".ini",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_SESSION_SIZE = 100 * 1024 * 1024  # 100MB


class ResearchAssistantService:
    """
    Service for managing research sessions and chat messages.

    Provides methods for:
    - Creating and managing research sessions
    - Storing and retrieving chat messages
    - Auto-creating default session
    - Executing Claude CLI prompts with background jobs (AC4)
    """

    # AC4: Background job tracking - CLASS LEVEL for persistence across requests
    _jobs: Dict[str, Dict[str, Any]] = {}
    _jobs_lock = threading.Lock()

    def __init__(
        self,
        db_path: Optional[str] = None,
        github_token: Optional[str] = None,
        job_tracker=None,
    ):
        """
        Initialize ResearchAssistantService.

        Args:
            db_path: Path to SQLite database. If None, uses default location.
            github_token: GitHub token for bug report creation (Story #202). If None, no token is set.
            job_tracker: Optional JobTracker for dashboard visibility (Story #314).
        """
        if db_path is not None:
            self.db_path = db_path
        else:
            server_data_dir = os.environ.get(
                "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
            )
            self.db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")

        # Store GitHub token for subprocess environment (Story #202 AC3)
        self._github_token = github_token
        # Story #314: JobTracker for dashboard visibility (dual tracking with _jobs dict)
        self._job_tracker = job_tracker

    def _detect_repo_root(self) -> Optional[str]:
        """
        Detect CIDX repository root from file location.

        Walks up directory tree looking for repo markers (pyproject.toml + src/code_indexer).

        Returns:
            Repository root path or None if not found
        """
        # Try to find repo root from this file's location
        # This file is in src/code_indexer/server/services/
        current_file = Path(__file__).resolve()

        # Walk up looking for repo markers
        for parent in [current_file] + list(current_file.parents):
            if (parent / "pyproject.toml").exists() and (
                parent / "src" / "code_indexer"
            ).exists():
                return str(parent)

        return None

    def _get_config_dir(self) -> str:
        """
        Get config directory path.

        Returns:
            Path to config directory containing research_assistant_prompt.md
        """
        # Try CIDX_REPO_ROOT env var first
        cidx_repo_root = os.environ.get("CIDX_REPO_ROOT")
        if not cidx_repo_root:
            cidx_repo_root = self._detect_repo_root()

        if cidx_repo_root:
            return str(Path(cidx_repo_root) / "src" / "code_indexer" / "server" / "config")

        # Fallback: relative to this file
        return str(Path(__file__).parent.parent / "config")

    def _get_prompt_variables(self) -> Dict[str, str]:
        """
        Get runtime values for prompt template variables.

        Uses actual runtime values from the service instance rather than
        re-deriving from environment variables. This ensures consistency
        between the paths the service actually uses and what appears in
        the prompt template.

        Returns:
            Dictionary of variable names to values
        """
        from code_indexer import __version__

        # Derive server_data_dir from self.db_path (go up two levels: data/ -> server_data_dir/)
        # This ensures we use the ACTUAL runtime path, not a re-derived one from env vars
        db_path = Path(self.db_path)
        server_data_dir = str(db_path.parent.parent)

        cidx_repo_root = os.environ.get("CIDX_REPO_ROOT")
        if not cidx_repo_root:
            cidx_repo_root = self._detect_repo_root()
        if not cidx_repo_root:
            cidx_repo_root = ""

        return {
            "hostname": socket.gethostname(),
            "server_version": __version__,
            "server_data_dir": server_data_dir,
            "db_path": self.db_path,  # Use actual runtime path
            "cidx_repo_root": cidx_repo_root,
            "golden_repos_dir": str(Path(server_data_dir) / "golden-repos"),
            "service_name": "cidx-server",
        }

    def load_research_prompt(self) -> str:
        """
        Load and parametrize research assistant prompt template.

        Loads template from config/research_assistant_prompt.md and substitutes
        runtime variables. Falls back to hardcoded SECURITY_GUARDRAILS if
        template file is missing or unreadable.

        Returns:
            Parametrized prompt string
        """
        try:
            # Get config directory
            config_dir = self._get_config_dir()
            template_path = Path(config_dir) / "research_assistant_prompt.md"

            # Read template file
            if not template_path.exists():
                logger.warning(
                    f"Template file not found: {template_path}, using hardcoded prompt"
                )
                return SECURITY_GUARDRAILS

            template_content = template_path.read_text()

            # Get runtime variables
            variables = self._get_prompt_variables()

            # Substitute variables
            prompt = template_content.format(**variables)

            return prompt

        except Exception as e:
            logger.error(f"Failed to load prompt template: {e}, using hardcoded prompt")
            return SECURITY_GUARDRAILS

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with foreign keys enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        return conn

    def render_markdown(self, text: str) -> str:
        """
        Convert markdown to sanitized HTML.

        Supports:
        - Code blocks with syntax highlighting
        - Headers (h1-h6)
        - Lists (ordered and unordered)
        - Links
        - Bold, italic, inline code
        - Tables
        - Blockquotes

        Security:
        - Strips script tags and event handlers
        - Sanitizes HTML to prevent XSS attacks

        Args:
            text: Markdown text

        Returns:
            Sanitized HTML string
        """
        if not text:
            return ""

        # Convert markdown to HTML with extensions
        html = markdown.markdown(
            text,
            extensions=[
                "fenced_code",  # Support ```code blocks```
                "tables",  # Support tables
                "nl2br",  # Convert newlines to <br>
                "codehilite",  # Syntax highlighting for code blocks
            ],
        )

        # Define allowed HTML tags and attributes for sanitization
        allowed_tags = [
            "p",
            "br",
            "strong",
            "em",
            "code",
            "pre",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "li",
            "a",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "div",
            "span",
            "hr",
        ]

        allowed_attrs = {
            "a": ["href", "title"],
            "code": ["class"],
            "div": ["class"],
            "span": ["class"],
            "pre": ["class"],
        }

        # Sanitize HTML to prevent XSS attacks
        clean_html = bleach.clean(
            html,
            tags=allowed_tags,
            attributes=allowed_attrs,
            protocols=["http", "https", "mailto"]
        )

        return clean_html

    def _get_or_create_claude_session_id(self, session_id: str) -> str:
        """
        Get or create a unique Claude session ID for a research session.

        Retrieves the stored claude_session_id from the database. If NULL,
        generates a fresh UUID4, stores it, and returns it.

        This properly separates our internal session management from Claude's,
        ensuring each session gets a unique Claude session ID that persists
        across server restarts.

        Args:
            session_id: Internal research session ID

        Returns:
            A valid UUID string for Claude CLI (--session-id or --resume)
        """
        conn = self._get_connection()
        try:
            # Query the session's claude_session_id
            cursor = conn.execute(
                "SELECT claude_session_id FROM research_sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()

            if row is None:
                # Session doesn't exist - should not happen, but return a new UUID
                logger.warning(f"Session {session_id} not found, generating new Claude session ID")
                return str(uuid.uuid4())

            claude_session_id = row["claude_session_id"]

            # If NULL, generate and store a new UUID
            if claude_session_id is None:
                claude_session_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE research_sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
                    (claude_session_id, now, session_id),
                )
                conn.commit()
                logger.info(f"Generated new Claude session ID for session {session_id}: {claude_session_id}")

            return claude_session_id

        finally:
            conn.close()

    def get_default_session(self) -> Dict[str, Any]:
        """
        Get or create the default research session (AC6).

        Auto-creates the session if it doesn't exist.
        Also ensures session folder and softlink exist (AC3).

        Returns:
            Dictionary with session data (id, name, folder_path, created_at, updated_at)
        """
        conn = self._get_connection()
        try:
            # Check if default session exists
            cursor = conn.execute(
                "SELECT id, name, folder_path, created_at, updated_at "
                "FROM research_sessions WHERE id = 'default'"
            )
            row = cursor.fetchone()

            if row is not None:
                session_dict = dict(row)
                # Ensure folder and softlink exist even if session already in DB
                self._ensure_session_folder_setup(session_dict["folder_path"])
                return session_dict

            # Create default session
            now = datetime.now(timezone.utc).isoformat()
            folder_path = str(Path.home() / ".cidx-server" / "research" / "default")

            conn.execute(
                "INSERT INTO research_sessions (id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", "Default Session", folder_path, now, now),
            )
            conn.commit()

            # Ensure folder and softlink exist (AC3)
            self._ensure_session_folder_setup(folder_path)

            # Return the newly created session
            cursor = conn.execute(
                "SELECT id, name, folder_path, created_at, updated_at "
                "FROM research_sessions WHERE id = 'default'"
            )
            row = cursor.fetchone()
            return dict(row)

        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a research session (AC5 - Story #143).

        Deletes session from database (CASCADE deletes messages), removes
        session folder from filesystem, and cleans up Claude CLI project folder.

        Args:
            session_id: Session ID to delete

        Returns:
            True if deleted successfully, False if session not found
        """
        import shutil

        conn = self._get_connection()
        try:
            # Check if session exists and get folder path
            cursor = conn.execute(
                "SELECT folder_path FROM research_sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return False

            folder_path = row["folder_path"]

            # Delete from database (CASCADE will delete messages)
            conn.execute("DELETE FROM research_sessions WHERE id = ?", (session_id,))
            conn.commit()

            # Delete session folder from filesystem
            folder = Path(folder_path)
            if folder.exists():
                shutil.rmtree(folder)
                logger.info(f"Deleted session folder: {folder}")

            # Bug #154: Also delete Claude CLI project folder to keep storage clean
            # Claude CLI stores sessions in ~/.claude/projects/{path-with-dashes}/
            # where the folder name is the working directory path with / replaced by -
            self._cleanup_claude_cli_project(folder_path)

            return True

        finally:
            conn.close()

    def _cleanup_claude_cli_project(self, folder_path: str) -> None:
        """
        Clean up Claude CLI project folder associated with a session.

        Claude CLI creates project folders in ~/.claude/projects/ using the
        working directory path with '/' replaced by '-'. This method removes
        those folders to prevent orphaned session data.

        Args:
            folder_path: The session's folder path (e.g., /home/user/.cidx-server/research/uuid)
        """
        import shutil

        try:
            # Convert folder path to Claude CLI project folder name
            # /home/user/.cidx-server/research/uuid -> -home-user--cidx-server-research-uuid
            claude_project_name = folder_path.replace("/", "-")

            claude_projects_dir = Path.home() / ".claude" / "projects"
            claude_project_path = claude_projects_dir / claude_project_name

            if claude_project_path.exists() and claude_project_path.is_dir():
                shutil.rmtree(claude_project_path)
                logger.info(f"Deleted Claude CLI project folder: {claude_project_path}")
            else:
                logger.debug(f"Claude CLI project folder not found (may not exist): {claude_project_path}")

        except Exception as e:
            # Don't fail the session deletion if Claude cleanup fails
            logger.warning(f"Failed to cleanup Claude CLI project folder for {folder_path}: {e}")

    def generate_session_name(self, first_prompt: str) -> str:
        """
        Generate session name from first prompt (AC2/AC4 - Story #143).

        Takes first 50 chars, removes newlines, strips whitespace.
        Returns "New Session" if result is empty.

        Args:
            first_prompt: User's first prompt text

        Returns:
            Generated session name (max 50 chars)
        """
        import re

        # Take first 50 chars
        name = first_prompt[:50]

        # Replace newlines and carriage returns with spaces
        name = name.replace("\n", " ").replace("\r", " ")

        # Collapse multiple spaces into single space
        name = re.sub(r"\s+", " ", name)

        # Strip whitespace
        name = name.strip()

        # Return default if empty
        if not name:
            return "New Session"

        return name

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """
        Rename a research session (AC4 - Story #143).

        Validates name and updates in database.
        Validation rules:
        - Length: 1-100 characters
        - Characters: letters, numbers, spaces, hyphens only

        Args:
            session_id: Session ID to rename
            new_name: New name for session

        Returns:
            True if renamed successfully, False if validation failed or session not found
        """
        import re

        # Validate length
        if len(new_name) < 1 or len(new_name) > 100:
            return False

        # Validate characters: only letters, numbers, spaces, and hyphens
        if not re.match(r"^[a-zA-Z0-9\s\-]+$", new_name):
            return False

        # Update in database
        conn = self._get_connection()
        try:
            # Check if session exists first
            cursor = conn.execute(
                "SELECT id FROM research_sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone() is None:
                return False

            # Update name and updated_at
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE research_sessions SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, now, session_id),
            )
            conn.commit()
            return True

        finally:
            conn.close()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single research session by ID (AC3 - Story #143).

        Args:
            session_id: Session ID to retrieve

        Returns:
            Session dictionary or None if not found
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, name, folder_path, created_at, updated_at "
                "FROM research_sessions "
                "WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

        finally:
            conn.close()

    def get_all_sessions(self) -> List[Dict[str, Any]]:
        """
        Get all research sessions ordered by updated_at DESC (AC1 - Story #143).

        Returns:
            List of session dictionaries, most recently updated first
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, name, folder_path, created_at, updated_at "
                "FROM research_sessions "
                "ORDER BY updated_at DESC"
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        finally:
            conn.close()

    def _get_unique_session_name(
        self, conn: sqlite3.Connection, base_name: str = "New Session"
    ) -> str:
        """
        Generate unique session name by adding number suffix if needed.

        Args:
            conn: Active database connection (to maintain transaction consistency)
            base_name: Base name for the session (default: "New Session")

        Returns:
            Unique session name (e.g., "New Session", "New Session 2", "New Session 3")
        """
        # Get all existing session names that start with base_name
        cursor = conn.execute(
            "SELECT name FROM research_sessions WHERE name = ? OR name LIKE ?",
            (base_name, f"{base_name} %"),
        )
        existing_names = {row["name"] for row in cursor.fetchall()}

        # If base name is available, use it
        if base_name not in existing_names:
            return base_name

        # Find next available number with upper bound for defensive programming
        counter = 2
        max_counter = 10000
        while counter < max_counter and f"{base_name} {counter}" in existing_names:
            counter += 1

        # Fallback to UUID suffix if somehow 10000+ sessions exist with this base name
        if counter >= max_counter:
            return f"{base_name} {uuid.uuid4().hex[:8]}"

        return f"{base_name} {counter}"

    def create_session(self) -> Dict[str, Any]:
        """
        Create a new research session (AC2 - Story #143).

        Creates:
        - Session with UUID in database
        - Session folder at ~/.cidx-server/research/{uuid}/
        - Softlink to code-indexer source in session folder

        Returns:
            Dictionary with session data (id, name, folder_path, created_at, updated_at)
        """
        # Generate UUID for session
        session_id = str(uuid.uuid4())

        # Create folder path
        folder_path = str(Path.home() / ".cidx-server" / "research" / session_id)

        # Create timestamps
        now = datetime.now(timezone.utc).isoformat()

        # Insert into database
        conn = self._get_connection()
        try:
            # BEGIN IMMEDIATE acquires a write lock before reading,
            # preventing race conditions where concurrent creates both see
            # the same state and create duplicate "New Session" names
            conn.execute("BEGIN IMMEDIATE")

            # Generate unique session name within same transaction
            session_name = self._get_unique_session_name(conn)

            conn.execute(
                "INSERT INTO research_sessions (id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, session_name, folder_path, now, now),
            )
            conn.commit()

            # Ensure folder and softlink exist
            self._ensure_session_folder_setup(folder_path)

            # Return the created session
            cursor = conn.execute(
                "SELECT id, name, folder_path, created_at, updated_at "
                "FROM research_sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            return dict(row)

        finally:
            conn.close()

    def _ensure_session_folder_setup(self, folder_path: str) -> None:
        """
        Ensure session folder exists and contains softlink to code-indexer source (AC3).

        Args:
            folder_path: Path to session folder
        """
        folder = Path(folder_path)

        # Create folder if it doesn't exist
        folder.mkdir(parents=True, exist_ok=True)

        # Detect CIDX repo root (used for both code-indexer and issue_manager symlinks)
        cidx_repo_root = os.environ.get("CIDX_REPO_ROOT")
        if not cidx_repo_root:
            # Try to find repo root from this file's location
            # This file is in src/code_indexer/server/services/
            current_file = Path(__file__).resolve()

            # Walk up looking for repo markers (pyproject.toml + src/code_indexer)
            for parent in [current_file] + list(current_file.parents):
                if (parent / "pyproject.toml").exists() and (
                    parent / "src" / "code_indexer"
                ).exists():
                    cidx_repo_root = str(parent)
                    break

        # Create softlink to code-indexer source
        softlink = folder / "code-indexer"
        if not softlink.exists():
            if cidx_repo_root:
                source_path = Path(cidx_repo_root)
                if source_path.exists():
                    # Create softlink
                    softlink.symlink_to(source_path)
                    logger.info(f"Created softlink: {softlink} -> {source_path}")
                else:
                    logger.warning(f"CIDX source path not found: {source_path}")
            else:
                logger.warning(
                    "CIDX_REPO_ROOT not set and could not auto-detect repo root"
                )

        # Create symlink to issue_manager.py for GitHub bug report creation (Story #202 AC4)
        # Primary: bundled copy within CIDX codebase
        # Fallback: ~/.claude/scripts/utils/issue_manager.py (developer environments)
        issue_manager_source = None
        if cidx_repo_root:
            bundled_path = (
                Path(cidx_repo_root)
                / "src"
                / "code_indexer"
                / "server"
                / "scripts"
                / "issue_manager.py"
            )
            if bundled_path.exists():
                issue_manager_source = bundled_path

        if not issue_manager_source:
            fallback_path = Path.home() / ".claude" / "scripts" / "utils" / "issue_manager.py"
            if fallback_path.exists():
                issue_manager_source = fallback_path

        issue_manager_link = folder / "issue_manager.py"

        # Handle broken symlinks: is_symlink() returns True but exists() returns False
        if issue_manager_link.is_symlink() and not issue_manager_link.exists():
            issue_manager_link.unlink()
            logger.info(f"Removed broken symlink: {issue_manager_link}")

        if not issue_manager_link.exists():
            if issue_manager_source:
                issue_manager_link.symlink_to(issue_manager_source)
                logger.info(
                    f"Created issue_manager.py symlink: {issue_manager_link} -> {issue_manager_source}"
                )
            else:
                logger.warning(
                    "issue_manager.py not found in CIDX codebase or ~/.claude/scripts/utils/, skipping symlink"
                )

    def add_message(self, session_id: str, role: str, content: str) -> Dict[str, Any]:
        """
        Add a message to a research session (AC6).

        Args:
            session_id: Session ID
            role: Message role ('user' or 'assistant')
            content: Message content

        Returns:
            Dictionary with message data (id, session_id, role, content, created_at)

        Raises:
            sqlite3.IntegrityError: If session doesn't exist or role is invalid
        """
        conn = self._get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()

            cursor = conn.execute(
                "INSERT INTO research_messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            conn.commit()

            # Get the inserted message
            message_id = cursor.lastrowid
            cursor = conn.execute(
                "SELECT id, session_id, role, content, created_at "
                "FROM research_messages WHERE id = ?",
                (message_id,),
            )
            row = cursor.fetchone()
            return dict(row)

        finally:
            conn.close()

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Get all messages for a session in chronological order (AC6).

        Args:
            session_id: Session ID

        Returns:
            List of message dictionaries ordered by created_at (oldest first)
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, role, content, created_at "
                "FROM research_messages "
                "WHERE session_id = ? "
                "ORDER BY id ASC",  # Using id for ordering (auto-increment)
                (session_id,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        finally:
            conn.close()

    def execute_prompt(self, session_id: str, user_prompt: str) -> str:
        """
        Execute a Claude prompt with security guardrails (AC4, AC5).

        Prepends security guardrails only to the first user message.
        Spawns background thread to execute Claude CLI.

        Args:
            session_id: Session ID
            user_prompt: User's question/prompt

        Returns:
            Job ID for polling status
        """
        # Get existing messages to determine if this is first prompt
        messages = self.get_messages(session_id)
        is_first_prompt = len(messages) == 0

        # AC5: Prepend security guardrails only to first prompt sent to Claude
        if is_first_prompt:
            # Load parametrized prompt template (falls back to hardcoded if needed)
            security_prompt = self.load_research_prompt()
            claude_prompt = security_prompt + "\n\nUser's request:\n" + user_prompt
        else:
            claude_prompt = user_prompt

        # Store ONLY user's original message in database (not guardrails)
        self.add_message(session_id, "user", user_prompt)

        # Create job for background execution
        job_id = str(uuid.uuid4())

        with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "running",
                "session_id": session_id,
                "user_prompt": user_prompt,
                "response": None,
                "error": None,
            }

        # Story #314: Register research_assistant_chat in JobTracker (dual tracking).
        # Keep existing _jobs dict as primary for poll_job() compatibility.
        if self._job_tracker is not None:
            try:
                self._job_tracker.register_job(
                    job_id, "research_assistant_chat", username="system"
                )
                self._job_tracker.update_status(job_id, status="running")
            except Exception:
                pass  # Tracker failure must never break chat execution

        # Start background thread to execute Claude (with guardrails if first message)
        thread = threading.Thread(
            target=self._run_claude_background,
            args=(job_id, session_id, claude_prompt, is_first_prompt),
            daemon=True,
        )
        thread.start()

        return job_id

    def poll_job(self, job_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Poll status of a Claude execution job (AC4).

        Bug #151 Fix: When job not found in memory (server restart, job expiry),
        falls back to checking database for messages. If messages exist,
        returns complete status to recover lost job state.

        Args:
            job_id: Job ID returned by execute_prompt
            session_id: Optional session ID for database fallback

        Returns:
            Dictionary with status, response (if complete), error (if failed),
            and session_id for message retrieval
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job:
                # Job found in memory - use it (normal path)
                return {
                    "status": job["status"],
                    "response": job.get("response"),
                    "error": job.get("error"),
                    "session_id": job.get("session_id"),
                }

        # Job not found in memory - try database fallback (Bug #151 fix)
        if session_id:
            # Check if messages exist in database for this session
            messages = self.get_messages(session_id)

            if messages:
                # Messages exist - job likely completed but was lost
                # Find the last assistant message as the response
                assistant_messages = [m for m in messages if m["role"] == "assistant"]

                if assistant_messages:
                    # Have assistant response - job completed
                    last_response = assistant_messages[-1]["content"]
                    return {
                        "status": "complete",
                        "response": last_response,
                        "session_id": session_id,
                        "fallback": True,  # Indicate this came from database
                    }
                else:
                    # Only user messages - job may still be running or failed
                    return {
                        "status": "error",
                        "error": "Job not found in memory and no assistant response in database",
                        "session_id": session_id,
                    }

        # No session_id or no messages found
        return {"status": "error", "error": "Job not found"}

    def get_job_session_id(self, job_id: str) -> Optional[str]:
        """
        Get the session ID associated with a job.

        Args:
            job_id: Job ID to look up

        Returns:
            Session ID or None if job not found
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return job.get("session_id") if job else None

    def _run_claude_background(
        self, job_id: str, session_id: str, claude_prompt: str, is_first_prompt: bool
    ) -> None:
        """
        Background worker to execute Claude CLI (AC4).

        Bug #153 Fix: Implements retry logic for Claude session handling:
        - First message: Use --session-id (creates new session)
        - Subsequent messages: Try --resume first
        - Retry: If --resume fails with "No conversation found" or "not found",
          retry with --session-id

        Args:
            job_id: Job ID for tracking
            session_id: Session ID (used as Claude session ID for continuity)
            claude_prompt: Prompt to send to Claude (includes guardrails for first message)
            is_first_prompt: True if this is the first message in the session
        """
        try:
            # Get session to get folder path
            session = self.get_session(session_id)
            if not session:
                session = self.get_default_session()
            working_dir = Path(session["folder_path"])

            # Load timeout and analysis_model from config (defaults: 1200s, "opus")
            timeout_seconds = 1200
            analysis_model = "opus"
            try:
                from code_indexer.server.utils.config_manager import ServerConfigManager
                config_manager = ServerConfigManager()
                config = config_manager.load_config()
                if config and config.claude_integration_config:
                    timeout_seconds = config.claude_integration_config.research_assistant_timeout_seconds
                if config and config.golden_repos_config:
                    analysis_model = config.golden_repos_config.analysis_model
            except Exception as e:
                logger.warning(f"Failed to load research assistant config, using defaults (timeout={timeout_seconds}s, model={analysis_model}): {e}")

            # Get the stored Claude session ID from database
            claude_session_id = self._get_or_create_claude_session_id(session_id)

            # Prepare environment with GitHub token if available (Story #202 AC3)
            env = os.environ.copy()
            if self._github_token:
                env["GITHUB_TOKEN"] = self._github_token
                env["GH_TOKEN"] = self._github_token

            # Build base command
            base_cmd = ["claude", "--dangerously-skip-permissions", "--model", analysis_model]

            # Bug #153 Fix: Use --session-id for first message, --resume for subsequent
            if is_first_prompt:
                # First message - create new session
                cmd = base_cmd + ["--session-id", claude_session_id, "-p", claude_prompt]
                result = subprocess.run(
                    cmd,
                    cwd=str(working_dir),
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=env,
                )
            else:
                # Subsequent message - try resume first
                cmd = base_cmd + ["--resume", claude_session_id, "-p", claude_prompt]
                result = subprocess.run(
                    cmd,
                    cwd=str(working_dir),
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=env,
                )

                # If resume failed due to missing session, retry with --session-id
                if result.returncode != 0 and (
                    "No conversation found" in result.stderr
                    or "not found" in result.stderr.lower()
                ):
                    logger.info(
                        f"Resume failed (session cleared or expired), retrying with --session-id: {result.stderr}"
                    )
                    cmd = base_cmd + ["--session-id", claude_session_id, "-p", claude_prompt]
                    result = subprocess.run(
                        cmd,
                        cwd=str(working_dir),
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        env=env,
                    )

            if result.returncode == 0:
                response = result.stdout.strip()

                # Store assistant response in database
                self.add_message(session_id, "assistant", response)

                # Update job status
                with self._jobs_lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "complete"
                        self._jobs[job_id]["response"] = response
                # Story #314: Track completion in JobTracker for dashboard
                if self._job_tracker is not None:
                    try:
                        self._job_tracker.complete_job(job_id)
                    except Exception as e:
                        logger.debug(f"Failed to mark job {job_id} complete in tracker: {e}")
            else:
                error = result.stderr.strip()
                logger.error(f"Claude CLI failed: {error}")

                with self._jobs_lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "error"
                        self._jobs[job_id]["error"] = error
                # Story #314: Track failure in JobTracker for dashboard
                if self._job_tracker is not None:
                    try:
                        self._job_tracker.fail_job(job_id, error=error)
                    except Exception as e:
                        logger.debug(f"Failed to mark job {job_id} failed in tracker: {e}")

        except subprocess.TimeoutExpired:
            error = "Claude CLI execution timed out"
            logger.error(error)
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "error"
                    self._jobs[job_id]["error"] = error
            # Story #314: Track timeout failure in JobTracker for dashboard
            if self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(job_id, error=error)
                except Exception as e:
                    logger.debug(f"Failed to mark job {job_id} failed in tracker: {e}")

        except Exception as e:
            error = f"Claude CLI execution failed: {e}"
            logger.error(error)
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "error"
                    self._jobs[job_id]["error"] = str(e)
            # Story #314: Track generic failure in JobTracker for dashboard
            if self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(job_id, error=str(e))
                except Exception as ex:
                    logger.debug(f"Failed to mark job {job_id} failed in tracker: {ex}")

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename for safe filesystem storage (AC2 - Story #144).

        Removes:
        - Path separators (/ and \\)
        - Null bytes and control characters
        Replaces:
        - Spaces with underscores
        Limits:
        - Length to 255 characters (preserving extension)

        Args:
            filename: Original filename to sanitize

        Returns:
            Sanitized filename safe for filesystem
        """
        # Remove path separators
        name = filename.replace("/", "").replace("\\", "")

        # Remove null bytes and control characters (ASCII 0-31)
        name = "".join(char for char in name if ord(char) >= 32)

        # Replace spaces with underscores
        name = name.replace(" ", "_")

        # Handle empty result
        if not name:
            return "unnamed_file"

        # Limit length to 255 chars while preserving extension
        if len(name) > 255:
            # Split into name and extension
            if "." in name:
                parts = name.rsplit(".", 1)
                base = parts[0]
                ext = "." + parts[1]
                # Truncate base to fit with extension
                max_base_len = 255 - len(ext)
                base = base[:max_base_len]
                name = base + ext
            else:
                name = name[:255]

        return name

    def get_unique_filename(self, upload_dir: Path, filename: str) -> str:
        """
        Get unique filename by adding suffix if file exists (AC2 - Story #144).

        If file.txt exists, returns file_1.txt.
        If file_1.txt also exists, returns file_2.txt, etc.

        Args:
            upload_dir: Directory where file will be uploaded
            filename: Desired filename

        Returns:
            Unique filename (original or with _N suffix)
        """
        file_path = upload_dir / filename

        if not file_path.exists():
            return filename

        # File exists - add suffix
        if "." in filename:
            parts = filename.rsplit(".", 1)
            base = parts[0]
            ext = "." + parts[1]
        else:
            base = filename
            ext = ""

        counter = 1
        max_counter = 10000  # Prevent infinite loops
        while counter < max_counter:
            new_filename = f"{base}_{counter}{ext}"
            new_path = upload_dir / new_filename

            if not new_path.exists():
                return new_filename

            counter += 1

        # Fallback if max counter reached
        import uuid

        return f"{base}_{uuid.uuid4().hex[:8]}{ext}"

    def get_session_upload_size(self, session_id: str) -> int:
        """
        Get total bytes uploaded to session (AC6 - Story #144).

        Sums file sizes in session uploads folder.

        Args:
            session_id: Session ID

        Returns:
            Total bytes uploaded (0 if no uploads folder)
        """
        session = self.get_session(session_id)
        if not session:
            return 0

        uploads_dir = Path(session["folder_path"]) / "uploads"
        if not uploads_dir.exists():
            return 0

        total_size = 0
        for file_path in uploads_dir.iterdir():
            if file_path.is_file():
                total_size += file_path.stat().st_size

        return total_size

    def upload_file(self, session_id: str, file: Any) -> Dict[str, Any]:
        """
        Upload file to session uploads folder (AC2, AC6, AC7 - Story #144).

        Validates:
        - Session exists
        - File extension in ALLOWED_EXTENSIONS
        - File size under MAX_FILE_SIZE (10MB)
        - Session total under MAX_SESSION_SIZE (100MB)

        Creates uploads folder if needed, sanitizes filename, handles duplicates.

        Args:
            session_id: Session ID
            file: FastAPI UploadFile object

        Returns:
            Dict with success/error/filename/size/uploaded_at
        """
        try:
            # AC7: Validate session exists
            session = self.get_session(session_id)
            if not session:
                return {"success": False, "error": "Session not found"}

            # AC6: Validate file extension
            filename = file.filename
            ext = Path(filename).suffix.lower()

            if ext not in ALLOWED_EXTENSIONS:
                return {
                    "success": False,
                    "error": f"File type {ext} not allowed. Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                }

            # Read file content
            content = file.file.read()
            file_size = len(content)

            # AC6: Validate file size
            if file_size > MAX_FILE_SIZE:
                return {
                    "success": False,
                    "error": f"File size {file_size / (1024 * 1024):.1f}MB exceeds maximum of 10MB",
                }

            # AC6: Validate session total size
            current_size = self.get_session_upload_size(session_id)
            if current_size + file_size > MAX_SESSION_SIZE:
                return {
                    "success": False,
                    "error": f"Session upload limit reached. Current: {current_size / (1024 * 1024):.1f}MB, Limit: 100MB",
                }

            # AC2: Create uploads folder
            uploads_dir = Path(session["folder_path"]) / "uploads"
            uploads_dir.mkdir(exist_ok=True)

            # AC2: Sanitize filename
            safe_filename = self.sanitize_filename(filename)

            # AC2: Get unique filename
            unique_filename = self.get_unique_filename(uploads_dir, safe_filename)

            # Save file
            file_path = uploads_dir / unique_filename
            with open(file_path, "wb") as f:
                f.write(content)

            logger.info(
                f"Uploaded file {unique_filename} ({file_size} bytes) to session {session_id}"
            )

            return {
                "success": True,
                "filename": unique_filename,
                "size": file_size,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.error(f"File upload failed: {e}")
            return {"success": False, "error": f"Upload failed: {str(e)}"}

    def list_files(self, session_id: str) -> List[Dict[str, Any]]:
        """
        List all uploaded files for a session (AC4 - Story #144).

        Returns file metadata: filename, size, uploaded_at.

        Args:
            session_id: Session ID

        Returns:
            List of file info dicts (empty if no uploads folder or no files)
        """
        session = self.get_session(session_id)
        if not session:
            return []

        uploads_dir = Path(session["folder_path"]) / "uploads"
        if not uploads_dir.exists():
            return []

        files = []
        for file_path in uploads_dir.iterdir():
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    {
                        "filename": file_path.name,
                        "size": stat.st_size,
                        "uploaded_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )

        # Sort by upload time (newest first)
        files.sort(key=lambda f: f["uploaded_at"], reverse=True)

        return files

    def delete_file(self, session_id: str, filename: str) -> bool:
        """
        Delete uploaded file from session (AC4 - Story #144).

        Args:
            session_id: Session ID
            filename: Filename to delete

        Returns:
            True if deleted, False if file not found or session not found
        """
        session = self.get_session(session_id)
        if not session:
            return False

        # Sanitize filename to prevent path traversal
        safe_filename = self.sanitize_filename(filename)

        uploads_dir = Path(session["folder_path"]) / "uploads"
        file_path = (uploads_dir / safe_filename).resolve()

        # Verify path is within uploads directory (path traversal protection)
        try:
            file_path.relative_to(uploads_dir.resolve())
        except ValueError:
            logger.warning(f"Path traversal attempt detected: {filename}")
            return False

        if not file_path.exists() or not file_path.is_file():
            return False

        try:
            file_path.unlink()
            logger.info(f"Deleted file {safe_filename} from session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete file {safe_filename}: {e}")
            return False

    def get_file_path(self, session_id: str, filename: str) -> Optional[Path]:
        """
        Get path to uploaded file for download (AC4 - Story #144).

        Args:
            session_id: Session ID
            filename: Filename

        Returns:
            Path object if file exists, None otherwise
        """
        session = self.get_session(session_id)
        if not session:
            return None

        # Sanitize filename to prevent path traversal
        safe_filename = self.sanitize_filename(filename)

        uploads_dir = Path(session["folder_path"]) / "uploads"
        file_path = (uploads_dir / safe_filename).resolve()

        # Verify path is within uploads directory (path traversal protection)
        try:
            file_path.relative_to(uploads_dir.resolve())
        except ValueError:
            logger.warning(f"Path traversal attempt detected: {filename}")
            return None

        if file_path.exists() and file_path.is_file():
            return file_path

        return None
