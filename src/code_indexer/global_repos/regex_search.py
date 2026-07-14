"""
Regex search service for global repository file pattern matching.

Provides ripgrep-style regex search with grep fallback for searching
directly against files on disk in global repositories.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from code_indexer.server.services.subprocess_executor import (
    SubprocessExecutor,
    ExecutionStatus,
)

logger = logging.getLogger(__name__)

# Default timeout for search operations (5 minutes)
DEFAULT_SEARCH_TIMEOUT_SECONDS = 300

# Timeout for PCRE2 availability check subprocess
_PCRE2_CHECK_TIMEOUT_SEC = 5

# Above this many trigram-index candidates, skip the pre-filter and full-scan:
# the ripgrep arg list would be unwieldy and the filter is not selective enough
# to beat a plain scan.
_MAX_PREFILTER_CANDIDATES = 8000

# Lazy trigram-index rebuild: when a regex search finds no compatible trigram
# index (missing, or stale/old-format per the schema-version guard), kick off a
# one-shot background build so the index self-heals before the next scheduled
# golden-repo refresh. This request still full-scans; later ones use the index.
# Disable by setting CIDX_TRIGRAM_LAZY_BUILD=0.
_LAZY_BUILD_COOLDOWN_SEC = 300
_lazy_build_lock = threading.Lock()
_lazy_build_in_progress: "set[str]" = set()
_lazy_build_last_attempt: "dict[str, float]" = {}


def _lazy_build_enabled() -> bool:
    return os.environ.get("CIDX_TRIGRAM_LAZY_BUILD", "1") not in ("0", "false", "False")


def _maybe_trigger_lazy_index_build(repo_path: Path) -> None:
    """Start a background trigram-index build for ``repo_path`` if none is
    already running/recent. Best-effort; never raises into the caller."""
    if not _lazy_build_enabled():
        return
    key = str(repo_path)
    now = time.monotonic()
    with _lazy_build_lock:
        if key in _lazy_build_in_progress:
            return  # a build is already running for this repo
        last = _lazy_build_last_attempt.get(key)
        if last is not None and now - last < _LAZY_BUILD_COOLDOWN_SEC:
            return  # backed off after a recent attempt (avoid retry storms)
        _lazy_build_in_progress.add(key)
        _lazy_build_last_attempt[key] = now

    def _run() -> None:
        try:
            from .trigram_index_manager import TrigramIndexManager

            index_dir = repo_path / ".code-indexer" / "trigram_index"
            n = TrigramIndexManager(index_dir).build(repo_path)
            logger.info("lazy trigram index build complete for %s (%d files)", key, n)
        except Exception as exc:  # never let the background build crash the server
            logger.warning("lazy trigram index build failed for %s: %s", key, exc)
        finally:
            with _lazy_build_lock:
                _lazy_build_in_progress.discard(key)
                _lazy_build_last_attempt[key] = time.monotonic()

    threading.Thread(target=_run, name="trigram-lazy-build", daemon=True).start()


class RipgrepExecutionError(Exception):
    """Raised when ripgrep/grep exits with a non-zero code AND has stderr output.

    Finding 3.1 (v10.4.4): Previously these errors were swallowed (log + return
    empty), causing callers to see COMPLETED status with silently empty results.
    Raising here lets XRaySearchEngine surface phase1_failed in the job result.
    """


@dataclass
class RegexMatch:
    """A single regex match result."""

    file_path: str
    line_number: int
    column: int
    line_content: str
    context_before: List[str] = field(default_factory=list)
    context_after: List[str] = field(default_factory=list)


@dataclass
class RegexSearchResult:
    """Result of a regex search operation."""

    matches: List[RegexMatch]
    total_matches: int
    truncated: bool
    search_engine: str
    search_time_ms: float


class RegexSearchService:
    """Service for performing regex searches on repository files."""

    # Set once, process-wide, the first time we degrade to the grep fallback so
    # the warning in _detect_search_engine is emitted a single time, not per call.
    _grep_fallback_warned: bool = False

    def __init__(self, repo_path: Path, subprocess_max_workers: int = 2):
        """Initialize the regex search service.

        Args:
            repo_path: Path to the repository root
            subprocess_max_workers: Maximum concurrent workers for subprocess execution
                (default: 2, per Story #27 resource audit recommendation)

        Raises:
            RuntimeError: If neither ripgrep nor grep is available
        """
        # Bug #1401: canonicalize the repo root ONCE, here, rather than at
        # scattered per-call-site resolutions. self.repo_path is the single
        # source of truth every relative_to() comparison inside this service
        # (trigram pre-filter, ripgrep JSON parsing, grep-mode parsing,
        # Python-multiline fallback parsing) is checked against, via
        # _to_repo_relative(). Without this, an unresolved symlinked repo
        # root desyncs against candidate paths that subprocesses report back
        # resolved, raising an uncaught pathlib ValueError downstream.
        self.repo_path = Path(repo_path).resolve()
        self._subprocess_max_workers = subprocess_max_workers
        self._search_engine = self._detect_search_engine()
        self._pcre2_supported: Optional[bool] = None  # Lazy-detected, cached

    def _detect_search_engine(self) -> str:
        """Detect available search engine (ripgrep preferred).

        Returns:
            String identifying the search engine ("ripgrep" or "grep")

        Raises:
            RuntimeError: If neither ripgrep nor grep is found
        """
        if shutil.which("rg"):
            return "ripgrep"
        elif shutil.which("grep"):
            # ripgrep is absent -> we degrade to a linear `grep -r` scan that has
            # no gitignore awareness and reads the entire working tree. On large
            # repos this reliably hits the search timeout. Warn loudly (once) so
            # the degradation is visible instead of manifesting as opaque 30s
            # timeouts. Install ripgrep in the deployment image to avoid this.
            if not RegexSearchService._grep_fallback_warned:
                RegexSearchService._grep_fallback_warned = True
                logger.warning(
                    "ripgrep (rg) not found on PATH; falling back to 'grep -r'. "
                    "Regex search will linearly scan the full working tree and "
                    "may time out on large repos. Install ripgrep in the image."
                )
            return "grep"
        else:
            raise RuntimeError("Neither ripgrep nor grep found on system")

    def _detect_pcre2_support(self) -> bool:
        """Detect whether ripgrep has PCRE2 support. Result is cached."""
        if self._pcre2_supported is not None:
            return self._pcre2_supported
        try:
            result = subprocess.run(
                ["rg", "--pcre2-version"],
                capture_output=True,
                text=True,
                timeout=_PCRE2_CHECK_TIMEOUT_SEC,
            )
            self._pcre2_supported = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self._pcre2_supported = False
        return self._pcre2_supported

    async def search(
        self,
        pattern: str,
        path: Optional[str] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        case_sensitive: bool = True,
        context_lines: int = 0,
        max_results: int = 100,
        timeout_seconds: Optional[int] = None,
        multiline: bool = False,
        pcre2: bool = False,
    ) -> RegexSearchResult:
        """Execute regex search and return structured results.

        Args:
            pattern: Regular expression pattern to search for
            path: Subdirectory to search within (relative to repo root)
            include_patterns: Glob patterns for files to include
            exclude_patterns: Glob patterns for files to exclude
            case_sensitive: Whether search is case-sensitive
            context_lines: Number of context lines before/after match
            max_results: Maximum number of matches to return
            timeout_seconds: Maximum execution time in seconds (optional)
            multiline: Enable multi-line matching (pattern spans lines)
            pcre2: Enable PCRE2 engine for lookahead/lookbehind

        Returns:
            RegexSearchResult with matches and metadata

        Raises:
            ValueError: If path doesn't exist or PCRE2 unavailable
            TimeoutError: If search exceeds timeout_seconds
        """
        # Story #4 AC2: Regex metrics tracked at MCP handler layer with
        # correct username attribution (_legacy.py:245). Removed duplicate
        # increment_regex_search() call here that caused _anonymous attribution.

        if pcre2 and not self._detect_pcre2_support():
            raise ValueError(
                "PCRE2 not available. Install libpcre2 and ensure ripgrep "
                "is built with PCRE2 support (rg --pcre2-version)."
            )

        start_time = time.time()

        search_path = self.repo_path / path if path else self.repo_path
        if not search_path.exists():
            raise ValueError(f"Path does not exist: {path}")

        if self._search_engine == "ripgrep":
            # Index-assisted pre-filter: when a trigram index is present, narrow
            # the scan to files that could match instead of walking the whole
            # (NFS-backed) working tree. Returns None to fall back to a full
            # scan; an empty list means no file can match.
            candidate_files = self._prefilter_candidate_files(
                pattern, search_path, path, case_sensitive
            )
            if candidate_files is not None and not candidate_files:
                matches, total = [], 0
            else:
                matches, total = await self._search_ripgrep(
                    pattern,
                    search_path,
                    include_patterns,
                    exclude_patterns,
                    case_sensitive,
                    context_lines,
                    max_results,
                    timeout_seconds,
                    multiline=multiline,
                    pcre2=pcre2,
                    candidate_files=candidate_files,
                )
        else:
            matches, total = await self._search_grep(
                pattern,
                search_path,
                include_patterns,
                exclude_patterns,
                case_sensitive,
                context_lines,
                max_results,
                timeout_seconds,
                multiline=multiline,
                pcre2=pcre2,
            )

        elapsed_ms = (time.time() - start_time) * 1000
        return RegexSearchResult(
            matches=matches,
            total_matches=total,
            truncated=total > max_results,
            search_engine=self._search_engine,
            search_time_ms=elapsed_ms,
        )

    def _extract_line_text(self, lines_data: dict) -> str:
        """Extract text content from ripgrep JSON lines data.

        Ripgrep uses two formats for line content:
        - {"text": "..."} for valid UTF-8 content
        - {"bytes": "..."} for binary/non-UTF8 (base64-encoded)
        """
        if "text" in lines_data:
            return str(lines_data["text"]).rstrip("\n")
        elif "bytes" in lines_data:
            import base64

            return (
                base64.b64decode(lines_data["bytes"])
                .decode("utf-8", errors="replace")
                .rstrip("\n")
            )
        return ""

    def _to_repo_relative(self, raw_path: str) -> Optional[str]:
        """Convert a subprocess-reported path into a repo-relative string.

        Bug #1401: this is the single shared containment-check policy used
        by every output-parsing site in this service (ripgrep JSON, grep-mode,
        Python-multiline fallback), checked against the canonical
        ``self.repo_path`` set once at construction.

        - Absolute paths are compared directly against the canonical root.
        - Relative paths are joined onto the canonical root first -- being
          relative is NOT a free pass; a ``../`` escape or an internal
          symlink that resolves outside the repo is rejected the same way
          an absolute-outside-repo path is ("genuinely relative" means
          "relative AND contained", not "relative, therefore trust it").

        Returns None (and logs a warning) if the path does not resolve to
        somewhere inside the repository root; callers must drop that match
        rather than ever storing an absolute/escaping path as ``file_path``.
        """
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.repo_path / candidate
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(self.repo_path)
        except ValueError:
            logger.warning(
                "regex search match path %r resolves outside repository root "
                "%s; dropping match to avoid an incorrect absolute file_path",
                raw_path,
                self.repo_path,
            )
            return None
        return str(rel)

    def _parse_ripgrep_json_output(
        self,
        output: str,
        max_results: int,
        context_lines: int,
    ) -> tuple:
        """Parse ripgrep JSON output into RegexMatch objects.

        Args:
            output: JSON output from ripgrep command
            max_results: Maximum number of matches to return
            context_lines: Number of context lines (used for context parsing)

        Returns:
            Tuple of (matches list, total count)
        """
        matches: List[RegexMatch] = []
        total = 0
        context_before: List[str] = []

        for line in output.splitlines():
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    match_data = data["data"]
                    raw_path = self._extract_line_text(match_data["path"])
                    rel_path = self._to_repo_relative(raw_path)
                    if rel_path is None:
                        # Bug #1401: never silently store an absolute/escaping
                        # path as file_path -- drop the match (already warned).
                        continue
                    total += 1
                    if len(matches) < max_results:
                        submatches = match_data.get("submatches", [])
                        column = submatches[0]["start"] + 1 if submatches else 1

                        matches.append(
                            RegexMatch(
                                file_path=rel_path,
                                line_number=match_data["line_number"],
                                column=column,
                                line_content=self._extract_line_text(
                                    match_data["lines"]
                                ),
                                context_before=context_before.copy(),
                                context_after=[],
                            )
                        )
                        context_before = []
                elif data.get("type") == "context" and context_lines > 0:
                    ctx = self._extract_line_text(data["data"]["lines"])
                    if (
                        matches
                        and data["data"]["line_number"] > matches[-1].line_number
                    ):
                        matches[-1].context_after.append(ctx)
                    else:
                        context_before.append(ctx)
            except json.JSONDecodeError:
                logger.debug(
                    f"Skipping non-JSON line from ripgrep output: {line[:100]}"
                )
                continue

        return matches, total

    def _prefilter_candidate_files(
        self,
        pattern: str,
        search_path: Path,
        path: Optional[str],
        case_sensitive: bool,
    ) -> Optional[List[Path]]:
        """Return candidate file paths from the trigram index, or None.

        None means "no usable pre-filter -> scan the whole ``search_path``". A
        (possibly empty) list means the scan can be restricted to exactly those
        files -- a guaranteed superset of matches (see :mod:`trigram_index_manager`).
        Any failure degrades to None (full scan); the pre-filter never narrows
        unsafely.
        """
        try:
            from .regex_trigram import extract_required_trigrams
            from .trigram_index_manager import TrigramIndexManager

            index = TrigramIndexManager(
                self.repo_path / ".code-indexer" / "trigram_index"
            )
            if not index.exists():
                # No compatible index (missing or stale/old-format). Self-heal in
                # the background so later searches are pre-filtered; this one
                # full-scans.
                _maybe_trigger_lazy_index_build(self.repo_path)
                return None
            required = extract_required_trigrams(
                pattern, case_insensitive=not case_sensitive
            )
            if not required:
                return None
            rel_candidates = index.query(required)
            if rel_candidates is None:
                return None
            if len(rel_candidates) > _MAX_PREFILTER_CANDIDATES:
                # Too many candidates: the arg list would be huge and the filter
                # is not selective -- a full scan is simpler and comparable.
                return None
            scope = search_path.resolve()
            abs_paths: List[Path] = []
            for rel in rel_candidates:
                ap = (self.repo_path / rel).resolve()
                try:
                    ap.relative_to(scope)  # keep only files under the scan scope
                except ValueError:
                    continue
                abs_paths.append(ap)
            return abs_paths
        except Exception as exc:  # never let the optimization break search
            logger.debug("trigram pre-filter unavailable (%s); full scan", exc)
            return None

    async def _search_ripgrep(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        context_lines: int,
        max_results: int,
        timeout_seconds: Optional[int],
        multiline: bool = False,
        pcre2: bool = False,
        candidate_files: Optional[List[Path]] = None,
    ) -> tuple:
        """Search using ripgrep with JSON output and timeout protection.

        When ``candidate_files`` is provided, ripgrep searches exactly those
        files (the trigram pre-filter's superset) instead of walking
        ``search_path``; include/exclude globs still apply.
        """
        cmd = ["rg", "--json", "-e", pattern]

        if multiline:
            cmd.extend(["--multiline", "--multiline-dotall"])
        if pcre2:
            cmd.append("--pcre2")

        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

        if include_patterns:
            for pat in include_patterns:
                cmd.extend(["-g", pat])
        if exclude_patterns:
            for pat in exclude_patterns:
                cmd.extend(["-g", f"!{pat}"])

        # Always exclude CIDX internal directories (Bug #158)
        cmd.extend(["-g", "!.code-indexer/**"])
        cmd.extend(["-g", "!.git/**"])

        if candidate_files is not None:
            # Trigram pre-filter narrowed the search to specific files; ripgrep
            # searches those directly (gitignore is irrelevant for explicit paths,
            # and the candidates already came from a gitignore-aware index).
            cmd.append("--")
            cmd.extend(str(f) for f in candidate_files)
        else:
            cmd.extend(["--", str(search_path)])

        # Create temp file for output
        temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="rg_search_")
        os.close(temp_fd)

        try:
            # Execute with SubprocessExecutor for async + timeout protection
            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS,
                    output_file_path=temp_path,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"Search timed out after {result.timeout_seconds} seconds "
                        f"(pattern='{pattern}', path='{search_path}')"
                    )

                if result.status == ExecutionStatus.ERROR:
                    # Bug #173: Differentiate exit code 1 (no matches) from actual errors
                    if result.exit_code == 1 and not result.stderr_output:
                        # Exit code 1 with no stderr = no matches found (normal ripgrep behavior)
                        logger.debug("ripgrep found no matches (exit code 1)")
                        return [], 0
                    else:
                        # Exit code 2+ or stderr present = actual error (Finding 3.1, v10.4.4)
                        stderr = result.stderr_output or result.error_message or ""
                        raise RipgrepExecutionError(
                            f"ripgrep failed: exit_code={result.exit_code}, stderr={stderr}"
                        )

                # Read output from temp file
                with open(temp_path, "r") as f:
                    output = f.read()

            finally:
                executor.shutdown(wait=True)
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return self._parse_ripgrep_json_output(output, max_results, context_lines)

    async def _find_files_by_patterns(
        self,
        search_path: Path,
        include_patterns: List[str],
        exclude_patterns: Optional[List[str]],
        timeout_seconds: int,
    ) -> List[str]:
        """Use subprocess-based glob script with timeout and process isolation.

        Supports the following pattern types to match ripgrep's -g flag behavior:
        - "**/file.java" - Recursive search from search_path
        - "code/**/file.java" - Recursive search from search_path/code
        - "code/src/file.java" - Explicit path (non-recursive)
        - "*.java" - Simple pattern (recursive from search_path)

        Args:
            search_path: Base directory to search from. All patterns resolved relative to this path.
            include_patterns: List of glob patterns following ripgrep -g flag syntax.
            exclude_patterns: Optional list of patterns to exclude from results.
            timeout_seconds: Maximum time to spend searching (enforced via subprocess timeout).

        Returns:
            List of relative file paths as strings for all files matching include patterns
            and not matching exclude patterns. Empty list if no matches found.

        Raises:
            ValueError: If search_path doesn't exist.
            TimeoutError: If file discovery exceeds timeout_seconds.
        """
        # Validate search path exists
        if not search_path.exists():
            raise ValueError(f"Search path does not exist: {search_path}")

        # Create temp files for config and output
        config_fd, config_path = tempfile.mkstemp(suffix=".json", prefix="glob_config_")
        output_fd, output_path = tempfile.mkstemp(suffix=".json", prefix="glob_output_")
        os.close(config_fd)
        os.close(output_fd)

        try:
            # Write glob config to temp file
            config = {
                "search_path": str(search_path),
                "include_patterns": include_patterns,
                "exclude_patterns": exclude_patterns,
            }
            with open(config_path, "w") as f:
                json.dump(config, f)

            # Get path to glob_files.py script (in scripts/ directory)
            # Path from src/code_indexer/global_repos/regex_search.py -> project_root/scripts/glob_files.py
            script_path = (
                Path(__file__).parent.parent.parent.parent / "scripts" / "glob_files.py"
            )
            if not script_path.exists():
                raise RuntimeError(f"glob_files.py script not found at {script_path}")

            # Execute glob script with subprocess executor for timeout + async protection
            cmd = ["python3", str(script_path), config_path]

            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout_seconds,
                    output_file_path=output_path,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"File discovery timed out after {timeout_seconds} seconds"
                    )

                if result.status == ExecutionStatus.ERROR:
                    logger.warning(f"glob_files.py failed: {result.error_message}")
                    # Return empty list on error (graceful degradation)
                    return []

                # Read and parse JSON output
                with open(output_path, "r") as f:
                    output = f.read().strip()
                    if not output:
                        return []

                    try:
                        files = json.loads(output)
                        if not isinstance(files, list):
                            logger.warning(
                                f"glob_files.py returned non-list: {type(files)}"
                            )
                            return []
                        return files
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse glob output as JSON: {e}")
                        return []

            finally:
                executor.shutdown(wait=True)

        finally:
            # Clean up temp files
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(output_path):
                os.remove(output_path)

    def _build_grep_command(
        self,
        pattern: str,
        case_sensitive: bool,
        context_lines: int,
        recursive: bool,
        file_list: Optional[List[str]] = None,
    ) -> List[str]:
        """Build grep command with common flags.

        Always includes -H flag to force filename output even when searching
        a single file. This ensures consistent parsing of grep output where
        the regex expects 'filename:line:content' format.
        """
        cmd = ["grep", "-E", "-H"]
        if recursive:
            cmd.append("-rn")
        else:
            cmd.append("-n")
        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        cmd.append(pattern)
        if file_list:
            cmd.extend(file_list)
        return cmd

    def _parse_grep_output(
        self,
        output: str,
        max_results: int,
        context_lines: int,
    ) -> tuple:
        """Parse grep output into RegexMatch objects with context lines.

        Handles grep's output format with:
        - Match lines: filename:linenum:content (colon separators)
        - Context lines: filename-linenum-content (dash separators)
        - Group separators: -- (between match groups)

        The -- separator indicates separate context groups. When encountered,
        any subsequent context lines belong to the next match (context_before),
        not the previous match (context_after).

        When matches are adjacent and context overlaps, grep omits the --
        separator. In this case, we limit context_after to context_lines count
        to prevent over-collection.

        Args:
            output: Raw grep output text
            max_results: Maximum number of matches to return
            context_lines: Number of context lines requested (for limiting context_after)

        Returns:
            Tuple of (matches list, total count)
        """
        matches: List[RegexMatch] = []
        total = 0
        context_before: List[str] = []
        collecting_context_after = False

        for line in output.splitlines():
            # Group separator indicates end of current match's context group
            if line.strip() == "--":
                collecting_context_after = False
                context_before = []
                continue

            # Try matching as match line (colon separator)
            match_line = re.match(r"^(.+?):(\d+):(.*)$", line)
            if match_line:
                file_path = match_line.group(1)
                rel_path = self._to_repo_relative(file_path)
                if rel_path is None:
                    # Bug #1401: never silently store an absolute/escaping
                    # path as file_path -- drop the match (already warned).
                    # Grep-mode legitimately reports relative filenames in
                    # some cases; those are still accepted here since
                    # _to_repo_relative only rejects genuine escapes.
                    continue
                total += 1
                if len(matches) < max_results:
                    # Parse line number with error handling
                    try:
                        line_num = int(match_line.group(2))
                    except ValueError:
                        logger.warning(f"Invalid line number in grep output: {line}")
                        continue

                    matches.append(
                        RegexMatch(
                            file_path=rel_path,
                            line_number=line_num,
                            column=1,
                            line_content=match_line.group(3),
                            context_before=context_before.copy(),
                            context_after=[],
                        )
                    )
                    context_before = []
                    collecting_context_after = True
                continue

            # Try matching as context line (dash separator)
            context_line = re.match(r"^(.+?)-(\d+)-(.*)$", line)
            if context_line:
                line_content = context_line.group(3)

                # Determine if this is context_before or context_after
                if collecting_context_after and matches:
                    # Check if we've already collected enough context_after lines
                    if len(matches[-1].context_after) < context_lines:
                        # This is context AFTER the last match
                        matches[-1].context_after.append(line_content)
                    else:
                        # We've collected enough context_after, this is context_before for next match
                        context_before.append(line_content)
                        collecting_context_after = False
                else:
                    # This is context BEFORE the next match
                    context_before.append(line_content)

        return matches, total

    def _search_python_multiline(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        max_results: int,
    ) -> tuple:
        """Python re.DOTALL search for multiline patterns.

        Used when multiline=True on the grep engine path, or as a fallback
        when ripgrep is unavailable.
        """
        from fnmatch import fnmatch

        flags = re.DOTALL
        if not case_sensitive:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        matches: List[RegexMatch] = []
        total = 0

        for root, _dirs, files in os.walk(search_path):
            # Skip internal directories
            rel_root = os.path.relpath(root, search_path)
            if rel_root.startswith(".code-indexer") or rel_root.startswith(".git"):
                continue

            for fname in files:
                file_path = os.path.join(root, fname)
                rel_path = self._to_repo_relative(file_path)
                if rel_path is None:
                    # Bug #1401: never silently store an absolute/escaping
                    # path as file_path -- skip the file (already warned).
                    continue

                if include_patterns and not any(
                    fnmatch(fname, p) for p in include_patterns
                ):
                    continue
                if exclude_patterns and any(
                    fnmatch(fname, p) for p in exclude_patterns
                ):
                    continue

                try:
                    with open(file_path, "r", errors="replace") as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    logger.debug("Skipping unreadable file: %s", file_path)
                    continue

                for m in compiled.finditer(content):
                    total += 1
                    if len(matches) < max_results:
                        start_line = content[: m.start()].count("\n") + 1
                        col = m.start() - content.rfind("\n", 0, m.start())
                        matches.append(
                            RegexMatch(
                                file_path=rel_path,
                                line_number=start_line,
                                column=col,
                                line_content=m.group(0),
                                context_before=[],
                                context_after=[],
                            )
                        )

        return matches, total

    async def _search_grep(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        context_lines: int,
        max_results: int,
        timeout_seconds: Optional[int],
        multiline: bool = False,
        pcre2: bool = False,
    ) -> tuple:
        """Fallback search using grep with timeout protection."""
        # For multiline searches, use Python re.DOTALL fallback
        if multiline:
            return self._search_python_multiline(
                pattern,
                search_path,
                include_patterns,
                exclude_patterns,
                case_sensitive,
                max_results,
            )

        timeout = timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS
        has_path_patterns = include_patterns and any(
            "/" in pat for pat in include_patterns
        )

        if has_path_patterns:
            # Use find to get files matching all patterns (both path and simple)
            # Type assertion: include_patterns is not None here (checked by has_path_patterns)
            assert include_patterns is not None
            file_list = await self._find_files_by_patterns(
                search_path, include_patterns, exclude_patterns, timeout
            )
            if not file_list:
                return [], 0

            cmd = self._build_grep_command(
                pattern, case_sensitive, context_lines, False, file_list
            )
        else:
            # Original behavior: recursive grep with --include/--exclude
            cmd = self._build_grep_command(pattern, case_sensitive, context_lines, True)
            if include_patterns:
                for pat in include_patterns:
                    cmd.extend(["--include", pat])
            if exclude_patterns:
                for pat in exclude_patterns:
                    cmd.extend(["--exclude", pat])
            # Always exclude CIDX internal directories (Bug #158)
            cmd.extend(["--exclude-dir", ".code-indexer"])
            cmd.extend(["--exclude-dir", ".git"])
            cmd.append(str(search_path))

        # Create temp file for output
        temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="grep_search_")
        os.close(temp_fd)

        try:
            # Execute with SubprocessExecutor for async + timeout protection
            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout,
                    output_file_path=temp_path,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"Search timed out after {result.timeout_seconds} seconds "
                        f"(pattern='{pattern}', path='{search_path}')"
                    )

                if result.status == ExecutionStatus.ERROR:
                    # Bug #173: Differentiate exit code 1 (no matches) from actual errors
                    if result.exit_code == 1 and not result.stderr_output:
                        # Exit code 1 with no stderr = no matches found (normal grep behavior)
                        logger.debug("grep found no matches (exit code 1)")
                        return [], 0
                    else:
                        # Exit code 2+ or stderr present = actual error (Finding 3.1, v10.4.4)
                        stderr = result.stderr_output or result.error_message or ""
                        raise RipgrepExecutionError(
                            f"grep failed: exit_code={result.exit_code}, stderr={stderr}"
                        )

                # Read output from temp file
                with open(temp_path, "r") as f:
                    output = f.read()

            finally:
                executor.shutdown(wait=True)
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # Parse grep output with context line support
        matches, total = self._parse_grep_output(output, max_results, context_lines)
        return matches, total
