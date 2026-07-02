"""Langfuse README Generator (Story #592).

Generates README.md files at the repo root and per-session level for Langfuse
trace repos, enabling AI agents to navigate session archives via cidx.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

README_VERSION_MARKER = "<!-- cidx-readme-v1 -->"


class LangfuseReadmeGenerator:
    """Generates README.md files for Langfuse trace repository folders.

    Writes a root README with a session index table and per-session READMEs
    listing all trace files in chronological order.

    Atomic writes are used throughout (temp file + rename) to avoid partial
    reads by cidx during indexing.
    """

    def generate_for_repo(
        self, repo_path: Path, modified_session_ids: Set[str]
    ) -> None:
        """Generate README files for a Langfuse trace repo.

        Args:
            repo_path: Root directory of the repo (e.g. golden-repos/langfuse_Claude_Code_seba/).
            modified_session_ids: Set of session folder names that received new/updated traces.
        """
        # Collect all session folders (directories that are not .git)
        session_folders: List[Path] = []
        if repo_path.exists():
            session_folders = sorted(
                [d for d in repo_path.iterdir() if d.is_dir() and d.name != ".git"],
                key=lambda d: d.stat().st_mtime,
            )

        # Build session rows for root README
        session_rows = self._build_session_rows(session_folders)

        # Root README — only rewrite if content changed
        root_readme = repo_path / "README.md"
        existing_content = (
            root_readme.read_text(encoding="utf-8") if root_readme.exists() else ""
        )
        if not self._should_skip_root(existing_content, session_rows):
            content = self._render_root_readme(repo_path, session_rows)
            root_readme.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(root_readme, content)

        # Per-session READMEs — only for sessions that received new/updated traces
        for session_id in modified_session_ids:
            session_path = repo_path / session_id
            if not session_path.is_dir():
                continue
            content = self._render_session_readme(session_id, session_path)
            self._atomic_write(session_path / "README.md", content)

    def _should_skip_root(
        self, existing_content: str, current_session_rows: List[Dict[str, Any]]
    ) -> bool:
        """Return True only if version marker present AND session table unchanged.

        Args:
            existing_content: Current content of root README (empty string if missing).
            current_session_rows: Freshly-computed session rows to compare against.
        """
        if README_VERSION_MARKER not in existing_content:
            return False

        # Build a canonical fingerprint of current session rows
        current_fingerprint = self._session_rows_fingerprint(current_session_rows)

        # Extract fingerprint from existing README
        existing_fingerprint = self._extract_fingerprint_from_readme(existing_content)

        return current_fingerprint == existing_fingerprint

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write content atomically using temp file + rename.

        Args:
            path: Target file path.
            content: UTF-8 text to write.
        """
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)

    # ------------------------------------------------------------------
    # Private rendering methods
    # ------------------------------------------------------------------

    def _build_session_rows(self, session_folders: List[Path]) -> List[Dict[str, Any]]:
        """Build structured data for each session folder."""
        rows = []
        for folder in session_folders:
            files = sorted(folder.glob("*.json"), key=lambda f: f.name)
            turns = [f for f in files if "_turn_" in f.name]
            subagents = [f for f in files if "_subagent-" in f.name]

            last_prompt = ""
            last_date = ""

            if turns:
                last_turn_data = self._read_trace_json(turns[-1])
                if last_turn_data is not None:
                    raw_input = last_turn_data.get("trace", {}).get("input", "") or ""
                    last_prompt = str(raw_input)[:80].replace("\n", " ")
                    ts = last_turn_data.get("trace", {}).get("timestamp", "") or ""
                    last_date = str(ts)[:10]

            rows.append(
                {
                    "session_id": folder.name,
                    "date": last_date,
                    "turns": len(turns),
                    "subagents": len(subagents),
                    "last_prompt": last_prompt,
                }
            )
        return rows

    def _render_root_readme(
        self, repo_path: Path, session_rows: List[Dict[str, Any]]
    ) -> str:
        """Render the root README content."""
        # Extract project/user context from repo folder name if possible
        # Folder name pattern: langfuse_{project}_{user_id}
        repo_name = repo_path.name
        parts = repo_name.split("_", 2)
        project = parts[1] if len(parts) > 1 else repo_name
        user_id = parts[2] if len(parts) > 2 else "unknown"

        # Build session table rows
        table_lines = []
        for i, row in enumerate(session_rows, start=1):
            last_prompt = row["last_prompt"].replace("|", "\\|")
            table_lines.append(
                f"| {i} | {row['session_id']} | {row['date']} | {row['turns']} | {row['subagents']} | {last_prompt} |"
            )
        session_table = (
            "\n".join(table_lines) if table_lines else "| — | — | — | — | — | — |"
        )

        # Embed fingerprint as HTML comment for skip-check
        fingerprint = self._session_rows_fingerprint(session_rows)

        return f"""{README_VERSION_MARKER}
<!-- cidx-fingerprint:{fingerprint} -->
# Langfuse Trace Repository: {project} / {user_id}

This repository contains Claude Code session traces exported from Langfuse.
Each subfolder is one Claude Code session (identified by session ID).

## File Naming Convention

Files within each session folder are named: `{{NNN}}_{{type}}_{{shortId}}.json`

- `NNN` - three-digit sequence number (chronological order)
- `turn` - a main conversation turn (user prompt + Claude's response)
- `subagent-{{name}}` - a delegated subagent execution (e.g., `subagent-tdd-engineer`)

## How to Read a Turn File

Each `NNN_turn_*.json` file contains a complete conversation turn:

```json
{{
  "trace": {{
    "input": "<the user's message>",
    "output": "<Claude's final response after all tool calls>"
  }},
  "observations": [
    {{ "name": "Assistant Response", "output": "Let me look at this..." }},
    {{ "name": "Tool - Read", "input": {{"file_path": "..."}}, "output": "..." }}
  ]
}}
```

**Reading order**: `trace.input` -> `observations[]` (sorted by `startTime`) -> `trace.output`

## Sessions

| # | Session ID | Date | Turns | Subagent Calls | Last Prompt |
|---|-----------|------|-------|----------------|-------------|
{session_table}

_Generated by cidx Langfuse sync. Updated automatically on each sync._
"""

    def _render_session_readme(self, session_id: str, session_path: Path) -> str:
        """Render per-session README content."""
        files = sorted(session_path.glob("*.json"), key=lambda f: f.name)

        # Determine date range and counts
        first_timestamp = ""
        last_timestamp = ""
        turn_count = 0
        subagent_count = 0

        file_rows = []
        for f in files:
            if "_turn_" in f.name:
                turn_count += 1
                file_type = "turn"
            elif "_subagent-" in f.name:
                subagent_count += 1
                # Extract subagent name: NNN_subagent-{name}_shortid.json
                name_part = f.stem.split("_", 1)[1] if "_" in f.stem else f.stem
                file_type = (
                    name_part if name_part.startswith("subagent-") else "subagent"
                )
            else:
                file_type = "unknown"

            # Read timestamp and first prompt from trace
            trace_data = self._read_trace_json(f)
            ts = ""
            prompt_snippet = ""
            if trace_data is not None:
                ts = str(trace_data.get("trace", {}).get("timestamp", "") or "")[:19]
                raw_input = trace_data.get("trace", {}).get("input", "") or ""
                prompt_snippet = (
                    str(raw_input)[:80].replace("\n", " ").replace("|", "\\|")
                )

            if ts:
                if not first_timestamp or ts < first_timestamp:
                    first_timestamp = ts
                if not last_timestamp or ts > last_timestamp:
                    last_timestamp = ts

            file_rows.append(f"| {f.name} | {file_type} | {ts} | {prompt_snippet} |")

        table = "\n".join(file_rows) if file_rows else "| — | — | — | — |"
        date_range = (
            f"{first_timestamp} -> {last_timestamp}" if first_timestamp else "unknown"
        )

        return f"""{README_VERSION_MARKER}
# Session: {session_id}

**Date range:** {date_range}
**Turns:** {turn_count} | **Subagent calls:** {subagent_count}

## Files (in chronological order)

| File | Type | Timestamp | User Prompt (first 80 chars) |
|------|------|-----------|------------------------------|
{table}

## How to Resume Work

1. Read files in sequence (001, 002, 003...)
2. Each `turn` file: `trace.input` = what the user asked, `trace.output` = final answer
3. Each `subagent-*` file: `trace.input` = task given to subagent, `trace.output` = result
4. The last turn's `trace.output` shows the final state of work

_Generated by cidx Langfuse sync. Updated automatically when session receives new traces._
"""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_trace_json(self, path: Path) -> Optional[Dict[str, Any]]:
        """Read and parse a trace JSON file, returning None on error."""
        try:
            result: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return result
        except Exception as e:
            logger.debug(f"Failed to read trace file {path}: {e}")
            return None

    def _session_rows_fingerprint(self, rows: List[Dict[str, Any]]) -> str:
        """Build a compact fingerprint string from session rows for change detection."""
        # Format: "session_id:turns:subagents,session_id2:turns:subagents"
        # Sort by session_id for determinism (mtime order changes after README writes)
        parts = sorted(
            [f"{r['session_id']}:{r['turns']}:{r['subagents']}" for r in rows]
        )
        return ",".join(parts)

    def _extract_fingerprint_from_readme(self, content: str) -> str:
        """Extract the cidx-fingerprint value from README content."""
        marker = "<!-- cidx-fingerprint:"
        end_marker = " -->"
        start = content.find(marker)
        if start == -1:
            return ""
        start += len(marker)
        end = content.find(end_marker, start)
        if end == -1:
            return ""
        return content[start:end]
