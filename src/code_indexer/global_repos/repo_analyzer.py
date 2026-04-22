"""
Repository Analyzer for extracting information from repositories.

Analyzes repository contents (README, package files, directory structure)
to extract metadata for generating semantic descriptions.

Supports Claude CLI integration for enhanced AI-powered analysis when
CIDX_USE_CLAUDE_FOR_META environment variable is set to 'true' (default).
Requires ANTHROPIC_API_KEY environment variable and 'claude' CLI in PATH.
"""

import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

logger = logging.getLogger(__name__)


def split_frontmatter_and_body(content: str) -> Tuple[Dict[str, Any], str]:
    """
    Split YAML frontmatter from markdown body.

    Expects content that may start with ``---`` followed by YAML and a
    closing ``---`` delimiter.  If no opening delimiter is found, or the
    closing delimiter is absent, returns an empty dict paired with the
    original content unchanged.

    Args:
        content: Raw file content, possibly starting with a ``---`` block.

    Returns:
        Tuple of (frontmatter_dict, body) where frontmatter_dict is the
        parsed YAML mapping and body is everything after the closing ``---``
        line. When no valid frontmatter is found both elements reflect the
        original content: ({}, content).
    """
    if not content.startswith("---"):
        logger.debug(
            "split_frontmatter_and_body: content has no opening '---' delimiter"
        )
        return {}, content

    # Find the closing delimiter (search starting after the opening "---")
    close_pos = content.find("---", 3)
    if close_pos == -1:
        logger.debug("split_frontmatter_and_body: no closing '---' delimiter found")
        return {}, content

    yaml_text = content[3:close_pos].strip()
    body = content[close_pos + 3 :]
    # Strip the newline immediately after the closing ---
    if body.startswith("\n"):
        body = body[1:]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        # Story #885 A9c: upgrade severity and add structured context so
        # operators can diagnose frontmatter write-side bugs quickly.
        # file_path is None because this function accepts raw content, not a
        # file path; callers that have a path should log it separately.
        first_line = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            first_line = exc.problem_mark.line + 1  # 1-indexed for display
        logger.error(
            "split_frontmatter_and_body: failed to parse frontmatter YAML: %s",
            exc,
            extra={
                "file_path": None,
                "first_offending_line": first_line,
            },
        )
        return {}, content

    if not isinstance(parsed, dict):
        logger.debug(
            "split_frontmatter_and_body: frontmatter parsed to %s, expected dict",
            type(parsed).__name__,
        )
        return {}, content

    return parsed, body


# Portable null device for the `script` pseudo-TTY wrapper.
# `script -q -c <cmd> <null>` discards the typescript; os.devnull is "/dev/null"
# on Unix and "nul" on Windows. Claude CLI itself requires Unix, but using
# os.devnull avoids hard-coding the path.
_SCRIPT_NULL_DEVICE = os.devnull


def _clean_claude_output(output: str) -> str:
    """
    Remove all terminal control sequences from Claude CLI stdout.

    Applies the cleaning sequence used by description_refresh_scheduler.py
    (lines 596-610) to strip CSI, OSC, ESC, script artifacts, and
    normalize line endings.

    Two CSI variants are handled (Bug #871):
    - ESC-prefixed: ``\\x1b[`` + params + final byte  (standard ECMA-48)
    - Bare (no ESC): ``[`` + params + final byte       (produced when the
      ``script`` pseudo-TTY wrapper strips the ESC byte from the stream, as
      observed in 182+ production failures since Epic #725 deploy)

    The bare-CSI pattern uses the optional private-parameter prefix
    ``[?<>=!]?`` to cover all production-observed variants ([>4m, [?25h,
    [?1004h, [0m) while remaining disjoint from YAML flow sequences
    (``[1, 2, 3]``): digits 0-9 are valid param bytes but are NOT in the
    final-byte range ``[@-~]`` (0x40-0x7e), so ``[1, 2, 3]`` never matches.
    """
    # CSI sequences: full ECMA-48 grammar — parameter bytes [0-?], intermediate bytes [ -/],
    # final bytes [@-~] (covers colors, cursor, private modes, intermediate byte variants).
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    # Bare CSI tails (Bug #871): same grammar but without the ESC prefix.
    # Produced when ``script -q -c ...`` strips the ESC byte from the stream.
    # Pattern requires either a private-param prefix ([?<>=!]) OR at least one
    # leading digit so that bare YAML identifiers like ``[repo-a, repo-b]``
    # do NOT match: ``r`` is in [@-~] but is not preceded by a digit or prefix.
    # All production-observed variants are covered: [>4m, [?25h, [?1004h, [0m.
    output = re.sub(r"(?m)\[(?:[?<>=!][0-9;]*|[0-9][0-9;]*)[ -/]*[@-~]", "", output)
    # OSC sequences: ESC ] ... BEL or ST
    output = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", output)
    # Other ESC sequences (ESC followed by a single char)
    output = re.sub(r"\x1b[^[\]()]", "", output)
    # Stray [<u artifacts from the script command
    output = re.sub(r"\[<u", "", output)
    # Strip remaining bare ESC bytes
    output = output.replace("\x1b", "")
    # Normalize line endings, then strip surrounding whitespace
    output = output.replace("\r\n", "\n").replace("\r", "")
    return output.strip()


def invoke_claude_cli(
    repo_path: str,
    prompt: str,
    shell_timeout_seconds: int,
    outer_timeout_seconds: int,
) -> tuple:
    """
    Shared parameterized subprocess wrapper for Claude CLI invocations.

    Consolidates the duplicated subprocess invocation pattern from
    description_refresh_scheduler._invoke_claude_cli and
    RepoAnalyzer._extract_info_with_claude into one implementation.

    Uses ``script -q -c ... <null>`` for pseudo-TTY in non-interactive
    environments, filters the subprocess environment to prevent inheriting
    parent agentic context, and delegates output cleaning to
    ``_clean_claude_output``.

    Args:
        repo_path: Non-empty working directory path for the subprocess (cwd).
        prompt: Non-empty prompt string to pass to ``claude -p``.
        shell_timeout_seconds: Positive int (not bool); inner ``timeout`` shell value (s).
        outer_timeout_seconds: Positive int (not bool) > shell_timeout_seconds;
            Python subprocess.run timeout. Typically shell_timeout + 60.

    Returns:
        Tuple of (success: bool, result: str).
        On success: (True, cleaned_stdout).
        On failure: (False, error_message).

    Raises:
        ValueError: If any parameter fails validation.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError(f"repo_path must be a non-empty string, got {repo_path!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"prompt must be a non-empty string, got {prompt!r}")
    if type(shell_timeout_seconds) is not int or shell_timeout_seconds <= 0:
        raise ValueError(
            f"shell_timeout_seconds must be a positive int, got {shell_timeout_seconds!r}"
        )
    if type(outer_timeout_seconds) is not int or outer_timeout_seconds <= 0:
        raise ValueError(
            f"outer_timeout_seconds must be a positive int, got {outer_timeout_seconds!r}"
        )
    if outer_timeout_seconds <= shell_timeout_seconds:
        raise ValueError(
            f"outer_timeout_seconds ({outer_timeout_seconds}) must exceed "
            f"shell_timeout_seconds ({shell_timeout_seconds})"
        )

    # A10 (Story #885): centralized MCP self-registration at the subprocess boundary.
    # All Claude CLI call sites funnel through invoke_claude_cli, so registering here
    # guarantees exactly-once registration per process without duplicating the call
    # in ClaudeCliManager._worker_loop or DependencyMapAnalyzer._run_claude_cli.
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    svc = MCPSelfRegistrationService.get_instance()
    if svc is not None:
        svc.ensure_registered()

    try:
        claude_cmd = (
            f"timeout {shell_timeout_seconds} claude "
            f"-p {shlex.quote(prompt)} --print --dangerously-skip-permissions"
        )
        full_cmd = ["script", "-q", "-c", claude_cmd, _SCRIPT_NULL_DEVICE]

        # Filter env: always drop CLAUDECODE; also drop ANTHROPIC_API_KEY when
        # CLAUDECODE is present to prevent inheriting parent agentic context.
        keys_to_drop = {"CLAUDECODE"}
        if "CLAUDECODE" in os.environ:
            keys_to_drop.add("ANTHROPIC_API_KEY")
        filtered_env = {k: v for k, v in os.environ.items() if k not in keys_to_drop}
        filtered_env["NO_COLOR"] = "1"

        result = subprocess.run(
            full_cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=outer_timeout_seconds,
            env=filtered_env,
        )

        if result.returncode != 0:
            error_msg = (
                f"Claude CLI returned non-zero: {result.returncode}, "
                f"stderr: {result.stderr}"
            )
            logger.warning(error_msg)
            return False, error_msg

        return True, _clean_claude_output(result.stdout)

    except subprocess.TimeoutExpired:
        error_msg = f"Claude CLI timed out after {outer_timeout_seconds}s"
        logger.warning(error_msg)
        return False, error_msg
    except Exception as exc:
        error_msg = f"Unexpected error during Claude CLI execution: {exc}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


@dataclass
class RepoInfo:
    """
    Repository information extracted by analyzer.

    Attributes:
        summary: High-level description of the repository
        technologies: List of technologies/languages detected
        features: List of key features
        use_cases: List of primary use cases
        purpose: Primary purpose of the repository
    """

    summary: str
    technologies: List[str]
    features: List[str]
    use_cases: List[str]
    purpose: str


class RepoAnalyzer:
    """
    Analyzes repository contents to extract information.

    Examines README files, package manifests, and directory structure
    to infer technologies, features, and purpose.
    """

    def __init__(
        self,
        repo_path: str,
        claude_cli_manager: Optional["ClaudeCliManager"] = None,
    ):
        """
        Initialize the repository analyzer.

        Args:
            repo_path: Path to the repository to analyze
            claude_cli_manager: Optional ClaudeCliManager for managed
                Claude CLI invocations (server mode). When provided,
                uses manager's CLI availability checking and API key
                synchronization. If None, uses direct invocation (CLI mode).
        """
        self.repo_path = Path(repo_path)
        self.claude_cli_manager = claude_cli_manager

    def get_prompt(
        self,
        mode: str,
        last_analyzed: Optional[str] = None,
        existing_description: Optional[str] = None,
        temp_file_path: Optional[Path] = None,
    ) -> str:
        """
        Get universal prompt for repository description generation (Story #190 AC1, AC6).

        Single universal prompt that teaches Claude to discover repo type dynamically
        by examining folder structure (.git directory, UUID folders, JSON trace files).

        Args:
            mode: Either "create" for initial generation or "refresh" for updating
            last_analyzed: ISO 8601 timestamp of last analysis (required for refresh mode)
            existing_description: Existing description text (required for refresh mode
                unless temp_file_path is provided)
            temp_file_path: Path to a file containing the existing description (Bug #840
                Site #5). When provided in refresh mode, the prompt instructs Claude to
                Read the file and Edit it in place rather than embedding the description
                inline. Mutually exclusive with existing_description for the inline path.

        Returns:
            Prompt string to send to Claude CLI

        Raises:
            ValueError: If mode is invalid or required parameters missing
        """
        if mode not in ("create", "refresh"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'create' or 'refresh'")

        if mode == "refresh":
            if last_analyzed is None:
                raise ValueError("last_analyzed is required for refresh mode")
            if temp_file_path is None and existing_description is None:
                raise ValueError(
                    "Either existing_description or temp_file_path is required for refresh mode"
                )

        if mode == "create":
            return self._get_create_prompt()

        if temp_file_path is not None:
            return self._get_refresh_prompt_via_file(
                last_analyzed or "", temp_file_path
            )

        return self._get_refresh_prompt(last_analyzed or "", existing_description or "")

    def _get_refresh_prompt_via_file(
        self, last_analyzed: str, temp_file_path: Path
    ) -> str:
        """
        Get refresh prompt that references an existing description via file path (Bug #840 Site #5).

        Instead of embedding the existing description inline (which bloats the prompt
        with large content), this method instructs Claude to Read the file at
        temp_file_path and Edit it in place with the updated description.

        Args:
            last_analyzed: ISO 8601 timestamp of last analysis.
            temp_file_path: Absolute path to the temp file containing the existing
                description. Claude is instructed to Read this file, not receive the
                content inline.

        Returns:
            Prompt string that references the file path for Read+Edit workflow.
        """
        return f"""Update the repository description based on changes since last analysis.

**Last Analyzed:** {last_analyzed}

**Existing Description File:** {temp_file_path}
Read the existing description at {temp_file_path} and apply a focused refresh edit.

**Instructions:**
1. Read the file at {temp_file_path} to get the current description.
2. Edit the file in place at {temp_file_path} with the updated description.
3. Do NOT output the full document to stdout — only edit the file directly.

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders with JSON trace files

**For Git Repositories:**
1. Run: git log --since="{last_analyzed}" --oneline
2. If material changes detected (not just cosmetic commits), update the description
3. If no material changes, return the existing description unchanged

**For Langfuse Trace Repositories:**
1. Find files modified after {last_analyzed} using file modification timestamps
2. IMPORTANT: Langfuse traces are immutable once established
3. Focus on NEW trace files only (files with modification time > last_analyzed)
4. Extract new findings from new traces and MERGE with existing (do not replace)

**Update Strategy:**
- Update description only if material changes detected
- Preserve existing YAML frontmatter structure
- Update last_analyzed timestamp to current time

**IMPORTANT:**
- Do NOT output the full document to stdout — edit the file at {temp_file_path} directly
- Output ONLY a brief status line to stdout (e.g. "Updated" or "No changes")
- Preserve all existing fields in YAML frontmatter
"""

    def _get_create_prompt(self) -> str:
        """
        Get universal initial description generation prompt.

        Teaches Claude to discover repo type by examining folder structure.
        """
        return """Analyze this repository and generate a comprehensive semantic description.

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders (e.g., 550e8400-e29b-41d4-a716-446655440000) with JSON trace files matching pattern NNN_turn_HASH.json

**For Git Repositories:**
Examine README, source files, and package files to extract:
- summary: 2-3 sentence description of what this repository does
- technologies: List of all technologies and tools detected
- features: Key features
- use_cases: Primary use cases
- purpose: One of: api, service, library, cli-tool, web-application, data-structure, utility, framework, general-purpose

**For Langfuse Trace Repositories:**
Extract intelligence from trace files (JSON files in UUID folders):
- user_identity: Extract from trace.userId field
- projects_detected: Extract from metadata.project_name field
- activity_summary: Summarize from trace.input and metadata.intel_task_type fields
- features: Key features based on trace patterns
- use_cases: Primary use cases inferred from traces

**Output Format:**
Generate YAML frontmatter + markdown body with these exact fields:
---
name: repository-name
repo_type: git|langfuse
technologies:
  - Technology 1
  - Technology 2
purpose: inferred-purpose
last_analyzed: (current timestamp)
user_identity: (Langfuse only - extracted user IDs)
projects_detected: (Langfuse only - list of project names)
---

# Repository Name

(Summary description)

## Key Features
- Feature 1
- Feature 2

## Technologies
- Tech 1
- Tech 2

## Primary Use Cases
- Use case 1
- Use case 2

## Activity Summary (Langfuse only)
(Summary of user activities based on traces)

**IMPORTANT:**
- Set repo_type field in YAML frontmatter to "git" or "langfuse"
- For Langfuse repos, include user_identity, projects_detected, and activity_summary sections
- Output ONLY the YAML + markdown (no explanations, no code blocks)
"""

    def _get_refresh_prompt(self, last_analyzed: str, existing_description: str) -> str:
        """
        Get universal refresh prompt for updating existing descriptions.

        Teaches Claude to detect changes and update accordingly.
        """
        return f"""Update the repository description based on changes since last analysis.

**Last Analyzed:** {last_analyzed}

**Existing Description:**
{existing_description}

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders with JSON trace files

**For Git Repositories:**
1. Run: git log --since="{last_analyzed}" --oneline
2. If material changes detected (not just cosmetic commits), update the description
3. If no material changes, return the existing description unchanged

**For Langfuse Trace Repositories:**
1. Find files modified after {last_analyzed} using file modification timestamps
2. IMPORTANT: Langfuse traces are immutable once established
3. Focus on NEW trace files only (files with modification time > last_analyzed)
4. Extract new findings from new traces:
   - New user IDs from trace.userId
   - New projects from metadata.project_name
   - New activities from trace.input and metadata.intel_task_type
5. MERGE new findings with existing description (preserve existing user_identity and projects_detected)
6. DO NOT replace existing data - only ADD new discoveries

**Update Strategy:**
- Update description only if material changes detected
- Preserve existing YAML frontmatter structure
- For Langfuse: merge new findings, don't replace
- Update last_analyzed timestamp to current time

**Output Format:**
Return updated YAML frontmatter + markdown body with same structure as original.
Include repo_type field in YAML.
If no material changes: return existing description with updated last_analyzed timestamp only.

**IMPORTANT:**
- Output ONLY the YAML + markdown (no explanations, no code blocks)
- Preserve all existing fields in YAML frontmatter
- For Langfuse: keep existing user_identity and projects_detected, only add new entries
"""

    def extract_info(self) -> RepoInfo:
        """
        Extract information from the repository.

        Uses Claude CLI for AI-powered analysis if available and enabled,
        otherwise falls back to static regex-based analysis.

        Returns:
            RepoInfo object containing extracted metadata
        """
        # Check if Claude is enabled (default: true)
        use_claude = (
            os.environ.get("CIDX_USE_CLAUDE_FOR_META", "true").lower() == "true"
        )

        if use_claude:
            claude_result = self._extract_info_with_claude()
            if claude_result is not None:
                return claude_result
            logger.info(
                "Claude analysis failed or unavailable, "
                "falling back to static analysis for %s",
                self.repo_path,
            )

        return self._extract_info_static()

    def _extract_info_with_claude(self) -> Optional[RepoInfo]:
        """
        Extract repository information using Claude CLI with tool support.

        Uses Claude Code CLI which can read files, explore directories,
        and provide much richer analysis than SDK-only approaches.

        If claude_cli_manager is provided, uses it for CLI availability
        checking and API key synchronization.

        Returns:
            RepoInfo if Claude succeeds and returns valid JSON,
            None otherwise (fallback to static analysis)
        """
        try:
            # Check if Claude CLI is available
            if self.claude_cli_manager is not None:
                # Use manager's CLI availability check (with caching)
                if not self.claude_cli_manager.check_cli_available():
                    logger.debug("Claude CLI not available (via manager)")
                    return None

                # Sync API key before invocation
                try:
                    self.claude_cli_manager.sync_api_key()
                except Exception as e:
                    logger.warning("API key sync failed: %s", e)
                    # Continue anyway - sync failure shouldn't block analysis
            else:
                # Direct check for CLI mode (no manager available)
                which_result = subprocess.run(
                    ["which", "claude"], capture_output=True, text=True, timeout=5
                )
                if which_result.returncode != 0:
                    logger.debug("Claude CLI not found in PATH")
                    return None

            # Build the analysis prompt
            prompt = """Analyze this repository. Examine the README, source files, and package files.
Output ONLY a JSON object (no markdown, no explanation) with these exact fields:
{
  "summary": "2-3 sentence description of what this repository does",
  "technologies": ["list", "of", "all", "technologies", "and", "tools", "detected"],
  "features": ["key feature 1", "key feature 2", ...],
  "use_cases": ["primary use case 1", "use case 2", ...],
  "purpose": "one of: api, service, library, cli-tool, web-application, data-structure, utility, framework, general-purpose"
}"""

            # Use script to provide pseudo-TTY (required for Claude CLI in non-interactive environments)
            # The command: script -q -c 'timeout 90 claude -p "..." --print --dangerously-skip-permissions' /dev/null
            claude_cmd = f"timeout 90 claude -p {shlex.quote(prompt)} --print --dangerously-skip-permissions"
            full_cmd = ["script", "-q", "-c", claude_cmd, "/dev/null"]

            result = subprocess.run(
                full_cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k
                    not in (
                        ("CLAUDECODE", "ANTHROPIC_API_KEY")
                        if "CLAUDECODE" in os.environ
                        else ("CLAUDECODE",)
                    )
                },
            )

            if result.returncode != 0:
                logger.debug("Claude CLI returned non-zero: %d", result.returncode)
                return None

            # Clean output (remove ANSI escape codes and carriage returns)
            output = result.stdout
            output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)  # Remove ANSI escapes
            output = output.replace("\r\n", "\n").replace(
                "\r", ""
            )  # Normalize line endings
            output = output.strip()

            # Extract JSON from response (may be wrapped in markdown code blocks)
            if "```json" in output:
                match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
                if match:
                    output = match.group(1)
            elif "```" in output:
                match = re.search(r"```\s*(.*?)\s*```", output, re.DOTALL)
                if match:
                    output = match.group(1)

            # Find JSON object in output
            json_match = re.search(
                r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", output, re.DOTALL
            )
            if json_match:
                output = json_match.group(0)

            # Parse JSON response
            data = json.loads(output)

            # Validate required fields
            required_fields = ["summary", "technologies", "purpose"]
            for field in required_fields:
                if field not in data:
                    logger.debug("Claude response missing required field: %s", field)
                    return None

            logger.info(
                "Successfully analyzed repository with Claude CLI: %s",
                self.repo_path.name,
            )

            return RepoInfo(
                summary=data["summary"],
                technologies=data.get("technologies", []),
                features=data.get("features", []),
                use_cases=data.get("use_cases", []),
                purpose=data.get("purpose", "general-purpose"),
            )

        except FileNotFoundError:
            logger.debug("script command not found")
            return None
        except subprocess.TimeoutExpired:
            logger.debug("Claude CLI timed out after 120 seconds")
            return None
        except json.JSONDecodeError as e:
            logger.debug("Failed to parse Claude CLI JSON response: %s", e)
            return None
        except Exception as e:
            logger.debug("Unexpected error during Claude CLI execution: %s", e)
            return None

    def _extract_info_static(self) -> RepoInfo:
        """
        Extract information using static regex-based analysis.

        Fallback method when Claude CLI is unavailable or disabled.

        Returns:
            RepoInfo object containing extracted metadata
        """
        summary = self._extract_summary()
        technologies = self._detect_technologies()
        features = self._extract_features()
        use_cases = self._extract_use_cases()
        purpose = self._infer_purpose()

        return RepoInfo(
            summary=summary,
            technologies=technologies,
            features=features,
            use_cases=use_cases,
            purpose=purpose,
        )

    def _extract_summary(self) -> str:
        """
        Extract summary from README or infer from structure.

        Returns:
            Repository summary string
        """
        readme = self._find_readme()
        if readme:
            content = readme.read_text()

            # Extract first meaningful paragraph after title
            lines = content.split("\n")
            summary_lines = []

            skip_title = False
            for line in lines:
                line = line.strip()

                # Skip title line
                if line.startswith("#"):
                    skip_title = True
                    continue

                # Collect first paragraph
                if skip_title and line:
                    summary_lines.append(line)
                    if len(" ".join(summary_lines)) > 50:
                        break

            if summary_lines:
                return " ".join(summary_lines)

        # Fallback: use repo name
        return f"A {self.repo_path.name} repository"

    def _detect_technologies(self) -> List[str]:
        """
        Detect technologies from package files and directory structure.

        Returns:
            List of detected technologies
        """
        technologies = []

        # Check for Python
        if (
            (self.repo_path / "setup.py").exists()
            or (self.repo_path / "pyproject.toml").exists()
            or (self.repo_path / "requirements.txt").exists()
            or self._has_python_files()
        ):
            technologies.append("Python")

        # Check for JavaScript/Node.js
        if (self.repo_path / "package.json").exists():
            technologies.append("JavaScript")
            technologies.append("Node.js")

        # Check for Rust
        if (self.repo_path / "Cargo.toml").exists():
            technologies.append("Rust")

        # Check for Go
        if (self.repo_path / "go.mod").exists():
            technologies.append("Go")

        # Check for Java
        if (self.repo_path / "pom.xml").exists() or (
            self.repo_path / "build.gradle"
        ).exists():
            technologies.append("Java")

        # Extract from README Technologies section
        readme_techs = self._extract_technologies_from_readme()
        technologies.extend(readme_techs)

        # Remove duplicates while preserving order
        seen = set()
        unique_techs = []
        for tech in technologies:
            if tech not in seen:
                seen.add(tech)
                unique_techs.append(tech)

        return unique_techs

    def _extract_features(self) -> List[str]:
        """
        Extract features from README.

        Returns:
            List of features
        """
        features = []
        readme = self._find_readme()

        if readme:
            content = readme.read_text()

            # Look for Features section
            features_section = self._extract_section(content, "Features")
            if features_section:
                # Extract bullet points
                for line in features_section.split("\n"):
                    line = line.strip()
                    if line.startswith("-") or line.startswith("*"):
                        feature = line.lstrip("-*").strip()
                        if feature:
                            features.append(feature)

        return features

    def _extract_use_cases(self) -> List[str]:
        """
        Extract use cases from README.

        Returns:
            List of use cases
        """
        use_cases = []
        readme = self._find_readme()

        if readme:
            content = readme.read_text()

            # Look for Use Cases section
            use_cases_section = self._extract_section(content, "Use Cases")
            if use_cases_section:
                # Extract bullet points
                for line in use_cases_section.split("\n"):
                    line = line.strip()
                    if line.startswith("-") or line.startswith("*"):
                        use_case = line.lstrip("-*").strip()
                        if use_case:
                            use_cases.append(use_case)

        return use_cases

    def _infer_purpose(self) -> str:
        """
        Infer repository purpose from name and content.

        Returns:
            Inferred purpose string
        """
        repo_name = self.repo_path.name

        # Common purpose keywords
        if "api" in repo_name.lower():
            return "api"
        if "service" in repo_name.lower():
            return "service"
        if "lib" in repo_name.lower() or "library" in repo_name.lower():
            return "library"
        if "cli" in repo_name.lower():
            return "cli-tool"
        if "web" in repo_name.lower():
            return "web-application"
        if "auth" in repo_name.lower():
            return "authentication"

        # Default
        return "general-purpose"

    def _find_readme(self) -> Optional[Path]:
        """
        Find README file in repository.

        Returns:
            Path to README or None if not found
        """
        for name in ["README.md", "README.rst", "README.txt", "README"]:
            readme = self.repo_path / name
            if readme.exists():
                return readme
        return None

    def _has_python_files(self) -> bool:
        """
        Check if repository contains Python files.

        Returns:
            True if Python files found
        """
        # Check for __init__.py
        for path in self.repo_path.rglob("__init__.py"):
            return True

        # Check for .py files
        for path in self.repo_path.rglob("*.py"):
            return True

        return False

    def _extract_technologies_from_readme(self) -> List[str]:
        """
        Extract technologies from README Technologies section.

        Returns:
            List of technologies found in README
        """
        technologies = []
        readme = self._find_readme()

        if readme:
            content = readme.read_text()

            # Look for Technologies section
            tech_section = self._extract_section(content, "Technologies")
            if tech_section:
                # Extract bullet points
                for line in tech_section.split("\n"):
                    line = line.strip()
                    if line.startswith("-") or line.startswith("*"):
                        tech = line.lstrip("-*").strip()
                        if tech:
                            technologies.append(tech)

        return technologies

    def _extract_section(self, content: str, section_name: str) -> Optional[str]:
        """
        Extract a section from markdown content.

        Args:
            content: Markdown content
            section_name: Name of section to extract

        Returns:
            Section content or None if not found
        """
        lines = content.split("\n")
        in_section = False
        section_lines = []

        for line in lines:
            # Check for section header
            if re.match(f"^##+ {section_name}", line, re.IGNORECASE):
                in_section = True
                continue

            # Stop at next section
            if in_section and line.startswith("##"):
                break

            # Collect section content
            if in_section:
                section_lines.append(line)

        if section_lines:
            return "\n".join(section_lines)

        return None
