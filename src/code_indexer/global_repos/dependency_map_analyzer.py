"""
Dependency Map Analyzer for Story #192 (Epic #191), updated Story #216.

Implements multi-pass Claude CLI pipeline to analyze source code across
all golden repositories and produce domain-clustered dependency documents.

Architecture:
- Pass 1 (Synthesis): Reads cidx-meta descriptions to identify domain clusters
- Pass 2 (Per-domain): Analyzes source code for each domain
- Index generation: Programmatic _generate_index_md() replaces the former Claude-based
  Pass 3. Produces _index.md with Domain Catalog, Repo-to-Domain Matrix, and
  Cross-Domain Dependencies deterministically from domain_list and repo_list.

Output:
- Domain-clustered markdown files with YAML frontmatter
- Written to cidx-meta/dependency-map/ directory
"""

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from code_indexer.global_repos.yaml_emitter_utils import yaml_quote_if_unsafe

# Story #848: lazy server imports for Pass 2 CliDispatcher wiring.
# These are None in pure CLI contexts (no server package) and populated when
# the server package is available.  Importing at module level (not inside the
# method) makes them patchable by unit tests via
#   patch("code_indexer.global_repos.dependency_map_analyzer.get_config_service")
try:
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.services.cli_dispatcher import CliDispatcher
    from code_indexer.server.services.claude_invoker import ClaudeInvoker
    from code_indexer.server.services.codex_invoker import CodexInvoker
    from code_indexer.server.services.codex_mcp_auth_header_provider import build_codex_mcp_auth_header_provider
except ImportError:  # pragma: no cover — server package absent in pure CLI context
    # Caller guards against None before any of these are used; CLI paths never reach
    # the Codex/server integration code below.
    get_config_service = None  # type: ignore[assignment]
    CliDispatcher = None  # type: ignore[assignment]
    ClaudeInvoker = None  # type: ignore[assignment]
    CodexInvoker = None  # type: ignore[assignment]
    build_codex_mcp_auth_header_provider = None  # type: ignore[assignment]  # Callable[[], str] | None; CLI paths never call this

logger = logging.getLogger(__name__)


# Bug #838: journal hook constants.
# Maximum characters for the tool-input preview appended to **claude-tool** entries.
_JOURNAL_HOOK_PREVIEW_LEN: int = 120
# How often (in tool calls) to emit a STATUS NUDGE prompting Claude to self-report.
_JOURNAL_HOOK_NUDGE_INTERVAL: int = 10

# Bug #840: threshold for pass-1 description staging.
# When total description bytes across all repos exceed this value, descriptions
# are written to a temp JSON file and Claude is instructed to Read it — rather
# than embedding all content inline in the prompt.
PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES = 8192

# Bug #849: sentinel returned by invoke_delta_merge_file when Claude signals
# FILE_UNCHANGED — meaning no edits were needed.  Distinct from None (invocation
# failure) so callers can suppress retries for intentional no-ops.
_DELTA_NOOP: str = "__DELTA_NOOP__"

# Structured Cross-Domain schema text used in all 4 prompt variants (AC1-AC5)
_CROSS_DOMAIN_SCHEMA = """\
## Cross-Domain Connections

List ONLY verified dependencies between this domain's repos and repos in other domains.
Each dependency MUST have concrete source code evidence.

### Outgoing Dependencies

If repos in THIS domain depend on repos in OTHER domains, list them in this table:

| This Repo | Depends On | Target Domain | Type | Why | Evidence |
|---|---|---|---|---|---|

If none: leave the table empty (header only).

### Incoming Dependencies

If repos in OTHER domains depend on repos in THIS domain, list them in this table:

| External Repo | Depends On | Source Domain | Type | Why | Evidence |
|---|---|---|---|---|---|

If none: leave the table empty (header only).

If NO cross-domain dependencies exist in either direction, write exactly: "No verified cross-domain dependencies."

Allowed dependency types (use exactly one per row):
- Code-level
- Data contracts
- Service integration
- External tool
- Configuration coupling
- Deployment dependency
"""


# Module-level state for the verification semaphore singleton (Story #724 AC5).
# Protected by _VERIFICATION_SEMAPHORE_LOCK for thread safety.
_VERIFICATION_SEMAPHORE_LOCK: threading.Lock = threading.Lock()
_VERIFICATION_SEMAPHORE_STATE: Dict[
    str, Any
] = {}  # keys: 'semaphore', 'max_concurrent'

# Default semaphore capacity when the config service is unavailable (CLI-only contexts
# outside the server).  Must equal the schema default for ClaudeIntegrationConfig so
# cold-start sequences are consistent between server and non-server invocations.
_DEFAULT_MAX_CONCURRENT_CLAUDE_CLI: int = 2


def _get_verification_semaphore(max_concurrent: int) -> threading.Semaphore:
    """Return process-wide singleton semaphore gating verification invocations.

    Story #724 AC5: The first call establishes the semaphore capacity. All
    subsequent calls MUST pass the same value; passing a different value raises
    ValueError to fail loudly rather than silently creating a second semaphore.

    Runtime changes to max_concurrent_claude_cli via the Web UI require a
    server restart to take effect (documented limitation in AC5 Technical Notes).

    Args:
        max_concurrent: Maximum number of concurrent verification invocations.
                        Must be a positive integer (>= 1).

    Returns:
        The singleton threading.Semaphore for this process.

    Raises:
        ValueError: If max_concurrent is not a positive integer.
        ValueError: If called with a different value than the first call.
    """
    if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool):
        raise ValueError(
            f"max_concurrent must be a positive integer, got {type(max_concurrent).__name__!r}"
        )
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")

    with _VERIFICATION_SEMAPHORE_LOCK:
        if "semaphore" not in _VERIFICATION_SEMAPHORE_STATE:
            _VERIFICATION_SEMAPHORE_STATE["semaphore"] = threading.Semaphore(
                max_concurrent
            )
            _VERIFICATION_SEMAPHORE_STATE["max_concurrent"] = max_concurrent
        elif _VERIFICATION_SEMAPHORE_STATE["max_concurrent"] != max_concurrent:
            raise ValueError(
                f"Semaphore already initialized with max_concurrent="
                f"{_VERIFICATION_SEMAPHORE_STATE['max_concurrent']}; "
                f"cannot reinitialize with max_concurrent={max_concurrent}. "
                f"A server restart is required to change this value."
            )
        # cast is safe: the only write path sets this key to threading.Semaphore(n);
        # mypy cannot infer the concrete type from Dict[str, Any].
        return cast(threading.Semaphore, _VERIFICATION_SEMAPHORE_STATE["semaphore"])


_FRONTMATTER_DELIMITER = "---\n"


def _strip_leading_yaml_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block from markdown content.

    A frontmatter block is a ``---\\n...\\n---\\n`` section at the very start of
    the string.  An optional blank line immediately after the closing delimiter
    is also consumed so the returned body does not start with a spurious newline.

    Only a SINGLE leading block is stripped.  If the content does not start
    with ``---\\n``, or if there is no closing ``---\\n``, the content is
    returned unchanged (no partial strip, no infinite loop).

    CRLF line endings (``\\r\\n``) are NOT supported.  Content with CRLF does
    not start with ``---\\n`` so it is returned unchanged — this is the
    documented safe behaviour.

    Args:
        content: Markdown string, possibly with a leading YAML frontmatter block.

    Returns:
        The body portion after the closing ``---`` delimiter (and optional blank
        line), or *content* unchanged when no valid leading frontmatter is found.
    """
    if not content.startswith(_FRONTMATTER_DELIMITER):
        return content
    # Search for closing delimiter.  The pattern we look for is "\n---\n".
    # The earliest it can appear is at index len(_FRONTMATTER_DELIMITER) - 1
    # (i.e. index 3), which covers the degenerate case "---\n---\nbody" where
    # the frontmatter body is empty.
    _CLOSING_PATTERN = "\n" + _FRONTMATTER_DELIMITER
    _SEARCH_START = len(_FRONTMATTER_DELIMITER) - 1
    close_idx = content.find(_CLOSING_PATTERN, _SEARCH_START)
    if close_idx == -1:
        # No closing delimiter — return unchanged.
        return content
    # Position just after the closing "\n---\n"
    body_start = close_idx + len(_CLOSING_PATTERN)
    # Consume one optional blank line immediately after the closing delimiter.
    if content[body_start : body_start + 1] == "\n":
        body_start += 1
    return content[body_start:]


class VerificationFailed(RuntimeError):
    """Raised by invoke_verification_pass when both retry attempts fail (Story #724 v2).

    The exception message includes the temp file path for debugging.
    """


class DependencyMapAnalyzer:
    """
    Analyzes dependencies across golden repositories using Claude CLI.

    Uses multi-pass pipeline:
    1. Domain synthesis from repo descriptions
    2. Per-domain source code analysis
    3. Index generation with catalog and matrix
    """

    def __init__(
        self,
        golden_repos_root: Path,
        cidx_meta_path: Path,
        pass_timeout: int,
        mcp_registration_service=None,
        analysis_model: str = "opus",
        cli_dispatcher=None,
    ):
        """
        Initialize dependency map analyzer.

        Args:
            golden_repos_root: Root directory containing all golden repo clones
            cidx_meta_path: Path to cidx-meta directory for output
            pass_timeout: Timeout in seconds for each pass (Pass 2 uses full, others use half)
            mcp_registration_service: MCPSelfRegistrationService for auto-registering CIDX as MCP server
            analysis_model: Claude model to use ("opus" or "sonnet", default: "opus")
            cli_dispatcher: Optional CliDispatcher instance (Story #848).
                When provided, Pass 2 routes through this dispatcher instead of
                building one from config. Primarily used to inject a mock dispatcher
                for deterministic behaviour in tests.
        """
        self.golden_repos_root = Path(golden_repos_root)
        self.cidx_meta_path = Path(cidx_meta_path)
        self.pass_timeout = pass_timeout
        self._mcp_registration_service = mcp_registration_service
        self.analysis_model = analysis_model
        self._cli_dispatcher = cli_dispatcher
        # HIGH #3 (Story #848): lazy cache for the built dispatcher so
        # _invoke_pass2_dispatcher constructs CliDispatcher only once per
        # analyzer instance lifetime instead of on every Pass 2 call.
        self._cached_pass2_dispatcher = None

    def generate_claude_md(self, repo_list: List[Dict[str, Any]]) -> None:
        """
        Generate CLAUDE.md orientation file in golden-repos root (AC2).

        Creates orientation context for Claude CLI listing all repositories
        and the dependency analysis task.

        Args:
            repo_list: List of repository metadata dicts with 'alias' and 'description_summary'
        """
        content = "# CIDX Dependency Map Analysis\n\n"
        content += "## Available Repositories\n\n"

        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            summary = repo.get("description_summary", "No description")
            clone_path = repo.get("clone_path", "unknown")
            content += f"- **{alias}**: {summary}\n"
            content += f"  - Path: `{clone_path}`\n"

        content += "\n## Tools Available\n\n"
        content += "You MUST use the `cidx-local` MCP server's `search_code` tool for semantic code search.\n"
        content += "Search for repo names, class names, and API endpoints across all repositories to discover integration patterns.\n\n"

        content += "## Task\n\n"
        content += (
            "Analyze cross-repository dependencies at the domain and subdomain level.\n"
        )
        content += "Focus on identifying:\n"
        content += "- Domain clusters that span multiple repositories\n"
        content += "- Code-level dependencies (imports, shared libraries, type reuse)\n"
        content += "- Data contract dependencies (shared database tables/views/schemas, file formats)\n"
        content += "- Service integration dependencies (REST/HTTP/MCP/gRPC API calls)\n"
        content += (
            "- External tool invocation dependencies (CLI tools, subprocess calls)\n"
        )
        content += (
            "- Configuration coupling (shared env vars, config keys, feature flags)\n"
        )
        content += "- Message/event contract dependencies (queues, webhooks, pub/sub)\n"
        content += "- Deployment dependencies (runtime availability requirements)\n"
        content += "- Semantic coupling (behavioral contracts without code imports)\n"

        claude_md_path = self.golden_repos_root / "CLAUDE.md"
        claude_md_path.write_text(content)
        logger.info(f"Generated CLAUDE.md orientation file at {claude_md_path}")

    def run_pass_1_synthesis(
        self,
        staging_dir: Path,
        repo_descriptions: Dict[str, str],
        repo_list: List[Dict[str, Any]],
        max_turns: int,
    ) -> List[Dict[str, Any]]:
        """
        Run Pass 1: Domain synthesis from repository descriptions (AC1).

        Analyzes cidx-meta repository descriptions to identify domain clusters.

        Uses file-based output: Claude writes the JSON to a staging file,
        validates it with python3 -m json.tool, and self-corrects errors.
        The analyzer reads the file after Claude CLI returns. Falls back to
        stdout extraction if the file is not written.

        Args:
            staging_dir: Staging directory for output files
            repo_descriptions: Dict mapping repo alias to description content
            repo_list: List of repository metadata dicts with alias and clone_path
            max_turns: Maximum Claude CLI turns for this pass

        Returns:
            List of domain dicts with 'name', 'description', 'participating_repos'
        """
        # Paths for file-based output (Story #349)
        pass1_file = staging_dir / "pass1_domains.json"
        # Relative path from Claude CLI cwd (golden_repos_root)
        # staging_dir is cidx-meta/dependency-map.staging/ inside golden_repos_root
        try:
            pass1_file_rel = pass1_file.relative_to(self.golden_repos_root)
        except ValueError:
            # If staging_dir is not under golden_repos_root (e.g. in tests), use absolute
            pass1_file_rel = pass1_file
        pass1_file_abs = str(pass1_file)
        staging_dir_abs = str(staging_dir)

        # Build synthesis prompt — output format + file instructions FIRST (primacy/recency)
        prompt = "# Domain Synthesis Task\n\n"
        prompt += "You are running in the golden-repos root directory with filesystem access to all repositories.\n\n"

        # ── OUTPUT FORMAT AND FILE INSTRUCTIONS (at TOP — before repo descriptions) ──
        prompt += "## CRITICAL: Output Format and File Instructions\n\n"
        prompt += "You MUST write your output as a JSON file. If you cannot write files due to permission errors, output ONLY the raw JSON array to stdout as fallback (no commentary, no explanation — just the JSON).\n\n"
        prompt += "### JSON Schema\n\n"
        prompt += "Write a JSON array where each element is a domain object with these exact fields:\n\n"
        prompt += "[\n"
        prompt += '  {"name": "domain-name", "description": "1-sentence domain scope", '
        prompt += '"participating_repos": ["alias1", "alias2"], '
        prompt += '"repo_paths": {"alias1": "/full/path/to/alias1", "alias2": "/full/path/to/alias2"}, '
        prompt += '"evidence": "Brief justification referencing actual files/patterns observed"}\n'
        prompt += "]\n\n"
        prompt += "### File Output Instructions\n\n"
        prompt += (
            "**STEP 0 — MANDATORY CANARY TEST (do this FIRST, before any analysis):**\n"
        )
        prompt += "Before doing ANY analysis work, test that you can write files by running:\n"
        prompt += "```\n"
        prompt += f"echo 'canary' > {pass1_file_abs}.canary && rm {pass1_file_abs}.canary && echo 'CANARY_OK'\n"
        prompt += "```\n"
        prompt += "- If you see `CANARY_OK`: proceed with analysis and file writing normally.\n"
        prompt += "- If the write FAILS for ANY reason: STOP IMMEDIATELY. Do NOT attempt analysis. "
        prompt += "Output ONLY this exact line to stdout:\n"
        prompt += f"  `CANARY_FAIL: Cannot write to {staging_dir_abs} — [reason: OS permission denied | Claude permission denied | other]`\n"
        prompt += "  Then exit. Do NOT retry with other write methods. Do NOT proceed with analysis.\n\n"
        prompt += "**STEP 1** — Write the JSON array to this file path:\n"
        prompt += f"   - Relative from your cwd: `./{pass1_file_rel}`\n"
        prompt += f"   - Absolute path: `{pass1_file_abs}`\n\n"
        prompt += "**STEP 2** — Validate the file with:\n"
        prompt += "   ```\n"
        prompt += f"   python3 -m json.tool {pass1_file_abs}\n"
        prompt += "   ```\n\n"
        prompt += "**STEP 3** — If validation fails, fix the JSON errors and re-validate until it passes.\n\n"
        prompt += "**FALLBACK**: If file writing worked in the canary test but fails later during the actual write, output ONLY the raw JSON array to stdout (no explanation, no commentary).\n\n"

        # ── REPOSITORY DESCRIPTIONS (after file instructions) ──
        prompt += "## Repository Descriptions\n\n"
        for alias, content in repo_descriptions.items():
            prompt += f"### {alias}\n\n"
            prompt += f"{content}\n\n"

        prompt += "## Repository Filesystem Locations\n\n"
        prompt += "Each repository is available on disk. Use these paths to explore source code:\n\n"
        # Build alias-to-path mapping from repo_list
        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            clone_path = repo.get("clone_path", "unknown")
            file_count = repo.get("file_count", "?")
            total_mb = round(repo.get("total_bytes", 0) / (1024 * 1024), 1)
            prompt += (
                f"- **{alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
            )
        prompt += "\n"

        prompt += self._build_domain_definition_section()

        prompt += "## Instructions\n\n"
        prompt += (
            "Identify domain clusters and list participating repos per domain.\n\n"
        )
        prompt += "**IMPORTANT**: The `cidx-meta` repository is the system metadata registry where dependency map output is stored. "
        prompt += (
            "It must NOT be included as a participating repository in any domain. "
        )
        prompt += "If you see references to cidx-meta in other repos, that is a system-level integration, not a domain dependency.\n\n"

        prompt += "### Source-Code-First Exploration (MANDATORY)\n\n"
        prompt += "ALWAYS examine source code, not just descriptions. Documentation may be incomplete or misleading. Source code is the ground truth.\n\n"
        prompt += "For each repository:\n"
        prompt += "1. Assess documentation depth relative to codebase size (file count, directory depth)\n"
        prompt += "2. If a repo description is short/generic but has many source files, the description is unreliable - explore source\n"
        prompt += "3. Look at imports, entry points, and config/manifest files (e.g., package.json, requirements.txt, setup.py, pyproject.toml, go.mod, Cargo.toml, pom.xml, build.gradle, *.csproj, CMakeLists.txt, Makefile, Dockerfile)\n"
        prompt += "4. Check build files, test patterns, directory structures to understand actual repo purpose\n"
        prompt += "5. Examine interesting modules to infer purpose and integration patterns\n\n"

        prompt += "### Evidence-Based Domain Clustering\n\n"
        prompt += "Cluster repositories by integration-level relationships, not just functional similarity.\n"
        prompt += "Consider: shared data sources, service-to-service calls, tool chains, deployment coupling.\n\n"
        prompt += "For each domain clustering decision, briefly justify WHY repos belong together based on what you observed in source code (not just description similarity).\n\n"

        prompt += "### Domain Clustering Standards\n\n"
        prompt += "Cluster repositories that have integration relationships. Evidence can include:\n"
        prompt += "- Direct: shared imports, API calls, configuration references, deployment scripts\n"
        prompt += "- Transitive: A depends on B depends on C (all three belong in same domain)\n"
        prompt += "- Semantic: A reads data that B produces, A calls services that B exposes\n"
        prompt += "- Ecosystem: A and B are tools in the same workflow (e.g., one generates data, another visualizes it)\n\n"
        prompt += "DO NOT cluster based solely on naming similarity without verifying in source code.\n"
        prompt += (
            "DO NOT cluster based on general knowledge without source code evidence.\n"
        )
        prompt += "But DO cluster when you find source-code-verified integration of ANY type listed above.\n\n"
        repo_count = len(repo_list)
        if repo_count <= 20:
            domain_guidance = "3-7"
        elif repo_count <= 50:
            domain_guidance = "5-15"
        elif repo_count <= 100:
            domain_guidance = "10-30"
        else:
            domain_guidance = "15-50"
        prompt += f"AIM for {domain_guidance} domains for a codebase of {repo_count} repositories. If you find very few domains,\n"
        prompt += "consider whether you may be applying too strict a threshold for integration evidence.\n\n"

        prompt += "### COMPLETENESS MANDATE\n\n"
        prompt += f"There are exactly {len(repo_list)} repositories. Your output MUST assign ALL {len(repo_list)} of them.\n"
        prompt += (
            "Verify INTERNALLY that total repos across all domains equals "
            + str(len(repo_list))
            + ". Do NOT output the verification.\n"
        )
        prompt += "If you cannot find integration evidence for a repo, assign it to its own standalone domain.\n"
        prompt += "MISSING REPOS = FAILED ANALYSIS. Every valid alias must appear exactly once.\n\n"

        prompt += "### Unassigned Repository Handling\n\n"
        prompt += "If a repository does not fit any domain (no integration evidence found after examining\n"
        prompt += "source code), assign it to a standalone domain as a LAST RESORT. Name the domain after\n"
        prompt += "its functional purpose as a technical area (e.g., 'code-search-tooling' rather than\n"
        prompt += "'code-indexer'), even if the repository alias sounds descriptive. A domain always\n"
        prompt += (
            "represents a functional area, even when it contains only one repository.\n"
        )
        prompt += "Do NOT leave repositories unassigned.\n\n"

        prompt += "### CRITICAL: Valid Repository Aliases\n\n"
        prompt += "ONLY the following repository aliases are valid. Do NOT invent or modify alias names.\n"
        prompt += "Every alias in your output MUST come from this exact list:\n\n"
        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            prompt += f"- `{alias}`\n"
        prompt += "\nAny domain containing repos not in this list will be rejected by validation.\n"
        prompt += f"\nCOMPLETENESS CHECK: Your output must contain exactly {len(repo_list)} repos total across all domains.\n\n"

        # Invoke Claude CLI (Pass 1 explores all repos to identify domains and outputs JSON)
        timeout = (
            self.pass_timeout
        )  # Pass 1 uses full timeout (heaviest phase: explores all repos)
        result = self._invoke_claude_cli(
            prompt,
            timeout,
            max_turns,
            allowed_tools=None,
            dangerously_skip_permissions=True,
        )

        # ── Canary failure fast-path ──
        if "CANARY_FAIL" in result:
            canary_msg = result.strip()
            # Extract just the CANARY_FAIL line
            for line in canary_msg.split("\n"):
                if "CANARY_FAIL" in line:
                    canary_msg = line.strip()
                    break
            raise RuntimeError(
                f"Pass 1 file-write canary test failed: {canary_msg}. "
                f"Claude CLI cannot write to {staging_dir_abs}. "
                f"Check --dangerously-skip-permissions flag and OS file permissions."
            )

        # ── File-based output: primary path (Story #349) ──
        logger.debug(f"Pass 1 raw output length: {len(result)} chars")
        domain_list = None

        if pass1_file.exists():
            file_content = pass1_file.read_text()
            file_size = len(file_content)
            try:
                domain_list = json.loads(file_content)
                logger.info(
                    f"Pass 1 output read from file: {pass1_file} "
                    f"({file_size} bytes, {len(domain_list)} domains)"
                )
            except json.JSONDecodeError as e:
                logger.warning(
                    f"Pass 1 output file exists but contains invalid JSON: {e}"
                )
            finally:
                pass1_file.unlink(missing_ok=True)  # cleanup even on parse failure
        else:
            logger.warning(
                f"Pass 1 output file not found at {pass1_file}, "
                "falling back to stdout extraction"
            )

        # ── Stdout fallback ──
        if domain_list is None:
            try:
                domain_list = self._extract_json(result)
                logger.info("Pass 1 output extracted from stdout (fallback)")
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"Pass 1 stdout extraction failed: {e}, will retry")

        # ── Retry if both paths failed ──
        if domain_list is None:
            logger.warning(
                "Pass 1 first attempt failed (no file, no parseable stdout). "
                "Retrying with file-write reminder."
            )
            # Delete stale file if it exists from prior attempt
            pass1_file.unlink(missing_ok=True)

            retry_prompt = (
                "CRITICAL: You MUST write your output to the file. "
                "If file writing is blocked by permissions, output ONLY the raw JSON array to stdout.\n"
                f"Write the JSON array to: {pass1_file_abs}\n"
                f"Then validate with: python3 -m json.tool {pass1_file_abs}\n"
                "Fix any errors and re-validate until the file contains valid JSON.\n\n"
                + prompt
            )
            result = self._invoke_claude_cli(
                retry_prompt,
                timeout,
                max_turns,
                allowed_tools=None,
                dangerously_skip_permissions=True,
            )

            if pass1_file.exists():
                file_content = pass1_file.read_text()
                try:
                    domain_list = json.loads(file_content)
                    logger.info(
                        f"Pass 1 retry output read from file: {pass1_file} "
                        f"({len(file_content)} bytes, {len(domain_list)} domains)"
                    )
                except json.JSONDecodeError as e2:
                    logger.error(f"Pass 1 retry file also invalid: {e2}")
                finally:
                    pass1_file.unlink(missing_ok=True)

            if domain_list is None:
                try:
                    domain_list = self._extract_json(result)
                except (json.JSONDecodeError, ValueError) as e2:
                    raise RuntimeError(
                        f"Pass 1 (Synthesis) failed after retry: "
                        f"file not written and stdout unparseable. "
                        f"File path checked: {pass1_file_abs}. "
                        f"Stdout preview: {result[:1000]!r}"
                    ) from e2

        # Validate Pass 1 output: filter hallucinated repos, catch unassigned repos
        valid_aliases = {r.get("alias") for r in repo_list}

        # Filter out hallucinated repo aliases and repos with wrong/missing paths
        for domain in domain_list:
            original_repos = domain.get("participating_repos", [])
            repo_paths = domain.get("repo_paths", {})
            filtered_repos = []
            for r in original_repos:
                if r not in valid_aliases:
                    logger.warning(
                        f"Pass 1 hallucinated repo '{r}' in domain "
                        f"'{domain.get('name')}' - not in valid alias list"
                    )
                    continue
                # Validate path if provided - check alias appears as delimited segment
                # (lenient: allows different directory structures like .versioned/)
                # Uses regex to avoid false positives (e.g., "db" matching "adobe")
                claimed_path = repo_paths.get(r)
                if claimed_path:
                    # Check alias appears as a delimited segment in path (not arbitrary substring)
                    # This handles paths like /repos/.versioned/flask-large/v_123/
                    pattern = r"(?:^|[/\\_.-])" + re.escape(r) + r"(?:$|[/\\_.-])"
                    if not re.search(pattern, claimed_path):
                        logger.warning(
                            f"Pass 1 repo '{r}' has suspicious path '{claimed_path}' "
                            f"(alias not found in path) in domain "
                            f"'{domain.get('name')}' - removed"
                        )
                        continue
                filtered_repos.append(r)
            removed = set(original_repos) - set(filtered_repos)
            if removed:
                logger.warning(
                    f"Pass 1 filtered repo(s) {removed} from domain "
                    f"'{domain.get('name')}'"
                )
            domain["participating_repos"] = filtered_repos
            # Clean up repo_paths from output (not needed downstream)
            domain.pop("repo_paths", None)

        # Remove domains that became empty after filtering
        domain_list = [d for d in domain_list if d.get("participating_repos")]

        # Auto-create standalone domains for unassigned repos
        assigned_repos = set()
        for domain in domain_list:
            assigned_repos.update(domain.get("participating_repos", []))

        # Check for duplicate repo assignments
        all_assigned = []
        for domain in domain_list:
            all_assigned.extend(domain.get("participating_repos", []))
        seen = set()
        for r in all_assigned:
            if r in seen:
                logger.warning(f"Pass 1 assigned repo '{r}' to multiple domains")
            seen.add(r)

        unassigned = valid_aliases - assigned_repos
        for alias in sorted(unassigned):  # type: ignore[assignment,type-var]
            # Find description from repo_list
            desc = "No description"
            for r in repo_list:
                if r.get("alias") == alias:
                    desc = r.get("description_summary", "No description")
                    break

            # Strip markdown heading markers from description
            desc = desc.lstrip("#").strip()

            # If description equals alias name or is empty, use better default
            if not desc or desc.lower() == alias.lower():
                desc = f"{alias} (standalone repository)"

            domain_list.append(
                {
                    "name": alias,
                    "description": desc,
                    "participating_repos": [alias],
                    "evidence": f"Auto-assigned: {alias} was not placed in any domain by Pass 1",
                }
            )
            logger.warning(
                f"Pass 1 did not assign repo '{alias}' - auto-creating standalone domain"
            )

        # Write domains.json to staging
        domains_file = staging_dir / "_domains.json"
        domains_file.write_text(json.dumps(domain_list, indent=2))
        logger.info(
            f"Pass 1 complete: identified {len(domain_list)} domains, wrote {domains_file}"
        )

        return domain_list  # type: ignore[no-any-return]

    def _build_output_first_prompt(
        self,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        previous_domain_dir: Optional[Path] = None,
        journal_path: Optional[Path] = None,
    ) -> str:
        """Build output-first prompt for large domains (>3 repos)."""
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])

        # Sort participating repos by size (largest first) using repo_list metadata
        repo_size_map = {r.get("alias"): r.get("total_bytes", 0) for r in repo_list}
        participating_repos_sorted = sorted(
            participating_repos, key=lambda a: repo_size_map.get(a, 0), reverse=True
        )

        prompt = f"# Domain Analysis: {domain_name}\n\n"
        prompt += "## CRITICAL INSTRUCTION: WRITE YOUR ANALYSIS FIRST\n\n"
        prompt += "You MUST write your complete domain analysis output BEFORE doing any MCP searches.\n"
        prompt += "Your primary source material is the Pass 1 evidence and repository descriptions below.\n"
        prompt += "MCP searches are OPTIONAL and limited to AT MOST 5 calls for verification only.\n\n"
        prompt += "**NOTE**: The `cidx-meta` directory is the system metadata registry, not a source code repository. Ignore it during analysis.\n\n"

        prompt += f"**Domain Description**: {domain.get('description', 'N/A')}\n\n"

        # Pass 1 evidence prominently
        evidence = domain.get("evidence", "")
        if evidence:
            prompt += f"## Pass 1 Evidence (PRIMARY SOURCE)\n\n{evidence}\n\n"

        # Domain structure
        prompt += "## Full Domain Structure\n\n"
        for d in domain_list:
            prompt += f"- **{d['name']}**: {d.get('description', 'N/A')}\n"
            prompt += f"  - Repos: {', '.join(d.get('participating_repos', []))}\n"

        prompt += "\n## Participating Repositories\n\n"
        path_map = {r.get("alias"): r.get("clone_path") for r in repo_list}
        repo_file_count_map = {
            r.get("alias"): r.get("file_count", "?") for r in repo_list
        }
        for repo_alias in participating_repos_sorted:
            clone_path = path_map.get(repo_alias, "path not found")
            file_count = repo_file_count_map.get(repo_alias, "?")
            total_mb = round(repo_size_map.get(repo_alias, 0) / (1024 * 1024), 1)
            prompt += f"- **{repo_alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
        prompt += "\n"

        # Inside-out analysis strategy (only if there are repos)
        if participating_repos_sorted:
            prompt += "## INSIDE-OUT ANALYSIS STRATEGY\n\n"
            prompt += f"Start your analysis from **{participating_repos_sorted[0]}** (largest repository, "
            prompt += (
                f"{repo_size_map.get(participating_repos_sorted[0], 0) // 1024} KB). "
            )
            prompt += "Map its integration points first, then fan out to discover how the smaller repositories connect to it.\n"
            prompt += (
                "This ensures the dominant codebase anchors the dependency graph.\n\n"
            )

        # Feed previous analysis if good quality - with explicit improvement mandate
        if previous_domain_dir and (previous_domain_dir / f"{domain_name}.md").exists():
            existing_content = (previous_domain_dir / f"{domain_name}.md").read_text()
            if (
                self._has_markdown_headings(existing_content)
                and len(existing_content.strip()) > 1000
            ):
                prev_path = previous_domain_dir / f"{domain_name}.md"
                prompt += "## Previous Analysis (EXTEND, IMPROVE, and CORRECT)\n\n"
                prompt += "A previous analysis exists for this domain. You MUST:\n"
                prompt += (
                    "1. **Preserve** accurate findings from the previous analysis\n"
                )
                prompt += (
                    "2. **Correct** any errors, inaccuracies, or outdated information\n"
                )
                prompt += "3. **Extend** with new dependencies or details not previously documented\n"
                prompt += "4. **Improve** clarity, evidence quality, and structural organization\n\n"
                prompt += (
                    "Do NOT start from scratch - build upon the previous work.\n\n"
                )
                prompt += (
                    f"Use the Read tool to load the previous analysis at `{prev_path}` "
                    "before writing your output.\n\n"
                )

        # Output template
        prompt += "## OUTPUT TEMPLATE (fill in each section)\n\n"
        prompt += "Your output MUST follow this exact structure:\n\n"
        prompt += "```\n"
        prompt += f"# Domain Analysis: {domain_name}\n\n"
        prompt += "## Overview\n"
        prompt += "[1-2 paragraphs describing domain scope, purpose, and how repos relate]\n\n"
        prompt += "## Repository Roles\n"
        prompt += "[For each repo: name, primary language, role within domain]\n\n"
        prompt += "## Intra-Domain Dependencies\n"
        prompt += "[Dependencies BETWEEN repos in this domain, with evidence]\n\n"
        prompt += _CROSS_DOMAIN_SCHEMA
        prompt += "```\n\n"

        # Dependency types (condensed)
        prompt += "## Dependency Types to Document\n\n"
        prompt += "- Code-level (imports, shared libraries)\n"
        prompt += "- Data contracts (shared schemas, file formats)\n"
        prompt += "- Service integration (REST/HTTP/MCP/gRPC API calls)\n"
        prompt += "- External tool invocation (CLI tools, subprocess calls)\n"
        prompt += "- Configuration coupling (shared env vars, config keys)\n"
        prompt += "- Deployment dependencies (runtime requirements)\n"
        prompt += "- Semantic coupling (behavioral contracts)\n\n"

        # Evidence requirements (condensed)
        prompt += "## Evidence Requirements\n\n"
        prompt += "Every dependency MUST include: source reference (module/subsystem), evidence type, reasoning.\n"
        prompt += (
            "If you cannot find concrete evidence, DO NOT include the dependency.\n\n"
        )

        # OPTIONAL verification searches at the end
        prompt += "## OPTIONAL: MCP Verification Searches (max 5 calls)\n\n"
        prompt += "After writing your analysis, you MAY use the `search_code` MCP tool for verification.\n"
        prompt += (
            "Limit: AT MOST 5 search_code calls total. Do NOT explore extensively.\n"
        )
        prompt += (
            "These searches are for CONFIRMING what you wrote, not for discovery.\n\n"
        )

        # Prohibited content
        prompt += "## PROHIBITED Content\n\n"
        prompt += "- YAML frontmatter blocks (system adds automatically)\n"
        prompt += "- Speculative sections (Recommendations, Future Considerations)\n"
        prompt += "- Meta-commentary about your process or thinking\n"
        prompt += "- Content not supported by evidence\n\n"

        prompt += "## Output Format\n\n"
        prompt += f"Your output MUST begin with: # Domain Analysis: {domain_name}\n"
        prompt += "Follow the template structure above exactly.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        if journal_path is not None:
            prompt += self._build_activity_journal_appendix(journal_path)

        return prompt

    def _build_activity_journal_appendix(self, journal_path: Path) -> str:
        """Build activity journal instruction appendix for Claude CLI prompts (Story #329)."""
        return f"""

## Activity Journal (MANDATORY)

You MUST log your progress to the activity journal file during this analysis.
Use the Bash tool to append entries:

    echo "[$(date +%H:%M:%S)] **claude** Your activity message here" >> {journal_path}

Required entries (log each as you perform the action):
- "Exploring repository: {{alias}}" -- when you start examining a repository
- "Reading file: {{relative_path}}" -- when you read a significant file
- "Searching code: '{{query}}'" -- when you use search_code MCP tool
- "Analyzing imports and dependencies in {{alias}}" -- when analyzing integration points
- "Writing domain analysis for {{domain}}" -- when you begin writing the output
- "Documenting cross-domain dependency: {{source}} -> {{target}}" -- when finding cross-domain links

Rules:
- One line per entry, present tense, minimal formatting (bold for emphasis only)
- Keep entries short (under 120 characters)
- Log at LEAST 3 entries during your analysis
- Do NOT read from or modify the journal file -- only append via echo
"""

    def _build_std_header(
        self,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        repos_sorted: List[str],
    ) -> str:
        """Build header section of the standard prompt."""
        domain_name = domain["name"]
        prompt = f"# Domain Analysis: {domain_name}\n\n"
        prompt += f"**Domain Description**: {domain.get('description', 'N/A')}\n\n"
        evidence = domain.get("evidence", "")
        if evidence:
            prompt += f"**Pass 1 Evidence (verify or refute)**: {evidence}\n\n"
        prompt += "## Full Domain Structure (for cross-domain awareness)\n\n"
        for d in domain_list:
            prompt += f"- **{d['name']}**: {d.get('description', 'N/A')}\n"
            prompt += f"  - Repos: {', '.join(d.get('participating_repos', []))}\n"
        prompt += f"\n## Focus Analysis on Domain: {domain_name}\n\n"
        prompt += f"Analyze dependencies for: {', '.join(repos_sorted)}\n\n"
        return prompt

    def _build_std_analysis_strategy(
        self, repos_sorted: List[str], repo_size_map: Dict[str, int]
    ) -> str:
        """Build inside-out analysis strategy section."""
        if not repos_sorted:
            return ""
        prompt = "## INSIDE-OUT ANALYSIS STRATEGY\n\n"
        prompt += (
            f"Start your analysis from **{repos_sorted[0]}** (largest repository, "
        )
        prompt += f"{repo_size_map.get(repos_sorted[0], 0) // 1024} KB). "
        prompt += "Map its integration points first, then fan out to discover how the smaller repositories connect to it.\n"
        prompt += "This ensures the dominant codebase anchors the dependency graph.\n\n"
        return prompt

    def _build_std_repo_locations(
        self,
        repos_sorted: List[str],
        repo_list: List[Dict[str, Any]],
        repo_size_map: Dict[str, int],
    ) -> str:
        """Build repository filesystem locations section."""
        prompt = "## Repository Filesystem Locations\n\n"
        prompt += "IMPORTANT: Each repository is a directory on disk. You MUST explore source code using these paths.\n"
        prompt += "Start by listing each repo's directory structure, then read key files (entry points, configs, manifests).\n\n"
        path_map = {r.get("alias"): r.get("clone_path") for r in repo_list}
        repo_file_count_map = {
            r.get("alias"): r.get("file_count", "?") for r in repo_list
        }
        for repo_alias in repos_sorted:
            clone_path = path_map.get(repo_alias, "path not found")
            file_count = repo_file_count_map.get(repo_alias, "?")
            total_mb = round(repo_size_map.get(repo_alias, 0) / (1024 * 1024), 1)
            prompt += f"- **{repo_alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
        prompt += "\n"
        return prompt

    def _build_std_mcp_search(
        self,
        repos_sorted: List[str],
        participating_repos: List[str],
        repo_list: List[Dict[str, Any]],
    ) -> str:
        """Build CIDX MCP search instructions section."""
        prompt = "## CIDX Semantic Search (MCP Tools) - MANDATORY\n\n"
        prompt += "You MUST use the `cidx-local` MCP server's `search_code` tool during this analysis.\n"
        prompt += (
            "It provides semantic search across ALL indexed golden repositories.\n\n"
        )
        prompt += "### Required Searches\n\n"
        prompt += "For EACH participating repository, run at least one search:\n"
        for repo_alias in repos_sorted:
            prompt += f"- Search for `{repo_alias}` references across all repos\n"
        prompt += "\n"
        prompt += "### How to Use\n\n"
        prompt += "Call the `search_code` tool with:\n"
        prompt += "- `query_text`: The search term (repo name, class name, API endpoint, etc.) — use the BARE alias here (e.g. `humanize`)\n"
        prompt += "- `repository_alias` (OPTIONAL scope): when scoping a search to a specific repo, you MUST use the `-global`-suffixed form (e.g. `humanize-global`, NOT `humanize`). Bug #898: bare aliases are rejected by the server with `Repository not found in global repositories` errors. To search all repos, omit this parameter entirely or use the wildcard `*-global`.\n"
        prompt += "- `limit`: Number of results (start with 10)\n\n"
        prompt += "This reveals cross-repo references that filesystem exploration alone cannot find.\n"
        prompt += "Do NOT skip MCP searches - they are essential for discovering service integration and semantic coupling.\n\n"
        prompt += "### Minimum Search Requirements\n\n"
        prompt += "You MUST call the `search_code` MCP tool AT LEAST 3 times during this analysis.\n"
        prompt += "Failure to use MCP search invalidates the analysis. Recommended searches:\n"
        prompt += "1. Each participating repo name (to find cross-repo references)\n"
        prompt += "2. Key class/function/module names discovered during source code exploration\n"
        prompt += "3. Shared identifiers, API endpoints, or configuration keys\n\n"
        prompt += "### All Repository Aliases (for cross-domain reference searches)\n\n"
        prompt += "These are ALL repos in the ecosystem. Search for these names to find cross-domain connections:\n\n"
        all_aliases = sorted(r.get("alias", "unknown") for r in repo_list)
        for alias in all_aliases:
            if alias not in participating_repos:
                prompt += f"- `{alias}` (other domain)\n"
        prompt += "\n"
        return prompt

    def _build_std_exploration_and_evidence(self) -> str:
        """Build source code exploration mandate, dependency types, and evidence sections."""
        prompt = "## Source Code Exploration Mandate\n\n"
        prompt += (
            "DO NOT rely solely on README files or documentation. Actively explore:\n"
        )
        prompt += "- Import statements and package dependencies (requirements.txt, package.json, setup.py, go.mod)\n"
        prompt += "- Entry points (main.py, app.py, index.ts, cmd/ directories)\n"
        prompt += "- Configuration files for references to other repos/services\n"
        prompt += "- API endpoint definitions and client code\n"
        prompt += "- Test files (often reveal integration dependencies)\n"
        prompt += "- Build and deployment scripts\n\n"
        prompt += (
            "Assess each repo's documentation depth relative to its codebase size.\n"
        )
        prompt += "A repo with 100+ source files and a 5-line README has unreliable documentation - explore its source code thoroughly.\n\n"
        prompt += "**NOTE**: The `cidx-meta` directory in the golden-repos root is the system metadata registry. "
        prompt += "It stores dependency map output and repo descriptions. Ignore it during analysis — it is not a source code repository.\n\n"
        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += (
            "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        )
        prompt += (
            "  Example: 'web-app imports shared-types package for User interface'\n\n"
        )
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += "  Example: 'lambda-processor reads customer_summary_view exposed by core-db'\n\n"
        prompt += (
            "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        )
        prompt += "  Example: 'frontend calls backend /api/auth endpoint for login'\n\n"
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "  Example: 'deployment-scripts invoke cidx CLI for indexing'\n\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "  Example: 'worker-service and api-service both read REDIS_URL from env'\n\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "  Example: 'order-service publishes order.created event consumed by notification-service'\n\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += (
            "  Example: 'web-app requires auth-service to be running and reachable'\n\n"
        )
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n"
        prompt += "  Example: 'analytics-pipeline expects user-service to always include email field in user records'\n\n"
        return prompt

    def _build_std_verification_mandates(self) -> str:
        """Build fact-check, tech stack verification, and evidence-based claims sections."""
        prompt = "## MANDATORY: Fact-Check Pass 1 Domain Assignments\n\n"
        prompt += "Before analyzing dependencies, verify that each repository listed in this domain actually belongs here.\n"
        prompt += "For each participating repo:\n"
        prompt += "1. Examine its source code, imports, and integration points\n"
        prompt += "2. Confirm it has actual code-level or integration relationships with other repos in this domain\n"
        prompt += "3. If a repo does NOT belong in this domain based on source code evidence, state this explicitly\n\n"
        prompt += "## MANDATORY: Technology Stack Verification\n\n"
        prompt += (
            "When describing a repository's technology stack or primary language:\n"
        )
        prompt += "1. Search for dependency manifests (requirements.txt, package.json, Cargo.toml, go.mod, *.csproj, pom.xml, pyproject.toml)\n"
        prompt += "2. Check actual source file extensions in the repository (.py, .ts, .js, .rs, .go, .cs, .java, .pas)\n"
        prompt += "3. Do NOT assume technology based on tool names, library names, or general knowledge\n"
        prompt += "4. If a repo uses a library written in language X as a binding/wrapper in language Y, the repo's primary language is Y, not X\n"
        prompt += (
            "5. State only what the dependency manifest and source files confirm\n\n"
        )
        prompt += "## MANDATORY: Evidence-Based Claims\n\n"
        prompt += "Every dependency you document MUST include:\n"
        prompt += '1. **Source reference**: The specific module, package, or subsystem where the dependency manifests (e.g., "code-indexer\'s server/mcp/handlers.py module")\n'
        prompt += "2. **Evidence type**: What you observed (import statement, API endpoint definition, configuration key, subprocess invocation, etc.)\n"
        prompt += "3. **Reasoning**: Why this constitutes a dependency and what would break if the depended-on component changed\n\n"
        prompt += "DO NOT document dependencies based on:\n"
        prompt += '- Assumptions about what "should" exist\n'
        prompt += "- Naming similarity between repos\n"
        prompt += "- General knowledge about how similar systems typically work\n"
        prompt += "- Documentation claims you cannot verify in source code\n\n"
        prompt += "If you cannot find concrete evidence of a dependency in actual source files, DO NOT include it.\n\n"
        prompt += "### External Dependency Verification\n\n"
        prompt += "For external/third-party dependencies, you MUST read the actual manifest file:\n"
        prompt += "- Python: requirements.txt, setup.py, pyproject.toml\n"
        prompt += "- JavaScript/TypeScript: package.json\n"
        prompt += "- .NET/C#: *.csproj, *.sln, packages.config\n"
        prompt += "- Go: go.mod\n"
        prompt += "- Java: pom.xml, build.gradle\n"
        prompt += "- Rust: Cargo.toml\n\n"
        prompt += "DO NOT list external dependencies from memory or general knowledge of similar systems.\n"
        prompt += "If you cannot find the dependency manifest file, state 'dependency manifest not found' rather than guessing.\n\n"
        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"
        prompt += "**CORRECT**: 'auth-service JWT subsystem provides token validation consumed by web-app middleware layer'\n\n"
        prompt += "**INCORRECT (too granular)**: 'auth-service/src/jwt/validator.py:validate_token() called by web-app/src/middleware/auth.py'\n\n"
        prompt += "**INCORRECT (too abstract)**: 'auth-service is used by web-app'\n\n"
        return prompt

    def _build_std_output_section(self, domain_name: str) -> str:
        """Build content guidelines, output template, prohibited content, and output format."""
        prompt = "## Content Guidelines\n\n"
        prompt += "Write CONCISE analysis focused on inter-repository navigation. Your audience is an MCP user deciding which repos to explore.\n"
        prompt += "- Document precise dependency connections (who calls who, shared data, integration points)\n"
        prompt += "- Include specific evidence (file names, function names, config keys) but NOT full code snippets\n"
        prompt += (
            "- Keep each section to 3-8 sentences. Shorter is better if precise.\n"
        )
        prompt += (
            "- Do NOT reproduce source code, JSON schemas, or directory listings\n\n"
        )
        prompt += "## Output Budget\n\n"
        prompt += "Your analysis MUST be between 3,000 and 10,000 characters.\n"
        prompt += (
            "If you find yourself writing more, you are including too much detail.\n"
        )
        prompt += "Focus on WHAT connects repos, not HOW the internals work.\n\n"
        prompt += "## OUTPUT TEMPLATE (fill in each section)\n\n"
        prompt += "Your output MUST follow this exact structure:\n\n"
        prompt += f"# Domain Analysis: {domain_name}\n\n"
        prompt += "## Overview\n"
        prompt += "[1-2 paragraphs: domain scope, purpose, how repos relate]\n\n"
        prompt += "## Repository Roles\n"
        prompt += "[Table: repo | language | role within domain]\n\n"
        prompt += "## Intra-Domain Dependencies\n"
        prompt += "[Numbered list of dependencies BETWEEN repos, with evidence]\n\n"
        prompt += _CROSS_DOMAIN_SCHEMA
        prompt += "\n"
        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
        prompt += (
            "- Advisory content about what SHOULD be done or COULD be integrated\n"
        )
        prompt += "- 'MCP Searches Performed' or search audit trail sections\n"
        prompt += "- Code snippets or source code blocks\n"
        prompt += "- JSON schema definitions or field-by-field breakdowns\n"
        prompt += "- Directory listings or file tree dumps\n"
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"
        prompt += "## Output Format\n\n"
        prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
        prompt += "Do NOT start with summary text, meta-commentary, or a description of what you found.\n"
        prompt += "The VERY FIRST LINE of your output must be a markdown heading.\n\n"
        prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
        prompt += "Do NOT include any meta-commentary about your process, thinking, or search strategy.\n"
        prompt += "Do NOT generate YAML frontmatter (--- blocks). The system handles frontmatter automatically.\n"
        prompt += "Start your output directly with the analysis content (headings, sections, findings).\n\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"
        return prompt

    def _build_standard_prompt(
        self,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        previous_domain_dir: Optional[Path] = None,
        journal_path: Optional[Path] = None,
    ) -> str:
        """Build standard prompt for small domains (<=3 repos)."""
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])
        repo_size_map = {r.get("alias"): r.get("total_bytes", 0) for r in repo_list}
        repos_sorted = sorted(
            participating_repos, key=lambda a: repo_size_map.get(a, 0), reverse=True
        )

        prompt = self._build_std_header(domain, domain_list, repos_sorted)
        prompt += self._build_std_analysis_strategy(repos_sorted, repo_size_map)  # type: ignore[arg-type]
        prompt += self._build_std_repo_locations(repos_sorted, repo_list, repo_size_map)  # type: ignore[arg-type]
        prompt += self._build_std_mcp_search(
            repos_sorted, participating_repos, repo_list
        )
        prompt += self._build_std_exploration_and_evidence()
        prompt += self._build_std_verification_mandates()

        # Feed previous analysis if good quality
        if previous_domain_dir and (previous_domain_dir / f"{domain_name}.md").exists():
            existing_content = (previous_domain_dir / f"{domain_name}.md").read_text()
            if (
                self._has_markdown_headings(existing_content)
                and len(existing_content.strip()) > 1000
            ):
                prev_path = previous_domain_dir / f"{domain_name}.md"
                prompt += "## Previous Analysis (refine and improve)\n\n"
                prompt += (
                    f"A previous analysis exists at `{prev_path}`. "
                    "Use the Read tool to load that file, then refine and improve it "
                    "rather than starting from scratch.\n\n"
                )
            else:
                logger.info(
                    f"Skipping low-quality previous analysis for domain '{domain_name}' "
                    f"({len(existing_content)} chars, headings={self._has_markdown_headings(existing_content)})"
                )

        prompt += self._build_std_output_section(domain_name)

        if journal_path is not None:
            prompt += self._build_activity_journal_appendix(journal_path)

        return prompt

    # ========================================================================
    # Story #848: CliDispatcher wiring for Pass 2 (Domain Refinement)
    # ========================================================================

    def _build_pass2_dispatcher(self):
        """
        Build a CliDispatcher for Pass 2 from the current ServerConfig (Story #848).

        Constructs a ClaudeInvoker unconditionally.  When
        config.codex_integration_config.enabled is True and CODEX_HOME is set
        in os.environ, also constructs a CodexInvoker and wires it in with the
        weight from config.  Otherwise codex=None and the effective weight
        collapses to 0.0 inside CliDispatcher.

        Wires auth_header_provider for cidx-local MCP authentication via
        persistent Basic auth header from MCPCredentialManager (no expiration;
        same credentials Claude uses — no JWT TTL issue).

        Returns:
            A fully initialised CliDispatcher.
        """
        config = get_config_service().get_config()
        claude_invoker = ClaudeInvoker(analysis_model=self.analysis_model)
        codex_invoker = None
        codex_weight = 0.0
        codex_cfg = config.codex_integration_config if config else None
        if codex_cfg and codex_cfg.enabled:
            codex_home = os.environ.get("CODEX_HOME", "")
            if codex_home:
                codex_invoker = CodexInvoker(
                    codex_home=codex_home,
                    auth_header_provider=build_codex_mcp_auth_header_provider(),
                )
                codex_weight = codex_cfg.codex_weight
        return CliDispatcher(
            claude=claude_invoker,
            codex=codex_invoker,
            codex_weight=codex_weight,
        )

    def _invoke_pass2_dispatcher(self, prompt: str, timeout: int) -> str:
        """
        Invoke the CliDispatcher for Pass 2 domain refinement (Story #848).

        Uses the injected CliDispatcher when available; otherwise builds one
        from the current ServerConfig on each call.  Logs an INFO record when
        failover fired so operators can see which CLI handled the job.

        # PostToolUse hooks fire only for the Bash tool in Codex, not for MCP tools.
        # The turn-count escalation logic (--settings PostToolUse JSON) is therefore
        # not supported on the Codex path. This is a known degradation accepted by
        # the user. See: https://github.com/openai/codex/issues/16732

        Args:
            prompt: Full prompt text for Pass 2 domain analysis.
            timeout: Hard timeout seconds for the subprocess. Must be > 0.

        Returns:
            Output string from the CLI (result.output; empty string when dispatch fails).

        Raises:
            ValueError: If timeout is not a positive integer.
        """
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(
                f"_invoke_pass2_dispatcher: timeout must be a positive int, got {timeout!r}"
            )

        if self._cli_dispatcher is not None:
            dispatcher = self._cli_dispatcher
        else:
            if self._cached_pass2_dispatcher is None:
                self._cached_pass2_dispatcher = self._build_pass2_dispatcher()
            dispatcher = self._cached_pass2_dispatcher

        result = dispatcher.dispatch(
            flow="dependency_map_pass_2",
            cwd=str(self.golden_repos_root),
            prompt=prompt,
            timeout=timeout,
        )

        if result.was_failover:
            logger.info(
                "Pass 2 CLI failover fired: cli_used=%s was_failover=True",
                result.cli_used,
            )

        return result.output

    def run_pass_2_per_domain(
        self,
        staging_dir: Path,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        max_turns: int,
        previous_domain_dir: Optional[Path] = None,
        journal_path: Optional[Path] = None,
    ) -> None:
        """
        Run Pass 2: Per-domain source code analysis (AC1).

        Analyzes source code interface surfaces for repositories in the domain.

        Args:
            staging_dir: Staging directory for output files
            domain: Domain dict with 'name', 'description', 'participating_repos'
            domain_list: Full list of all domains (for cross-domain awareness)
            repo_list: List of repository metadata dicts with alias and clone_path
            max_turns: Maximum Claude CLI turns for this pass
            previous_domain_dir: Previous dependency-map dir for incremental improvement
            journal_path: Optional path to activity journal file (Story #329)
        """
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])
        is_large_domain = len(participating_repos) > 3

        # Build per-domain prompt
        if is_large_domain:
            # Use output-first prompt for large domains (>3 repos)
            prompt = self._build_output_first_prompt(
                domain,
                domain_list,
                repo_list,
                previous_domain_dir,
                journal_path=journal_path,
            )
        else:
            # Use standard prompt for small domains (<=3 repos)
            prompt = self._build_standard_prompt(
                domain,
                domain_list,
                repo_list,
                previous_domain_dir,
                journal_path=journal_path,
            )

        # Fix 1 (Iteration 12): PostToolUse hook to prevent turn exhaustion
        # _invoke_claude_cli() builds turn-aware bash script with escalating urgency messages
        # Iteration 14: Purpose-driven hook emphasizing conciseness and navigation assistance
        # Iteration 15: Add character budget to hook reminder
        hook_reminder = (
            "Remember: you are documenting precise, factual, short semantic dependencies "
            "to assist inter-repository navigation. TARGET: 3,000-10,000 chars. "
            "Be concise — no code snippets, no schema dumps, no full file listings. "
            "Your output MUST begin with # Domain Analysis heading."
        )

        # Story #715: File-based output — Claude writes body to temp file
        safe_name = self._sanitize_domain_name_for_path(domain_name)
        body_file = staging_dir / f"_pass2_body_{safe_name}.md"
        if body_file.exists():
            body_file.unlink()

        try:
            body_file_rel = body_file.relative_to(self.golden_repos_root)
        except ValueError:
            body_file_rel = body_file

        file_write_instructions = (
            f"\n\n## CRITICAL: File-Based Output\n\n"
            f"You MUST write your complete analysis to a file using the Write tool.\n"
            f"Target file: `{body_file}`\n"
            f"Relative from your cwd: `./{body_file_rel}`\n\n"
            f"Write ONLY the document body (starting with # Domain Analysis heading).\n"
            f"Do NOT include YAML frontmatter — the system adds it automatically.\n"
            f"After writing the file, print exactly: FILE_WRITE_COMPLETE\n\n"
            f"Do NOT output the full document to stdout — use the Write tool.\n"
        )

        prompt = prompt + file_write_instructions

        # Story #848: Route Pass 2 primary invocation through CliDispatcher so
        # Codex CLI can participate in domain refinement alongside Claude.
        # The dispatcher selects Claude or Codex based on codex_weight and
        # handles failover automatically.  Retry paths below remain Claude-only
        # because they use --settings PostToolUse hooks not supported by Codex.
        #
        # IMPORTANT: ALL retries (whether triggered by max-turn errors OR
        # insufficient output below _MIN_PASS2_OUTPUT_CHARS) execute on the
        # Claude path with full --settings PostToolUse hook support. This is
        # broader than AC5 of Story #848 (which only specifies Claude failover
        # on non-retryable errors). The Codex primary attempt is one-shot; if
        # it produces a short result, we retry on Claude.
        result = self._invoke_pass2_dispatcher(prompt, self.pass_timeout)

        # Story #715: Read from file if Claude wrote it, else fall back to stdout
        result = self._read_pass2_file_or_strip(body_file, result)

        # Fix 2 (Iteration 12): Detect max-turns exhaustion and retry with search budget guidance
        # Iteration 13: Large domains use write-only retry, small domains use budget search retry
        if re.search(r"Error:\s*Reached max turns\s*\(\d+\)", result.strip()):
            logger.warning(
                f"Pass 2 hit max-turns exhaustion for domain '{domain_name}', "
                f"retrying with {'write-only mode' if is_large_domain else 'search budget guidance'}"
            )
            if is_large_domain:
                # Large domain: retry with strict write-only mode (no MCP tools)
                budget_prompt = (
                    "CRITICAL: You MUST write your complete analysis NOW. "
                    "Do NOT use any search tools. Write based on your existing knowledge "
                    "and the Pass 1 evidence provided.\n\n"
                ) + prompt
                result = self._invoke_claude_cli(
                    budget_prompt,
                    self.pass_timeout,
                    8,
                    allowed_tools="",  # NO MCP tools
                    post_tool_hook=hook_reminder,
                    dangerously_skip_permissions=True,
                )
            else:
                # Small domain: existing retry logic (budget search)
                budget_prompt = (
                    "CRITICAL INSTRUCTION: You have a STRICT search budget. "
                    "Use AT MOST 3 search_code calls total. After your searches, "
                    "you MUST write your complete analysis output immediately.\n\n"
                ) + prompt
                result = self._invoke_claude_cli(
                    budget_prompt,
                    self.pass_timeout,
                    15,
                    allowed_tools="mcp__cidx-local__search_code",
                    post_tool_hook=hook_reminder,
                    dangerously_skip_permissions=True,
                )
            # Story #715: Read from file if Claude wrote it, else fall back to stdout
            result = self._read_pass2_file_or_strip(body_file, result)

        # Check for insufficient output and retry with reduced turns (Fix 1: raised threshold to 1000)
        # FIX 2 (Iteration 10): Also check for missing headings (pure meta-commentary)
        has_headings = self._has_markdown_headings(result)
        if len(result.strip()) < 1000 or not has_headings:
            reason = "no headings" if not has_headings else f"{len(result)} chars"
            logger.warning(
                f"Pass 2 returned insufficient output for domain '{domain_name}' "
                f"({reason}), retrying with reduced turns"
            )
            # Iteration 14: Retry with write-only mode (no MCP tools) and write-focused prompt
            # Primary attempt produced no headings, so force writing with existing knowledge
            retry_prompt = (
                "IMPORTANT: Your previous attempt did not produce properly formatted output. "
                "Write your dependency analysis NOW with NO searching. Use the Pass 1 evidence and "
                "any knowledge you have. Focus on precise inter-repo connections for navigation.\n\n"
            ) + prompt
            result = self._invoke_claude_cli(
                retry_prompt,
                self.pass_timeout,
                10,
                allowed_tools="",  # No MCP tools - write only
                post_tool_hook=hook_reminder,
                dangerously_skip_permissions=True,
            )
            # Story #715: Read from file if Claude wrote it, else fall back to stdout
            result = self._read_pass2_file_or_strip(body_file, result)

            # If retry also fails (insufficient), skip writing garbage file
            # FIX 3 (Iteration 11): When both attempts fail, skip this domain - don't write garbage
            # Writing 485 bytes of "please approve my write" is worse than no file at all
            has_headings = self._has_markdown_headings(result)
            if len(result.strip()) < 1000 or not has_headings:
                reason = "no headings" if not has_headings else f"{len(result)} chars"
                logger.error(
                    f"Pass 2 retry also returned insufficient output for domain '{domain_name}' "
                    f"({reason}) - SKIPPING domain file (will not write garbage content)"
                )
                self._cleanup_temp_file(body_file)
                return

        # Build YAML frontmatter (description included so Phase 3.5 can backfill JSON)
        description = domain.get("description", "")
        frontmatter = self._build_domain_frontmatter(
            domain_name=domain_name,
            description=description,
            participating_repos=participating_repos,
        )

        # Write domain file
        domain_file = staging_dir / f"{domain_name}.md"
        domain_file.write_text(frontmatter + result)
        logger.info(f"Pass 2 complete for domain '{domain_name}': wrote {domain_file}")

    def _build_domain_frontmatter(
        self,
        domain_name: str,
        description: str,
        participating_repos: List[str],
    ) -> str:
        """
        Build the YAML frontmatter block for a domain .md file (Fix 1, Bug #687).

        Includes the 'description' field so Phase 3.5 can backfill _domains.json
        by reading it from frontmatter rather than relying on JSON staying in sync.

        The description value is YAML-escaped via yaml.dump to handle colons,
        quotes, newlines, and other special characters safely.

        Returns frontmatter string ending with '---\\n\\n' (ready to prepend to body).
        """
        import yaml  # lazy import — yaml not needed at module level

        now = datetime.now(timezone.utc).isoformat()
        # yaml.dump produces a safe scalar (quoted if necessary); strip trailing newline
        desc_yaml = yaml.dump({"description": description}, default_flow_style=False)
        # desc_yaml is "description: <value>\n"; extract just the value part
        desc_line = desc_yaml.rstrip("\n")
        fm = "---\n"
        fm += f"domain: {domain_name}\n"
        fm += f"{desc_line}\n"
        fm += f"last_analyzed: {now}\n"
        fm += "participating_repos:\n"
        for repo in participating_repos:
            fm += f"  - {yaml_quote_if_unsafe(repo)}\n"
        fm += "---\n\n"
        return fm

    def run_pass_3_index(
        self,
        staging_dir: Path,
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        max_turns: int,
    ) -> None:
        """
        Run Pass 3: Index generation with catalog and matrix (AC1).

        Generates domain catalog table and repo-to-domain matrix.

        Args:
            staging_dir: Staging directory for output files
            domain_list: List of domain dicts
            repo_list: List of repository metadata dicts
            max_turns: Maximum Claude CLI turns for this pass
        """
        # Build index generation prompt
        prompt = "# Index Generation Task\n\n"
        prompt += "Read all domain files in the staging directory and generate:\n"
        prompt += "1. Domain Catalog table listing all identified domains\n"
        prompt += "2. Repo-to-Domain Matrix mapping repos to domains\n\n"

        prompt += (
            "## AUTHORITATIVE Domain Assignments (from Pass 1 - use these EXACTLY)\n\n"
        )
        for domain in domain_list:
            prompt += f"- **{domain['name']}**: {domain.get('description', 'N/A')}\n"
            participating = domain.get("participating_repos", [])
            if participating:
                prompt += f"  - Participating repos: {', '.join(participating)}\n"
            else:
                prompt += "  - Participating repos: (none)\n"

        prompt += "\n## Fact-Check Requirement\n\n"
        prompt += "The Repository-to-Domain Mapping Matrix MUST match the authoritative domain assignments above exactly.\n"
        prompt += "Do NOT reassign repositories to different domains.\n"
        prompt += "Do NOT omit any repository from the mapping.\n"
        prompt += "Every repo in the Repository List below must appear in the mapping matrix.\n\n"

        prompt += "## Repository List\n\n"
        for repo in repo_list:
            prompt += f"- {repo.get('alias', 'unknown')}: {repo.get('description_summary', 'N/A')}\n"

        prompt += "\n## Output Format\n\n"
        prompt += "Generate markdown with domain catalog and matrix tables.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        # Invoke Claude CLI (Pass 3 does not need MCP tools - just reads domain files and generates index)
        timeout = self.pass_timeout // 2  # Pass 3 uses half timeout (lighter workload)
        result = self._invoke_claude_cli(prompt, timeout, max_turns, allowed_tools=None)

        # Strip meta-commentary from output (Fix 3: same as Pass 2)
        result = self._strip_meta_commentary(result)

        # Build cross-domain graph (Iteration 16)
        graph_section = self._build_cross_domain_graph(staging_dir, domain_list)

        # Build YAML frontmatter
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = "---\n"
        frontmatter += "schema_version: 1.0\n"
        frontmatter += f"last_analyzed: {now}\n"
        frontmatter += f"repos_analyzed_count: {len(repo_list)}\n"
        frontmatter += f"domains_count: {len(domain_list)}\n"
        frontmatter += "repos_analyzed:\n"
        for repo in repo_list:
            alias = repo.get("alias", repo.get("name", "unknown"))
            frontmatter += f"  - {yaml_quote_if_unsafe(alias)}\n"
        frontmatter += "---\n\n"

        # Write index file (with graph section appended if edges exist)
        index_file = staging_dir / "_index.md"
        index_file.write_text(frontmatter + result + graph_section)
        logger.info(f"Pass 3 complete: wrote {index_file}")

    def _generate_index_md(
        self,
        staging_dir: Path,
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
    ) -> None:
        """
        Programmatically generate _index.md from domain_list and repo_list (AC2, Story #216).

        Replaces the Claude-based Pass 3 with a deterministic, fast implementation.
        Generates YAML frontmatter, Domain Catalog, Repo-to-Domain Matrix, and
        Cross-Domain Dependencies sections.

        Args:
            staging_dir: Staging directory where _index.md will be written
            domain_list: List of domain dicts with name, description, participating_repos
            repo_list: List of repo dicts with alias, description_summary
        """
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_index_frontmatter(now, repo_list, domain_list)
        body = self._build_index_body(staging_dir, domain_list, repo_list)
        index_file = staging_dir / "_index.md"
        index_file.write_text(frontmatter + body)
        logger.info(f"Generated _index.md programmatically at {index_file}")

    def _build_index_frontmatter(
        self,
        now: str,
        repo_list: List[Dict[str, Any]],
        domain_list: List[Dict[str, Any]],
    ) -> str:
        """Build YAML frontmatter block for _index.md."""
        fm = "---\n"
        fm += "schema_version: 1.0\n"
        fm += f"last_analyzed: {now}\n"
        fm += f"repos_analyzed_count: {len(repo_list)}\n"
        fm += f"domains_count: {len(domain_list)}\n"
        fm += "repos_analyzed:\n"
        for repo in repo_list:
            alias = repo.get("alias", repo.get("name", "unknown"))
            fm += f"  - {yaml_quote_if_unsafe(alias)}\n"
        fm += "---\n\n"
        return fm

    def _build_index_body(
        self,
        staging_dir: Path,
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
    ) -> str:
        """Build the markdown body for _index.md (catalog, matrix, cross-domain deps)."""
        content = "# Dependency Map Index\n\n"

        # Domain Catalog
        content += "## Domain Catalog\n\n"
        content += "| Domain | Description | Repo Count |\n"
        content += "|---|---|---|\n"
        for domain in sorted(domain_list, key=lambda d: d.get("name", "")):
            name = domain.get("name", "")
            desc = domain.get("description", "")
            repo_count = len(domain.get("participating_repos", []))
            content += f"| {name} | {desc} | {repo_count} |\n"

        # Repo-to-Domain Matrix
        content += "\n## Repo-to-Domain Matrix\n\n"
        content += "| Repository | Domain |\n"
        content += "|---|---|\n"
        repo_domain_map: Dict[str, str] = {}
        for domain in domain_list:
            for alias in domain.get("participating_repos", []):
                repo_domain_map[alias] = domain.get("name", "")
        for repo in sorted(repo_list, key=lambda r: r.get("alias", "")):
            alias = repo.get("alias", "")
            domain_name = repo_domain_map.get(alias, "(unassigned)")
            content += f"| {alias} | {domain_name} |\n"

        # Cross-Domain Dependencies
        graph_section = self._build_cross_domain_graph(staging_dir, domain_list)
        if graph_section:
            graph_section = graph_section.replace(
                "## Cross-Domain Dependency Graph",
                "## Cross-Domain Dependencies",
            )
            content += graph_section
        else:
            content += "\n## Cross-Domain Dependencies\n\n"
            content += "_No cross-domain dependencies detected._\n"

        return content

    def build_pass1_prompt(
        self,
        repo_descriptions: Dict[str, str],
        repo_list: List[Dict[str, Any]],
        previous_domains_dir: Optional[Path] = None,
    ) -> str:
        """
        Build the Pass 1 synthesis prompt string (AC5, Story #216).

        Extracts prompt-building from run_pass_1_synthesis for testability.
        When previous_domains_dir is provided and _domains.json exists there,
        includes the previous domain structure as a stability anchor.

        Args:
            repo_descriptions: Dict mapping repo alias to description text
            repo_list: List of repo dicts with alias, clone_path, file_count, total_bytes
            previous_domains_dir: Optional directory containing a previous _domains.json

        Returns:
            Prompt string ready to send to Claude CLI
        """
        repo_count = len(repo_list)
        domain_guidance = (
            "3-7"
            if repo_count <= 20
            else (
                "5-15"
                if repo_count <= 50
                else ("10-30" if repo_count <= 100 else "15-50")
            )
        )

        total_description_bytes = sum(
            len(desc.encode("utf-8")) for desc in repo_descriptions.values()
        )
        use_staging = total_description_bytes > PASS1_INLINE_DESCRIPTION_THRESHOLD_BYTES

        prompt = "# Domain Synthesis Task\n\n"
        prompt += self._build_domain_definition_section()
        prompt += "Analyze the following repository descriptions and identify domain clusters.\n\n"
        prompt += self._build_previous_domains_section(previous_domains_dir)
        prompt += "## Repository Descriptions\n\n"

        if use_staging:
            import tempfile as _tempfile

            staging_fd, staging_path_str = _tempfile.mkstemp(
                suffix=".json", prefix="cidx_pass1_descriptions_"
            )
            with os.fdopen(staging_fd, "w") as fh:
                json.dump(repo_descriptions, fh, indent=2)

            staging_path = Path(staging_path_str)
            prompt += (
                f"Repository descriptions have been written to a staging file at:\n"
                f"`{staging_path}`\n\n"
                "Use the Read tool to load that file before performing your analysis. "
                "The file is a JSON object mapping repo alias to description text.\n\n"
            )

            # Also stage the filesystem locations to avoid embedding 500-repo lists inline
            loc_fd, loc_path_str = _tempfile.mkstemp(
                suffix=".json", prefix="cidx_pass1_locations_"
            )
            repo_locations = [
                {
                    "alias": repo.get("alias", "unknown"),
                    "clone_path": repo.get("clone_path", "unknown"),
                    "file_count": repo.get("file_count", "?"),
                    "total_mb": round(repo.get("total_bytes", 0) / (1024 * 1024), 1),
                }
                for repo in repo_list
            ]
            with os.fdopen(loc_fd, "w") as fh:
                json.dump(repo_locations, fh, indent=2)

            loc_path = Path(loc_path_str)
            prompt += "## Repository Filesystem Locations\n\n"
            prompt += (
                f"Repository filesystem locations have been written to a staging file at:\n"
                f"`{loc_path}`\n\n"
                "Use the Read tool to load that file. "
                "Each entry has alias, clone_path, file_count, and total_mb fields.\n\n"
            )
        else:
            for alias, content in repo_descriptions.items():
                prompt += f"### {alias}\n\n{content}\n\n"

            prompt += "## Repository Filesystem Locations\n\n"
            for repo in repo_list:
                alias = repo.get("alias", "unknown")
                clone_path = repo.get("clone_path", "unknown")
                file_count = repo.get("file_count", "?")
                total_mb = round(repo.get("total_bytes", 0) / (1024 * 1024), 1)
                prompt += f"- **{alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
            prompt += "\n"

        prompt += f"## Instructions\n\nAIM for {domain_guidance} domains for {repo_count} repositories.\n"
        prompt += f"Assign ALL {repo_count} repositories. Missing repos = failed analysis.\n\n"
        prompt += "## Output Format\n\nYour ENTIRE response must be ONLY a valid JSON array.\n"
        prompt += '[\n  {"name": "domain-name", "description": "scope", "participating_repos": ["alias1"]}\n]\n'
        return prompt

    def _build_domain_definition_section(self) -> str:
        """Return the 'What Is a Domain?' conceptual definition section for Pass 1 prompts."""
        section = "## What Is a Domain?\n\n"
        section += "A domain is a meaningful functional or technical area that groups multiple related repositories. "
        section += "A domain is NOT a repository — it represents a higher-level organizational concept "
        section += "(e.g., 'authentication', 'data-pipeline', 'frontend-platform') that encompasses the repositories "
        section += "participating in that area. "
        section += "The purpose of domain analysis is to group repositories into these meaningful areas so that "
        section += "cross-repository dependencies can be understood at a higher abstraction level, and so that "
        section += "changes to one repository can be traced to their impact across the broader domain.\n\n"
        return section

    def _build_previous_domains_section(
        self, previous_domains_dir: Optional[Path]
    ) -> str:
        """Return previous domain structure section for Pass 1 stability, or empty string."""
        if previous_domains_dir is None:
            return ""
        prev_file = previous_domains_dir / "_domains.json"
        if not prev_file.exists():
            return ""
        try:
            prev_domains = json.loads(prev_file.read_text())
        except Exception as e:
            logger.warning(
                f"build_pass1_prompt: failed to read previous _domains.json: {e}"
            )
            return ""
        if not isinstance(prev_domains, list) or not prev_domains:
            return ""
        section = "## Previous Domain Structure (Stability Reference)\n\n"
        section += "For stability, prefer keeping the same domain names unless evidence contradicts them.\n\n"
        for domain in prev_domains:
            name = domain.get("name", "")
            desc = domain.get("description", "")
            repos = domain.get("participating_repos", [])
            section += f"- **{name}**: {desc}\n"
            if repos:
                section += f"  - Repos: {', '.join(repos)}\n"
        return section + "\n"

    def _reconcile_domains_json(
        self,
        staging_dir: Path,
        domain_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Remove ghost domains (domains without a .md file) from domain_list (AC4, Story #216).

        A ghost domain is one where Pass 2 did not produce a corresponding .md file —
        typically because Claude's output was unparseable or the domain was skipped.

        Logs a warning for each ghost removed, then overwrites _domains.json in
        staging_dir with the reconciled list.

        Args:
            staging_dir: Staging directory containing domain .md files
            domain_list: List of domain dicts from Pass 1

        Returns:
            Filtered domain list containing only domains with matching .md files
        """
        reconciled = []
        for domain in domain_list:
            name = domain.get("name", "")
            md_file = staging_dir / f"{name}.md"
            if md_file.exists():
                reconciled.append(domain)
            else:
                logger.warning(
                    f"_reconcile_domains_json: ghost domain '{name}' has no .md file — removing"
                )

        domains_file = staging_dir / "_domains.json"
        domains_file.write_text(json.dumps(reconciled, indent=2))
        logger.info(
            f"_reconcile_domains_json: kept {len(reconciled)}/{len(domain_list)} domains"
        )
        return reconciled

    @staticmethod
    def _extract_cross_domain_section(content: str) -> str:
        """
        Extract Cross-Domain section from domain markdown file.

        Finds the ## Cross-Domain heading (with or without "Connections")
        and returns all text until the next ## heading or EOF.

        Args:
            content: Domain markdown file content

        Returns:
            Cross-Domain section text, or empty string if not found
        """
        if not content:
            return ""

        lines = content.split("\n")

        # Find ## Cross-Domain heading (case-insensitive, flexible wording)
        heading_pattern = re.compile(r"^##\s+Cross[- ]Domain\b", re.IGNORECASE)

        start_idx = None
        for i, line in enumerate(lines):
            if heading_pattern.match(line.strip()):
                start_idx = i + 1  # Start collecting from next line
                break

        if start_idx is None:
            return ""

        # Collect lines until next ## heading or EOF
        result_lines = []
        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Stop at next level-2 heading
            if line.strip().startswith("## "):
                break
            result_lines.append(line)

        return "\n".join(result_lines)

    @staticmethod
    def _parse_outgoing_table_rows(cross_domain_text: str) -> List[Dict[str, str]]:
        """
        Parse rows from the '### Outgoing Dependencies' subsection.

        Expects a 6-column table: This Repo | Depends On | Target Domain | Type | Why | Evidence
        Skips header rows, separator rows, and malformed rows (< 6 columns).
        Returns empty list if sentinel text found or no outgoing section.

        Args:
            cross_domain_text: Text of the '## Cross-Domain Connections' section

        Returns:
            List of dicts with keys: source_repo, depends_on, target_domain, dep_type, why
        """
        rows = []
        in_outgoing = False
        sentinel = "No verified cross-domain dependencies."

        for line in cross_domain_text.splitlines():
            stripped = line.strip()

            # Enter outgoing section
            if stripped == "### Outgoing Dependencies":
                in_outgoing = True
                continue

            # Exit outgoing section on any other ### heading or ## heading
            if in_outgoing and stripped.startswith("##"):
                break

            if not in_outgoing:
                continue

            # Sentinel in outgoing section → no outgoing edges
            if sentinel in stripped:
                return []

            # Must be a pipe-delimited row
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue

            cells = [c.strip() for c in stripped.split("|") if c.strip()]

            # Need 6 cells: This Repo | Depends On | Target Domain | Type | Why | Evidence
            if len(cells) < 6:
                continue

            source_repo = cells[0]
            depends_on = cells[1]
            target_domain = cells[2]
            dep_type = cells[3]
            why = cells[4]

            # Skip header rows
            if source_repo in ("This Repo", "External Repo", ""):
                continue

            # Skip separator rows
            if set(source_repo) <= {"-", " "}:
                continue

            rows.append(
                {
                    "source_repo": source_repo,
                    "depends_on": depends_on,
                    "target_domain": target_domain,
                    "dep_type": dep_type,
                    "why": why,
                }
            )

        return rows

    @staticmethod
    def _build_cross_domain_graph(staging_dir: Path, domain_list: List[Dict]) -> str:
        """
        Build cross-domain dependency graph from domain files.

        Parses each domain's '### Outgoing Dependencies' structured table
        and builds a directed graph showing which domains connect to which.
        Only outgoing tables are parsed to avoid double-counting.

        Args:
            staging_dir: Directory containing domain .md files
            domain_list: List of domain dicts with 'name' and 'participating_repos'

        Returns:
            Markdown section with cross-domain graph table and summary,
            or empty string if no cross-domain edges found
        """
        # Track edges: (source_domain, target_domain) → {via_repos, dep_type, why}
        # Key: (source, target); Value: dict with via_repos set, dep_type, why
        edges: Dict[tuple, Dict] = {}

        for domain in domain_list:
            domain_name = domain["name"]
            domain_file = staging_dir / f"{domain_name}.md"

            if not domain_file.exists():
                continue

            content = domain_file.read_text()
            cross_domain_text = DependencyMapAnalyzer._extract_cross_domain_section(
                content
            )

            if not cross_domain_text:
                continue

            # Parse outgoing table rows for this domain
            outgoing_rows = DependencyMapAnalyzer._parse_outgoing_table_rows(
                cross_domain_text
            )

            for row in outgoing_rows:
                target_domain = row["target_domain"]

                # Skip self-edges
                if target_domain == domain_name:
                    continue

                edge_key = (domain_name, target_domain)
                if edge_key not in edges:
                    edges[edge_key] = {
                        "via_repos": set(),
                        "dep_type": row["dep_type"],
                        "why": row["why"],
                    }
                edges[edge_key]["via_repos"].add(row["source_repo"])

        if not edges:
            return ""

        sorted_edges = sorted(edges.items(), key=lambda x: (x[0][0], x[0][1]))

        output = "\n\n## Cross-Domain Dependency Graph\n\n"
        output += "Directed connections between domains parsed from structured Outgoing Dependencies tables.\n\n"
        output += "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
        output += "|---|---|---|---|---|\n"

        for (source, target), edge_data in sorted_edges:
            via_repos_str = ", ".join(sorted(edge_data["via_repos"]))
            dep_type = edge_data["dep_type"]
            why = edge_data["why"]
            output += (
                f"| {source} | {target} | {via_repos_str} | {dep_type} | {why} |\n"
            )

        edge_count = len(sorted_edges)
        total_domains = len(domain_list)
        domains_with_edges: set = set()
        for source, target in edges:
            domains_with_edges.add(source)
            domains_with_edges.add(target)

        all_domain_names = {d["name"] for d in domain_list}
        standalone_domains = sorted(all_domain_names - domains_with_edges)

        output += f"\n**Summary**: {edge_count} cross-domain edges across {total_domains} domains."
        if standalone_domains:
            standalone_str = ", ".join(standalone_domains)
            output += (
                f" {len(standalone_domains)} standalone domains: {standalone_str}."
            )

        return output

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Strip markdown code fences from Claude CLI output."""
        text = text.strip()
        if text.startswith("```"):
            # Remove first line (```json or ```)
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        return text

    @staticmethod
    def _has_markdown_headings(text: str) -> bool:
        """
        Check if text contains markdown headings (levels 1-3).

        Used to detect if Claude output contains actual structured content
        vs pure meta-commentary.

        Args:
            text: Text to check

        Returns:
            True if text contains at least one markdown heading (# , ## , ### )
        """
        if not text:
            return False

        for line in text.split("\n"):
            stripped = line.strip()
            if (
                stripped.startswith("# ")
                or stripped.startswith("## ")
                or stripped.startswith("### ")
            ):
                return True

        return False

    @staticmethod
    def _strip_meta_commentary(text: str) -> str:
        """
        Strip meta-commentary from Claude CLI output.

        Claude sometimes returns meta-commentary before the actual analysis content:
        - "Based on my comprehensive analysis..."
        - "Perfect. Now I have sufficient evidence..."
        - "Let me compile the findings:"

        Also strips YAML frontmatter blocks that Claude may generate.

        This method strips such lines from the beginning until actual content is found.

        Args:
            text: Raw Claude CLI output

        Returns:
            Text with meta-commentary removed
        """
        if not text:
            return text

        # Strip YAML frontmatter blocks (loop to handle multiple consecutive blocks)
        # Fix: Iteration 9 - Claude sometimes outputs two consecutive YAML frontmatter blocks
        max_iterations = 10  # Safety limit to prevent infinite loops
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            # Strip leading whitespace/newlines between YAML blocks
            text = text.lstrip()
            if not text:
                break

            lines = text.split("\n")
            stripped_first = lines[0].strip() if lines else ""

            # Track if we found and stripped a YAML block in this iteration
            stripped_yaml = False

            if stripped_first == "---":
                # Find closing ---
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        # Found closing delimiter - strip entire frontmatter
                        text = "\n".join(lines[i + 1 :])
                        stripped_yaml = True
                        break
            else:
                # Fix 2: Also detect YAML-like content without opening ---
                # Claude sometimes omits the opening delimiter
                yaml_keys = (
                    "domain:",
                    "last_analyzed:",
                    "participating_repos:",
                    "schema_version:",
                )
                first_content = stripped_first.lower()
                if any(first_content.startswith(k) for k in yaml_keys):
                    # Find closing ---
                    for i in range(1, len(lines)):
                        if lines[i].strip() == "---":
                            text = "\n".join(lines[i + 1 :])
                            stripped_yaml = True
                            break

            # If no YAML block was found/stripped, we're done
            if not stripped_yaml:
                break

        # FIX 1 (Iteration 10): Strip everything before first markdown heading
        # This handles meta-commentary like "I have enough data..." or "Good - the code-indexer..."
        # that appears before the actual analysis content starts
        lines = text.split("\n")
        first_heading_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Check for markdown heading (level 1, 2, or 3: #, ##, ###)
            if (
                stripped.startswith("# ")
                or stripped.startswith("## ")
                or stripped.startswith("### ")
            ):
                first_heading_idx = i
                break

        # If we found a heading, strip everything before it
        if first_heading_idx is not None:
            text = "\n".join(lines[first_heading_idx:])
        else:
            # No heading found - return as-is (quality gate will catch this)
            # Don't run line-by-line cleanup since there's no structured content
            return text

        # Re-split for the line-by-line meta-pattern cleanup (secondary cleanup)
        # This only runs if we found a heading above
        lines = text.split("\n")

        # Meta-commentary patterns (case-insensitive starts)
        meta_patterns = [
            "based on",
            "perfect.",
            "now i have",
            "let me",
            "i now have",
            "here is",
            "here's",
            "i have gathered",  # Fix 4
            "i have all",  # Fix 3: Pass 3 meta-commentary
            "now i can",  # Fix 4
            "i'll",  # Fix 4
            "i will",  # Fix 4
        ]

        # Find first line of actual content
        content_start_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()

            # Skip empty lines at start
            if not stripped:
                continue

            # Skip spurious YAML-like separators
            if stripped == "---":
                content_start_idx = i + 1
                continue

            # Check if line is meta-commentary
            lower = stripped.lower()
            is_meta = any(lower.startswith(pattern) for pattern in meta_patterns)

            if is_meta:
                content_start_idx = i + 1
                continue

            # Found actual content - stop stripping
            # Content lines start with: #, ##, -, |, **, or regular text
            # NOTE: Numbered lists (digits) require special handling (see below)
            if (
                stripped.startswith("#")
                or stripped.startswith("**")
                or stripped.startswith("-")
                or stripped.startswith("|")
            ):
                break

            # Special handling for numbered list items (Fix 4)
            # If line starts with digit + period + space, and doesn't contain "#",
            # it's likely Claude's internal notes before the real analysis
            if stripped and stripped[0].isdigit():
                # Check if it's a numbered list item (e.g., "1. Some text")
                if len(stripped) > 2 and stripped[1] == "." and stripped[2] == " ":
                    # Check if this numbered item contains a heading marker
                    if "#" not in stripped:
                        # It's a pre-findings note - skip and continue looking for real content
                        content_start_idx = i + 1
                        continue
                # If it's a numbered list that IS part of the content, break
                break

            # If we hit a line that doesn't match meta patterns and isn't clearly content,
            # assume it's the start of actual content
            break

        # Return from content start onwards
        result_lines = lines[content_start_idx:]

        # Strip leading empty lines from the result
        while result_lines and not result_lines[0].strip():
            result_lines.pop(0)

        # FIX 3 (Iteration 12): Strip trailing meta-commentary patterns
        # Claude sometimes adds conversational endings like "Please let me know if you need changes."
        # or standalone `---` separators before conversational text
        trailing_patterns = [
            "would you like",
            "should i ",
            "shall i ",
            "do you want",
            "i can also",
            "if you'd like",
            "is there anything",
            "do you need",
            "please ",
            "let me know",
            "if you need",
            "happy to",
            "feel free",
        ]

        # Strip from the end backwards
        while result_lines:
            last_line = result_lines[-1].strip().lower()

            # Skip trailing empty lines
            if not last_line:
                result_lines.pop()
                continue

            # Strip standalone --- separator at the end (often precedes conversational text)
            if last_line == "---":
                result_lines.pop()
                continue

            # Check for trailing meta-commentary patterns (use 'in' not 'startswith')
            if any(p in last_line for p in trailing_patterns):
                result_lines.pop()
                continue

            # No more trailing meta-commentary found
            break

        # If we stripped everything, return the original text (edge case: only meta-commentary)
        result = "\n".join(result_lines)
        if not result.strip():
            return text

        return result

    @staticmethod
    def _extract_json(text: str) -> Any:
        """
        Extract JSON from Claude CLI output, handling preambles and code fences.

        Claude CLI with --print sometimes returns natural language text before JSON:
        "Based on my analysis... [JSON]" or "```json [JSON] ```"

        This method:
        1. Strips markdown code fences (```json...```)
        2. Finds first JSON bracket ([ or {)
        3. Extracts from that position to matching closing bracket
        4. Validates and parses the JSON

        Args:
            text: Raw Claude CLI output

        Returns:
            Parsed JSON object (dict or list)

        Raises:
            ValueError: If no valid JSON found in the text
        """
        logger.debug(f"Extracting JSON from output (length={len(text)})")

        # Step 1: Strip markdown code fences
        text = DependencyMapAnalyzer._strip_code_fences(text)

        # Step 2: Find first JSON bracket
        start_idx = -1
        for i, char in enumerate(text):
            if char in "[{":
                start_idx = i
                break

        if start_idx == -1:
            raise ValueError(f"No JSON found in output (first 200 chars): {text[:200]}")

        # Step 3: Find matching closing bracket using bracket counting
        # Track string state to ignore brackets inside JSON string values
        bracket_count = 0
        end_idx = -1
        in_string = False
        escape_next = False

        for i in range(start_idx, len(text)):
            char = text[i]

            # Handle escape sequences
            if escape_next:
                escape_next = False
                continue
            if char == "\\" and in_string:
                escape_next = True
                continue

            # Track string boundaries
            if char == '"':
                in_string = not in_string
                continue

            # Only count brackets outside of strings
            if in_string:
                continue

            if char in "[{":
                bracket_count += 1
            elif char in "]}":
                bracket_count -= 1
                if bracket_count == 0:
                    end_idx = i
                    break

        if end_idx == -1:
            raise ValueError(
                f"No matching closing bracket found for JSON starting at position {start_idx} "
                f"(context: {text[start_idx : start_idx + 200]})"
            )

        # Step 4: Extract and validate JSON
        json_text = text[start_idx : end_idx + 1]
        try:
            parsed = json.loads(json_text)
            logger.debug(f"Successfully extracted JSON: {type(parsed).__name__}")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Extracted text is not valid JSON: {e} (first 200 chars): {json_text[:200]}"
            ) from e

    def _invoke_claude_cli(
        self,
        prompt: str,
        timeout: int,
        max_turns: int,
        allowed_tools: Optional[str] = None,
        post_tool_hook: Optional[str] = None,
        hook_thresholds: Optional[tuple] = None,
        dangerously_skip_permissions: bool = False,
        journal_path: Optional[Path] = None,
    ) -> str:
        """
        Invoke Claude CLI with direct subprocess (AC1).

        Invokes Claude CLI as a subprocess from golden-repos root.
        Works with both API key and Claude subscription authentication.

        Args:
            prompt: Prompt to send to Claude
            timeout: Timeout in seconds
            max_turns: Maximum number of agentic turns.
                      - 0 = single-shot print mode (no --max-turns flag, no tool use, no agentic loop)
                      - >0 = agentic mode with --max-turns N (enables tool use for up to N turns)
            allowed_tools: Tool access control for Claude CLI (only controls MCP server tools):
                          - None: No --allowedTools flag is added (all built-in tools available;
                                  MCP tools available only if registered). Built-in tools (Read, Bash,
                                  Glob, etc.) are ALWAYS available regardless of --allowedTools flag.
                          - "" (empty string): --allowedTools "" (NO MCP tools, but built-in tools still available)
                          - "tool_name": --allowedTools "tool_name" (specific MCP tool only)
            post_tool_hook: Optional PostToolUse hook reminder text. If provided with max_turns > 0,
                           adds --settings flag with PostToolUse hook that echoes the reminder after
                           each tool call. Use for Pass 2 to reinforce output format requirements.
            dangerously_skip_permissions: If True, adds --dangerously-skip-permissions flag to allow
                                         Claude CLI to write files without permission prompts.
                                         Use for Pass 1 only (file-based JSON output).

        Returns:
            Claude CLI stdout output

        Raises:
            subprocess.CalledProcessError: If Claude CLI fails
            subprocess.TimeoutExpired: If timeout is exceeded
        """
        # Build command
        cmd = [
            "claude",
            "--print",
            "--model",
            self.analysis_model,
        ]

        # Guard against negative max_turns
        if max_turns < 0:
            logger.warning(
                f"max_turns={max_turns} is negative, treating as 0 (single-shot mode)"
            )
            max_turns = 0

        # max_turns=0 means single-shot print mode (no tool use, no agentic loop)
        # max_turns>0 means agentic mode with tool use for up to N turns
        counter_file = None
        if max_turns > 0:
            cmd.extend(["--max-turns", str(max_turns)])
            # Only add --dangerously-skip-permissions if explicitly requested by caller
            if dangerously_skip_permissions:
                cmd.append("--dangerously-skip-permissions")
            # Expand sandbox to include journal dir so Claude can write activity entries
            if journal_path is not None:
                cmd.extend(["--add-dir", str(journal_path.parent)])
            # Fix 1 (Iteration 12): Turn-aware PostToolUse hook with counter file.
            # Replaces static echo with bash script that tracks tool call count and
            # escalates urgency messages as turns run out (prevents Claude from
            # exhausting all 50 turns on search without ever writing output).
            if post_tool_hook is not None:
                # Create temporary counter file
                counter_file = tempfile.NamedTemporaryFile(
                    mode="w", prefix="depmap_hook_", suffix=".cnt", delete=False
                )
                counter_file.write("0")
                counter_file.close()

                # Calculate thresholds
                if hook_thresholds is not None:
                    early_threshold, late_threshold = hook_thresholds
                else:
                    early_threshold = max(5, int(max_turns * 0.3))
                    late_threshold = max(10, int(max_turns * 0.6))

                # Build bash script: counter (Bug #838 — F='path' always single-quoted so
                # test path-rewriting regex ^F='[^']*' matches), STATUS NUDGE at turn 10,
                # optional journal entries per tool type, and escalating urgency messages.
                _counter_sq = counter_file.name.replace("'", "'\\''")
                if journal_path is not None:
                    _journal_sq = str(journal_path).replace("'", "'\\''")
                    _journal_block = (
                        f"JRNL='{_journal_sq}'\n"
                        'case "$CLAUDE_TOOL_NAME" in\n'
                        "  Read) NAR='Claude read file' ;;\n"
                        "  Bash) NAR='Claude ran bash' ;;\n"
                        "  Grep) NAR='Claude searched' ;;\n"
                        "  Glob) NAR='Claude listed files' ;;\n"
                        "  Write|Edit) NAR='Claude wrote file' ;;\n"
                        '  *) NAR="Claude ran $CLAUDE_TOOL_NAME" ;;\n'
                        "esac\n"
                        'echo "**claude-tool** | $(date +%H:%M:%S) | $NAR" >> "$JRNL"\n'
                    )
                else:
                    _journal_block = ""
                bash_script = (
                    f"F='{_counter_sq}'\n"
                    'C=$(cat "$F" 2>/dev/null || echo 0)\n'
                    "C=$((C+1))\n"
                    'echo "$C" > "$F"\n'
                    'if [ "$C" -eq 10 ]; then '
                    "echo 'STATUS NUDGE: Briefly report what you have found so far and what you plan to do next.'; "
                    "fi\n" + _journal_block + f'if [ "$C" -gt {late_threshold} ]; then '
                    f"echo {shlex.quote('CRITICAL: STOP searching. Write your concise dependency analysis NOW. Document precise inter-repo connections only — no code snippets, no implementation details. Start with # Domain Analysis heading.')}; "
                    f'elif [ "$C" -gt {early_threshold} ]; then '
                    f"echo {shlex.quote('WARNING: Running low on turns. Start writing your concise dependency analysis. Focus on precise inter-repo connections for navigation — no code snippets, no verbose details. Output starts with # Domain Analysis heading.')}; "
                    f"else echo {shlex.quote(post_tool_hook)}; fi"
                )

                hook_settings = json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {
                                    "matcher": "",
                                    "command": f"bash -c {shlex.quote(bash_script)}",
                                }
                            ]
                        }
                    }
                )
                cmd.extend(["--settings", hook_settings])

        # Add --allowedTools only if specified
        if allowed_tools is not None:
            cmd.extend(["--allowedTools", allowed_tools])

        # Prompt passed via stdin (not command-line) to avoid E2BIG with large prompts
        prompt_size_kb = len(prompt.encode("utf-8")) / 1024
        logger.info(
            f"Claude CLI prompt size: {prompt_size_kb:.1f} KB, "
            f"cmd args: {len(' '.join(cmd))} chars"
        )

        # Story #724 AC5 (unconditional gating): always acquire the shared verification
        # semaphore before any Claude CLI subprocess. Read max_concurrent from the same
        # config source that _execute_verification_cli uses so both code paths initialize
        # the singleton with the same value — preventing ValueError on cold-start sequences
        # where _invoke_claude_cli fires before any verification pass.
        try:
            from code_indexer.server.services.config_service import get_config_service

            _max_concurrent = (
                get_config_service()
                .get_config()
                .claude_integration_config.max_concurrent_claude_cli
            )
        except (ImportError, AttributeError):
            # ImportError  : server package not installed (CLI-only context).
            # AttributeError: config object structure differs (non-server context).
            # Both cases are expected non-server deployments; use the schema default.
            _max_concurrent = _DEFAULT_MAX_CONCURRENT_CLAUDE_CLI
            logger.debug(
                "_invoke_claude_cli: config service unavailable; using max_concurrent=%d",
                _max_concurrent,
            )
        # _get_verification_semaphore is idempotent: returns existing semaphore if already
        # initialized with the same capacity, or creates it on cold start.
        _sem = _get_verification_semaphore(_max_concurrent)

        # Run subprocess (wrapped in try/finally to ensure semaphore release and
        # counter file cleanup)
        _sem.acquire()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.golden_repos_root),
                capture_output=True,
                text=True,
                timeout=timeout,
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
                input=prompt,  # Pass prompt via stdin to avoid ARG_MAX (E2BIG) with large prompts
            )
        finally:
            # Release semaphore before counter file cleanup
            _sem.release()
            # Clean up counter file if it was created
            if counter_file is not None:
                try:
                    os.unlink(counter_file.name)
                except OSError as exc:
                    # Non-fatal: counter file may already be absent; log for diagnostics
                    logger.debug(
                        "Failed to delete counter file %s: %s", counter_file.name, exc
                    )

        # Diagnostic logging for debugging empty output issues
        raw_stdout_len = len(result.stdout) if result.stdout else 0
        raw_stderr_len = len(result.stderr) if result.stderr else 0
        logger.info(
            f"Claude CLI completed: returncode={result.returncode}, "
            f"stdout={raw_stdout_len} chars, stderr={raw_stderr_len} chars"
        )
        if raw_stdout_len == 0:
            logger.warning(
                f"Claude CLI returned EMPTY stdout. "
                f"stderr (first 1000 chars): {(result.stderr or '')[:1000]}"
            )
        elif raw_stdout_len < 100:
            logger.warning(f"Claude CLI returned very short stdout: {result.stdout!r}")
        else:
            logger.debug(f"Claude CLI stdout (first 500 chars): {result.stdout[:500]}")

        if result.returncode != 0:
            logger.error(f"Claude CLI failed: {result.stderr}")
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        return self._strip_code_fences(result.stdout)

    # ========================================================================
    # Story #193: Delta Refresh Prompt Methods
    # ========================================================================

    def build_delta_merge_prompt(
        self,
        domain_name: str,
        existing_content: str,
        changed_repos: List[str],
        new_repos: List[str],
        removed_repos: List[str],
        domain_list: List[str],
        journal_path: Optional[Path] = None,
    ) -> str:
        """
        Build delta merge prompt with self-correction mandate (Story #193).

        Args:
            domain_name: Name of the domain to update
            existing_content: Current domain file content
            changed_repos: List of changed repo aliases
            new_repos: List of new repo aliases
            removed_repos: List of removed repo aliases
            domain_list: List of all domain names for cross-domain awareness
            journal_path: Optional path to activity journal file (Story #329)

        Returns:
            Prompt for Claude CLI delta merge
        """
        prompt = f"# Delta Update for Domain: {domain_name}\n\n"

        prompt += "## Task\n\n"
        prompt += "Update the existing domain analysis by incorporating changes from modified repositories.\n\n"

        prompt += "## Existing Domain Analysis\n\n"
        prompt += (
            "The existing domain analysis is provided as a file you will edit in place. "
            "Use the Read tool to load it, reason about the delta, then apply surgical edits "
            "via the Edit tool. Do NOT reproduce the full document inline.\n\n"
        )

        prompt += "## Changed Repositories\n\n"
        if changed_repos:
            prompt += "Re-verify ALL dependencies for these repos (commit changes detected):\n"
            for repo in changed_repos:
                if isinstance(repo, dict):
                    alias = repo.get("alias", "unknown")
                    clone_path = repo.get("clone_path")
                    prompt += f"- **{alias}**"
                    if clone_path:
                        prompt += f": `{clone_path}`"
                    prompt += "\n"
                else:
                    prompt += f"- {repo}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## New Repositories\n\n"
        if new_repos:
            prompt += "Incorporate these newly registered repos:\n"
            for repo in new_repos:
                if isinstance(repo, dict):
                    alias = repo.get("alias", "unknown")
                    clone_path = repo.get("clone_path")
                    prompt += f"- **{alias}**"
                    if clone_path:
                        prompt += f": `{clone_path}`"
                    prompt += "\n"
                else:
                    prompt += f"- {repo}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## Removed Repositories\n\n"
        if removed_repos:
            prompt += "Remove ALL references to these repos (no longer registered):\n"
            for alias in removed_repos:
                prompt += f"- {alias}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## Domain Context\n\n"
        prompt += "All domains in this analysis:\n"
        for domain in domain_list:
            prompt += f"- {domain}\n"
        prompt += "\n"

        # Compute unchanged repos: extract all repo aliases from existing_content
        # (headings matching "### alias" pattern in Repository Roles section only),
        # then subtract changed, new, and removed aliases.
        repo_roles_match = re.search(
            r"## Repository Roles\s*\n(.*?)(?=\n## |\Z)",
            existing_content,
            re.DOTALL,
        )
        if repo_roles_match:
            repo_roles_section = repo_roles_match.group(1)
            all_repos_in_domain = set(
                re.findall(r"^### ([\w][\w\-\.]*)", repo_roles_section, re.MULTILINE)
            )
        else:
            all_repos_in_domain = set()
        changed_aliases = {
            r.get("alias", "") if isinstance(r, dict) else r  # type: ignore[attr-defined]
            for r in changed_repos
        }
        new_aliases = {
            r.get("alias", "") if isinstance(r, dict) else r  # type: ignore[attr-defined]
            for r in new_repos
        }
        removed_aliases = set(removed_repos)
        unchanged_repos = (
            all_repos_in_domain - changed_aliases - new_aliases - removed_aliases
        )
        # Remove empty strings that may result from dict repos with no alias key
        unchanged_repos.discard("")

        if unchanged_repos:
            prompt += "## Unchanged Repositories\n\n"
            prompt += (
                "These repos had NO code changes. "
                "You MUST preserve their analysis exactly as-is.\n"
                "Do NOT remove, modify, or question dependencies involving these repos:\n"
            )
            for repo in sorted(unchanged_repos):
                prompt += f"- {repo}\n"
            prompt += "\n"

        prompt += "## Source Code Exploration (MCP Tool)\n\n"
        prompt += "You MUST use the `cidx-local` MCP server's `search_code` tool to verify changes.\n"
        prompt += "For each CHANGED or NEW repository, use `search_code` to find:\n"
        prompt += "- Cross-repository references (who calls this repo)\n"
        prompt += "- Integration patterns that may have changed\n"
        prompt += "- New dependencies introduced by the code changes\n\n"
        prompt += "Call `search_code` with the repo name, class names, or API endpoints to discover connections.\n\n"

        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += (
            "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        )
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += (
            "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        )
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n\n"

        prompt += "## CRITICAL SELF-CORRECTION RULES\n\n"
        prompt += "1. For every CHANGED repo: re-verify ALL dependencies listed for that repo against current source code\n"
        prompt += "2. For CHANGED repos ONLY: remove dependencies no longer present in source code (do NOT preserve stale deps)\n"
        prompt += (
            "3. CORRECT dependencies where the nature of the relationship changed\n"
        )
        prompt += "4. ADD new dependencies discovered in changed/new repos\n"
        prompt += "5. For UNCHANGED repos: preserve ALL existing analysis EXACTLY as-is — do NOT remove or modify their dependencies\n"
        prompt += (
            "6. NEVER remove a cross-domain dependency involving an UNCHANGED repo\n"
        )
        prompt += "7. Cross-Domain Connections MUST use the structured table format with Outgoing and Incoming subsections\n\n"

        prompt += "## Evidence-Based Claims Requirement\n\n"
        prompt += "Every dependency you document MUST include a source reference (module/subsystem name) and evidence type.\n"
        prompt += "For CHANGED repos: Do NOT preserve or add dependencies you cannot verify from current source code.\n"
        prompt += '"I assume this exists" is NOT evidence. "I found import X in module Y" IS evidence.\n\n'

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"
        prompt += "**CORRECT**: 'auth-service JWT subsystem provides token validation consumed by web-app middleware layer'\n\n"
        prompt += "**INCORRECT (too granular)**: 'auth-service/src/jwt/validator.py:validate_token() called by web-app/src/middleware/auth.py'\n\n"
        prompt += "**INCORRECT (too abstract)**: 'auth-service is used by web-app'\n\n"

        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
        prompt += (
            "- Advisory content about what SHOULD be done or COULD be integrated\n"
        )
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
        prompt += "You MUST return the COMPLETE updated domain analysis document.\n"
        prompt += "Do NOT return only a summary of changes — return the full document with all sections.\n\n"
        prompt += "The output MUST include ALL of the following sections:\n"
        prompt += "- Overview\n"
        prompt += "- Repository Roles\n"
        prompt += "- Intra-Domain Dependencies\n"
        prompt += "- Cross-Domain Connections\n\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n\n"
        prompt += "The Cross-Domain Connections section MUST use the following structured table format:\n\n"
        prompt += _CROSS_DOMAIN_SCHEMA

        if journal_path is not None:
            prompt += self._build_activity_journal_appendix(journal_path)

        return prompt

    def build_domain_discovery_prompt(
        self,
        new_repos: List[Dict[str, Any]],
        existing_domains: List[str],
        journal_path: Optional[Path] = None,
    ) -> str:
        """
        Build domain discovery prompt for new repos (Story #193).

        Args:
            new_repos: List of new repo dicts with alias and description_summary
            existing_domains: List of existing domain names
            journal_path: Optional path to activity journal file (Story #329)

        Returns:
            Prompt for discovering which domains new repos belong to
        """
        prompt = "# Domain Discovery for New Repositories\n\n"

        prompt += "## New Repositories\n\n"
        for repo in new_repos:
            alias = repo.get("alias", "unknown")
            summary = repo.get("description_summary", "No description")
            prompt += f"- **{alias}**: {summary}\n"
        prompt += "\n"

        prompt += "## Existing Domains\n\n"
        for domain in existing_domains:
            prompt += f"- {domain}\n"
        prompt += "\n"

        prompt += "## Task\n\n"
        prompt += "For each new repository, determine which existing domain(s) it belongs to, "
        prompt += "or identify if it represents a new domain.\n\n"

        prompt += "Output JSON array:\n"
        prompt += "[\n"
        prompt += '  {"repo": "alias", "domains": ["domain1", "domain2"]}\n'
        prompt += "]\n"

        if journal_path is not None:
            prompt += self._build_activity_journal_appendix(journal_path)

        return prompt

    def build_new_domain_prompt(
        self,
        domain_name: str,
        participating_repos: List[str],
    ) -> str:
        """
        Build prompt for creating a new domain file (Story #193).

        Args:
            domain_name: Name of the new domain
            participating_repos: List of repo aliases in this domain

        Returns:
            Prompt for generating new domain analysis
        """
        prompt = f"# Create New Domain: {domain_name}\n\n"

        prompt += "## Participating Repositories\n\n"
        for alias in participating_repos:
            prompt += f"- {alias}\n"
        prompt += "\n"

        prompt += "## Task\n\n"
        prompt += f"Analyze source code to create a new domain analysis for '{domain_name}'.\n\n"

        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += (
            "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        )
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += (
            "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        )
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n\n"

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"

        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
        prompt += (
            "- Advisory content about what SHOULD be done or COULD be integrated\n"
        )
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
        prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n\n"
        prompt += "The Cross-Domain Connections section MUST use the following structured table format:\n\n"
        prompt += _CROSS_DOMAIN_SCHEMA

        return prompt

    def _sanitize_domain_name_for_path(self, domain_name: str) -> str:
        """Return a filesystem-safe version of domain_name for use in filenames."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", domain_name)

    def _read_pass2_file_or_strip(self, body_file: Path, stdout_result: str) -> str:
        """Read from body_file if Claude wrote it, else strip meta-commentary from stdout."""
        if body_file.exists() and body_file.stat().st_size > 0:
            content = body_file.read_text()
            body_file.unlink()
            return content
        return self._strip_meta_commentary(stdout_result)

    def _build_file_based_instructions(self, temp_file: Path) -> str:
        """Build the prompt suffix that instructs Claude to edit temp_file in place."""
        try:
            temp_file_rel = temp_file.relative_to(self.golden_repos_root)
        except ValueError:
            temp_file_rel = temp_file
        return (
            f"\n\n## CRITICAL: File-Based Output\n\n"
            f"You MUST edit the domain document file directly using the Edit tool.\n"
            f"The current domain document is at: `{temp_file}`\n"
            f"Relative path from your cwd: `./{temp_file_rel}`\n\n"
            f"1. Read the file at the path above\n"
            f"2. Apply your changes using the Edit tool (NOT stdout)\n"
            f"3. After ALL edits are complete, print exactly this line: FILE_EDIT_COMPLETE\n"
            f"4. If NO changes are needed, print exactly this line instead: FILE_UNCHANGED\n\n"
            f"Do NOT output the full document to stdout. Edit the FILE.\n"
        )

    def _verify_file_modified(
        self, temp_file: Path, original_mtime: float, domain_name: str
    ) -> bool:
        """Return True if temp_file mtime changed; log and return False otherwise."""
        try:
            new_mtime = temp_file.stat().st_mtime
        except OSError:
            logger.error(f"Temp file disappeared after operation for '{domain_name}'")
            return False
        if new_mtime == original_mtime:
            logger.warning(
                f"File not modified for '{domain_name}' (mtime unchanged). "
                f"Returning False to preserve original."
            )
            return False
        return True

    def _read_file_if_changed(
        self, temp_file: Path, existing_content: str, domain_name: str, operation: str
    ) -> Optional[str]:
        """Read temp_file; return content if different from existing_content, else None."""
        updated_content = temp_file.read_text()
        if updated_content.strip() == existing_content.strip():
            logger.info(f"{operation} produced no changes for '{domain_name}'")
            return None
        return updated_content

    def invoke_delta_merge_file(
        self,
        domain_name: str,
        existing_content: str,
        merge_prompt: str,
        timeout: int,
        max_turns: int,
        temp_dir: Path,
    ) -> Optional[str]:
        """
        File-based delta merge: writes existing content to temp file,
        Claude edits in-place via Edit tool (Story #715).

        allowed_tools=None lets Claude use all tools: built-in Edit (for in-place
        file edits) and MCP search tools (for code lookup during merge).

        Args:
            domain_name: Name of the domain being updated
            existing_content: Current domain file content
            merge_prompt: The delta merge prompt (from build_delta_merge_prompt)
            timeout: Timeout in seconds
            max_turns: Maximum agentic turns
            temp_dir: Directory for temp file

        Returns:
            Updated content string, or None on failure (caller preserves original)
        """
        safe_name = self._sanitize_domain_name_for_path(domain_name)
        temp_file = temp_dir / f"_delta_merge_{safe_name}.md"
        try:
            # Bug #834: strip YAML frontmatter before handing the file to Claude.
            # Claude is instructed NOT to add frontmatter; if the temp file already
            # contains a frontmatter block, Claude would encounter it and the result
            # would end up with duplicate --- delimiters.  The original
            # existing_content (with frontmatter) is preserved in the caller for
            # _update_frontmatter_timestamp to reconstruct the final file.
            body_only = _strip_leading_yaml_frontmatter(existing_content)
            temp_file.write_text(body_only)
            original_mtime = temp_file.stat().st_mtime
            prompt = merge_prompt + self._build_file_based_instructions(temp_file)
            try:
                result = self._invoke_claude_cli(
                    prompt,
                    timeout,
                    max_turns,
                    allowed_tools="mcp__cidx-local__search_code",
                    dangerously_skip_permissions=True,
                )
            except Exception as e:
                logger.error(
                    f"Delta merge file invocation failed for '{domain_name}': {e}"
                )
                return None
            if "FILE_UNCHANGED" in (result or ""):
                logger.info(
                    f"Delta merge intentional no-op for '{domain_name}' "
                    f"— FILE_UNCHANGED signal received"
                )
                return _DELTA_NOOP
            if not self._verify_file_modified(temp_file, original_mtime, domain_name):
                return None
            if "FILE_EDIT_COMPLETE" not in (result or ""):
                logger.warning(
                    f"Completion signal missing for delta merge of '{domain_name}'"
                )
            updated = self._read_file_if_changed(
                temp_file, existing_content, domain_name, "Delta merge"
            )
            # Bug #834 (Step 3): defensive sanitization — Claude should never add
            # frontmatter to the body-only temp file, but strip it if it does and
            # log a WARNING so the violation is visible (Messi Rule #13).
            if updated is not None:
                stripped = _strip_leading_yaml_frontmatter(updated)
                if stripped != updated:
                    logger.warning(
                        f"Claude returned frontmatter in body-only path for "
                        f"'{domain_name}'; stripped it"
                    )
                    updated = stripped
            return updated
        finally:
            self._cleanup_temp_file(temp_file)

    def invoke_new_domain_generation(
        self, prompt: str, timeout: int, max_turns: int
    ) -> str:
        """
        Invoke Claude CLI to generate a new domain document from scratch (Story #715).

        Used when a domain file is missing and needs creation. Output is small enough
        that stdout is adequate (unlike delta merge/refinement of large existing docs).

        Args:
            prompt: New domain generation prompt
            timeout: Timeout in seconds
            max_turns: Maximum agentic turns

        Returns:
            Claude CLI stdout output (new domain document body)
        """
        return self._invoke_claude_cli(prompt, timeout, max_turns, allowed_tools=None)

    def invoke_domain_discovery(self, prompt: str, timeout: int, max_turns: int) -> str:
        """
        Invoke Claude CLI for domain discovery of new repositories (Story #216, H1 fix).

        Public method for domain discovery invocations that maintains encapsulation
        by wrapping the private _invoke_claude_cli method. Domain discovery does not
        need MCP search tools since it only processes repo metadata.

        Args:
            prompt: Domain discovery prompt to send to Claude
            timeout: Timeout in seconds
            max_turns: Maximum number of agentic turns

        Returns:
            Claude CLI stdout output (JSON list of repo-to-domain assignments)

        Raises:
            subprocess.CalledProcessError: If Claude CLI fails
            subprocess.TimeoutExpired: If timeout is exceeded
        """
        return self._invoke_claude_cli(prompt, timeout, max_turns, allowed_tools=None)

    def build_refinement_prompt(
        self,
        domain_name: str,
        existing_body: str,
        participating_repos: List[str],
    ) -> str:
        """
        Build editorial fact-checking prompt for refining an existing domain file (Story #359).

        This is EDITORIAL (fact-checking existing docs against source code), not
        AUTHORIAL (not rewriting from scratch). The prompt instructs Claude to verify
        existing claims, correct inaccuracies, and preserve the document structure.

        Args:
            domain_name: Name of the domain being refined
            existing_body: Existing document body (without frontmatter)
            participating_repos: List of repo aliases in this domain

        Returns:
            Refinement prompt string
        """
        prompt = f"# Refine Domain Document: {domain_name}\n\n"

        prompt += "## Task\n\n"
        prompt += (
            f"Fact-check and refine the existing domain analysis for '{domain_name}'. "
            "This is an EDITORIAL task: verify existing claims against source code, "
            "correct inaccuracies, and preserve the document structure. "
            "Do NOT rewrite from scratch.\n\n"
        )

        prompt += "## Participating Repositories\n\n"
        for alias in participating_repos:
            prompt += f"- {alias}\n"
        prompt += "\n"

        prompt += "## Existing Document\n\n"
        prompt += (
            "The existing document body has been written to a temp file. "
            "Use the Read tool to load the file at the path provided to you, "
            "then review its contents against the actual source code.\n\n"
        )

        prompt += "## Refinement Instructions\n\n"
        prompt += "Review the existing document (Read the temp file) against the actual source code and:\n\n"
        prompt += "1. **Verify** all stated dependencies against source code evidence\n"
        prompt += "2. **Correct** any inaccurate or outdated information\n"
        prompt += "3. **Preserve** the document structure and all sections\n"
        prompt += "4. **Retain** all accurate, evidence-supported content\n"
        prompt += "5. **Remove** only claims not supported by source code evidence\n\n"

        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', or 'Future Considerations'\n"
        prompt += (
            "- Advisory content about what SHOULD be done or COULD be integrated\n"
        )
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships.\n\n"

        prompt += "## Output Format\n\n"
        prompt += f"Return the full refined document body starting with '# Domain Analysis: {domain_name}'.\n"
        prompt += "Output ONLY the content body (no YAML frontmatter, no markdown code blocks, no preamble).\n"
        prompt += "Maintain the same section structure as the existing document.\n"

        return prompt

    def invoke_refinement_file(
        self,
        domain_name: str,
        existing_content: str,
        refinement_prompt: str,
        timeout: int,
        max_turns: int,
        temp_dir: Path,
    ) -> Optional[str]:
        """
        File-based refinement: writes existing content to temp file,
        Claude edits in-place via Edit tool (Story #715).

        Args:
            domain_name: Name of the domain being refined
            existing_content: Current domain body content (without frontmatter)
            refinement_prompt: The refinement prompt (from build_refinement_prompt)
            timeout: Timeout in seconds
            max_turns: Maximum agentic turns
            temp_dir: Directory for temp file

        Returns:
            Updated content string, or None on failure
        """
        safe_name = self._sanitize_domain_name_for_path(domain_name)
        temp_file = temp_dir / f"_refinement_{safe_name}.md"
        try:
            temp_file.write_text(existing_content)
            original_mtime = temp_file.stat().st_mtime
            prompt = refinement_prompt + self._build_file_based_instructions(temp_file)
            try:
                result = self._invoke_claude_cli(
                    prompt,
                    timeout,
                    max_turns,
                    allowed_tools=None,
                    dangerously_skip_permissions=True,
                )
            except Exception as e:
                logger.error(
                    f"Refinement file invocation failed for '{domain_name}': {e}"
                )
                return None
            if not self._verify_file_modified(temp_file, original_mtime, domain_name):
                return None
            if "FILE_EDIT_COMPLETE" not in (result or ""):
                logger.warning(
                    f"Completion signal missing for refinement of '{domain_name}'"
                )
            return self._read_file_if_changed(
                temp_file, existing_content, domain_name, "Refinement"
            )
        finally:
            self._cleanup_temp_file(temp_file)

    def _cleanup_temp_file(self, temp_file: Path) -> None:
        """Remove temp file, logging but not raising on failure."""
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError as e:
            logger.warning("Failed to remove temp file '%s': %s", temp_file, e)

    # ========================================================================
    # Story #724: Post-generation verification pass
    # ========================================================================

    def invoke_verification_pass(
        self,
        document_path: Path,
        repo_list: list,
        config: Any,  # duck-typed; avoids circular import of ClaudeIntegrationConfig
    ) -> None:
        """Post-generation verification pass — file-edit contract (Story #724 v2).

        Claude reads the file at document_path, verifies every claim against source,
        edits the file in-place, then prints FILE_EDIT_COMPLETE.  Two attempts are
        made; the file is re-seeded from original content before each attempt so a
        partial edit by attempt 1 cannot corrupt attempt 2.

        Postconditions checked after each subprocess call:
          1. subprocess.TimeoutExpired — caught, triggers retry
          2. subprocess.CalledProcessError — caught, triggers retry
          3. stdout does not end with "FILE_EDIT_COMPLETE" as last non-empty line
          4. temp file is empty or whitespace-only after subprocess return

        Raises:
            VerificationFailed: If both attempts fail any postcondition check.
        """
        if not isinstance(document_path, Path):
            raise ValueError("document_path must be a pathlib.Path")
        if not isinstance(repo_list, list):
            raise ValueError("repo_list must be a list")
        if config is None:
            raise ValueError("config must not be None")

        prompt = self._build_verification_prompt(document_path, repo_list)
        original_content = document_path.read_text(encoding="utf-8")
        started = time.monotonic()

        for attempt in (1, 2):
            document_path.write_text(original_content, encoding="utf-8")
            failure = self._run_verification_attempt(
                document_path, prompt, config, attempt
            )
            if failure is None:
                duration_ms = int(round((time.monotonic() - started) * 1000))
                logger.info(
                    "verification pass completed: domain=%s duration_ms=%d attempt=%d",
                    document_path.stem,
                    duration_ms,
                    attempt,
                )
                return

        raise VerificationFailed(f"verification failed twice for {document_path}")

    def _build_verification_prompt(self, document_path: Path, repo_list: list) -> str:
        """Build the full verification prompt: rendered fact_check.md + file instructions."""
        from code_indexer.global_repos.prompts import get_prompt  # lazy import

        repo_text = "\n".join(
            f"- {r.get('alias', 'unknown')}: {r.get('clone_path', 'unknown')}"
            for r in repo_list
        )
        base_prompt: str = str(get_prompt("fact_check")).replace(
            "{repo_list}", repo_text
        )
        return base_prompt + self._build_file_based_instructions(document_path)

    def _run_verification_attempt(
        self,
        document_path: Path,
        prompt: str,
        config: Any,  # duck-typed; avoids circular import of ClaudeIntegrationConfig
        attempt: int,
    ) -> Optional[str]:
        """Execute one verification subprocess and check all postconditions.

        Returns None on full success, or a short failure-reason string on any failure.
        Logs WARNING for each failure; the caller (invoke_verification_pass) retries or raises.
        """
        try:
            stdout = self._invoke_claude_cli(
                prompt,
                config.fact_check_timeout_seconds,
                config.dependency_map_delta_max_turns,
                dangerously_skip_permissions=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning("invoke_verification_pass: attempt %d timed out", attempt)
            return "timeout"
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "invoke_verification_pass: attempt %d exit=%d", attempt, exc.returncode
            )
            return "cli_error"

        return self._check_verification_postconditions(document_path, stdout, attempt)

    def _check_verification_postconditions(
        self,
        document_path: Path,
        stdout: str,
        attempt: int,
    ) -> Optional[str]:
        """Check all postconditions after a verification subprocess returns.

        Returns None on success (all checks pass). Returns a reason string
        (e.g., "empty_file", "missing_sentinel") on failure.
        Failure reason strings are used for WARNING log messages.

        Postconditions checked (4 total):
          1. subprocess.TimeoutExpired — handled by caller before this function
          2. subprocess.CalledProcessError — handled by caller before this function
          3. FILE_EDIT_COMPLETE not the last non-empty line of stdout
          4. temp file is empty or whitespace-only after subprocess return
        """
        # Sentinel must be the last non-empty line, not merely present as substring
        non_empty_lines = [
            line.strip() for line in (stdout or "").splitlines() if line.strip()
        ]
        if not non_empty_lines or non_empty_lines[-1] != "FILE_EDIT_COMPLETE":
            logger.warning(
                "invoke_verification_pass: attempt %d missing FILE_EDIT_COMPLETE sentinel on final line",
                attempt,
            )
            return "missing_sentinel"

        try:
            current_content = document_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "invoke_verification_pass: attempt %d could not read file", attempt
            )
            return "file_missing"

        if not current_content.strip():
            logger.warning(
                "invoke_verification_pass: attempt %d produced empty file", attempt
            )
            return "empty_file"

        return None
