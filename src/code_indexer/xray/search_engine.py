"""XRaySearchEngine: orchestrates two-phase X-Ray search.

Phase 1 (driver): regex-based candidate file selection via file walk + re.search.
Phase 2 (evaluator): AST-based per-match evaluation via PythonEvaluatorSandbox,
executed in parallel via ThreadPoolExecutor with wall-clock timeout enforcement.

Story #978 adds ThreadPoolExecutor parallelism and COMPLETED_PARTIAL contract.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.global_repos.regex_search import RegexSearchService


def _line_to_byte_offset(source: str, line_number: int) -> int:
    """Convert a 1-indexed line number to the byte offset of that line's start.

    Args:
        source: The full source code string (UTF-8 assumed).
        line_number: 1-indexed line number.

    Returns:
        Byte offset of the start of the given line.  Returns 0 for line_number
        <= 1.  Returns len(source) for line_number beyond the last line.
    """
    if line_number <= 1:
        return 0
    lines = source.split("\n")
    if line_number > len(lines):
        return len(source)
    return sum(len(line) + 1 for line in lines[: line_number - 1])


def _run_async_in_sync(coro: Any) -> Any:
    """Run an async coroutine from a synchronous context.

    Handles the case where an event loop is already running (e.g. FastAPI
    handler thread) by spawning a new thread with its own event loop.

    Args:
        coro: Awaitable coroutine to execute.

    Returns:
        The result of the coroutine.
    """
    try:
        asyncio.get_running_loop()
        # Already inside a running loop — use a fresh thread+loop.
        result: Any = None
        exc: Optional[BaseException] = None

        def _run() -> None:
            nonlocal result, exc
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(coro)
            except Exception as e:  # noqa: BLE001
                exc = e
            finally:
                loop.close()

        t = threading.Thread(target=_run)
        t.start()
        t.join()
        if exc is not None:
            raise exc
        return result
    except RuntimeError:
        # No running loop — use asyncio.run() directly.
        return asyncio.run(coro)


class XRaySearchEngine:
    """Orchestrates two-phase X-Ray search over a repository directory.

    Phase 1 (driver): regex match over file content or file paths to build a
    candidate list.

    Phase 2 (evaluator): for each candidate file, parse it with tree-sitter and
    run the caller-supplied Python evaluator code in the sandbox.

    Story #978 will add ThreadPoolExecutor parallelism and job-level timeout.
    """

    def __init__(self) -> None:
        """Initialise the engine, importing tree-sitter at this point."""
        from code_indexer.xray.ast_engine import AstSearchEngine
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        self.ast_engine = AstSearchEngine()
        self.sandbox = PythonEvaluatorSandbox()

    @staticmethod
    def _serialize_ast(node: Any, max_nodes: int) -> Dict[str, Any]:
        """Serialize an AST node to a JSON-serializable dict via BFS.

        Walks the tree breadth-first up to ``max_nodes`` total nodes. When the
        cap is hit, a ``{"type": "...truncated"}`` sentinel is appended to the
        children list of the parent being processed.

        Args:
            node: tree-sitter Node (or XRayNode-wrapped) to serialize.
            max_nodes: Maximum number of real nodes to include.

        Returns:
            Dict with keys: type, start_byte, end_byte, start_point, end_point,
            text_preview, child_count, children.
        """
        raw = getattr(node, "_node", node)
        text_bytes: bytes = raw.text if raw.text is not None else b""
        root_dict: Dict[str, Any] = {
            "type": raw.type,
            "start_byte": raw.start_byte,
            "end_byte": raw.end_byte,
            "start_point": [raw.start_point[0], raw.start_point[1]],
            "end_point": [raw.end_point[0], raw.end_point[1]],
            "text_preview": text_bytes.decode("utf-8", errors="replace")[:80],
            "child_count": raw.child_count,
            "children": [],
        }
        queue: List[tuple] = [(root_dict, raw)]
        visited = 1
        while queue:
            parent_dict, parent_raw = queue.pop(0)
            if visited >= max_nodes:
                # Cap reached — emit truncated sentinel if node has children.
                if parent_raw.child_count > 0:
                    parent_dict["children"].append({"type": "...truncated"})
                continue
            for child_raw in parent_raw.children:
                if visited >= max_nodes:
                    parent_dict["children"].append({"type": "...truncated"})
                    break
                child_text: bytes = (
                    child_raw.text if child_raw.text is not None else b""
                )
                child_dict: Dict[str, Any] = {
                    "type": child_raw.type,
                    "start_byte": child_raw.start_byte,
                    "end_byte": child_raw.end_byte,
                    "start_point": [
                        child_raw.start_point[0],
                        child_raw.start_point[1],
                    ],
                    "end_point": [
                        child_raw.end_point[0],
                        child_raw.end_point[1],
                    ],
                    "text_preview": child_text.decode("utf-8", errors="replace")[:80],
                    "child_count": child_raw.child_count,
                    "children": [],
                }
                parent_dict["children"].append(child_dict)
                queue.append((child_dict, child_raw))
                visited += 1
        return root_dict

    def run(
        self,
        *,
        repo_path: Path,
        driver_regex: str,
        evaluator_code: str,
        search_target: str,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        timeout_seconds: int = 120,
        worker_threads: int = 2,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
        max_files: Optional[int] = None,
        include_ast_debug: bool = False,
        max_debug_nodes: int = 50,
    ) -> Dict[str, Any]:
        """Run two-phase X-Ray search and return the job-result dict.

        Args:
            repo_path: Root directory of the repository to search.
            driver_regex: Regular expression applied in Phase 1.
            evaluator_code: Python expression evaluated per candidate file.
            search_target: ``"content"`` or ``"filename"``.
            include_patterns: Glob patterns; only matching files are considered.
            exclude_patterns: Glob patterns; matching files are excluded.
            timeout_seconds: Wall-clock cap (unused in #972; #978 enforces it).
            worker_threads: Thread-pool size (unused in #972; #978 uses it).
            progress_callback: Called with ``(percent, phase_name, phase_detail)``
                at key milestones.
            max_files: Maximum number of candidate files to evaluate. When the
                cap is reached the result includes ``partial=True`` and
                ``max_files_reached=True``.
            include_ast_debug: When ``True``, each accepted match gains an
                ``ast_debug`` field containing a BFS-serialised AST tree rooted
                at the file's root node.  Adds ~1-5 ms per match.
            max_debug_nodes: Maximum number of AST nodes to include in each
                ``ast_debug`` payload.  Capped by a ``{"type": "...truncated"}``
                sentinel.  Range 1..500; default 50.

        Returns:
            Dictionary with keys: matches, evaluation_errors, files_processed,
            files_total, elapsed_seconds, and optionally partial/max_files_reached.
        """
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if worker_threads <= 0:
            raise ValueError(f"worker_threads must be > 0, got {worker_threads}")
        if max_files is not None and max_files <= 0:
            raise ValueError(f"max_files must be > 0 when provided, got {max_files}")

        start = time.monotonic()
        include_patterns = include_patterns or []
        exclude_patterns = exclude_patterns or []

        if progress_callback:
            progress_callback(0, "phase1_driver", "regex driver scan")

        candidate_files = self._run_phase1_driver(
            repo_path, driver_regex, search_target, include_patterns, exclude_patterns
        )

        files_total = len(candidate_files)
        cap_hit = False

        if max_files is not None and len(candidate_files) > max_files:
            candidate_files = candidate_files[:max_files]
            cap_hit = True

        if progress_callback:
            progress_callback(
                50, "phase2_evaluator", f"evaluating {len(candidate_files)} files"
            )

        matches: List[Dict[str, Any]] = []
        evaluation_errors: List[Dict[str, Any]] = []
        files_processed = 0
        timeout_hit = False

        def _elapsed() -> float:
            return time.monotonic() - start

        def _timed_out() -> bool:
            return _elapsed() > timeout_seconds

        with ThreadPoolExecutor(max_workers=worker_threads) as pool:
            pending: Dict[Future, Path] = {
                pool.submit(
                    self._evaluate_file,
                    fp,
                    evaluator_code,
                    include_ast_debug,
                    max_debug_nodes,
                    self._last_phase1_positions.get(fp),
                ): fp
                for fp in candidate_files
            }

            while pending:
                if _timed_out():
                    timeout_hit = True
                    for fut in list(pending):
                        fut.cancel()
                    break

                # Poll for the next completion with a short timeout so we can
                # re-check wall-clock between polls without blocking forever.
                done, _ = wait(
                    list(pending.keys()), timeout=0.5, return_when=FIRST_COMPLETED
                )

                for fut in done:
                    fp = pending.pop(fut)
                    try:
                        file_matches, file_errors = fut.result()
                        matches.extend(file_matches)
                        evaluation_errors.extend(file_errors)
                    except Exception as exc:  # noqa: BLE001
                        evaluation_errors.append(
                            {
                                "file_path": str(fp),
                                "line_number": 0,
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                            }
                        )
                    files_processed += 1

                # Re-check timeout immediately after processing completions.
                if _timed_out():
                    timeout_hit = True
                    for fut in list(pending):
                        fut.cancel()
                    break

        elapsed = time.monotonic() - start

        result: Dict[str, Any] = {
            "matches": matches,
            "evaluation_errors": evaluation_errors,
            "files_processed": files_processed,
            "files_total": files_total,
            "elapsed_seconds": elapsed,
        }

        # COMPLETED_PARTIAL contract: timeout takes precedence over max_files cap.
        if timeout_hit:
            result["partial"] = True
            result["timeout"] = True
        elif cap_hit:
            result["partial"] = True
            result["max_files_reached"] = True

        if progress_callback:
            if timeout_hit:
                progress_callback(
                    100,
                    "timeout",
                    f"partial: {files_processed}/{files_total}",
                )
            elif cap_hit:
                progress_callback(
                    100,
                    "max_files_reached",
                    f"partial: {files_processed}/{files_total}",
                )
            else:
                progress_callback(
                    100,
                    "complete",
                    f"matches={len(matches)}, errors={len(evaluation_errors)}",
                )

        return result

    def _evaluate_file(
        self,
        file_path: Path,
        evaluator_code: str,
        include_ast_debug: bool,
        max_debug_nodes: int,
        match_positions: Optional[List[Tuple[int, str]]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Evaluate a candidate file once per Phase 1 match position.

        Parses the file once, then calls sandbox.run once per (line_number,
        line_content) position from Phase 1.  The node passed to the evaluator
        is the deepest AST node enclosing that position's byte offset.

        Called from Phase 2 via ThreadPoolExecutor workers.

        Args:
            file_path: Absolute path to the candidate file.
            evaluator_code: Python expression evaluated by the sandbox.
            include_ast_debug: When True, accepted matches carry ast_debug payload.
            max_debug_nodes: BFS node cap for ast_debug serialisation.
            match_positions: List of (line_number, line_content) tuples from
                Phase 1.  When None (filename-target or legacy call), falls
                back to a single call with the root node (line_number=None).

        Returns:
            Tuple of (matches, errors) where each is a list of dicts.
        """
        from code_indexer.xray.ast_engine import find_enclosing_node

        lang = self.ast_engine.detect_language(file_path)
        if lang is None:
            return [], [
                {
                    "file_path": str(file_path),
                    "line_number": 0,
                    "error_type": "UnsupportedLanguage",
                    "error_message": f"No grammar for extension {file_path.suffix!r}",
                }
            ]

        try:
            source_bytes = file_path.read_bytes()
            source = source_bytes.decode("utf-8", errors="replace")
            root = self.ast_engine.parse(source_bytes, lang)

            # Determine the set of positions to evaluate.
            # For content search: one entry per Phase 1 regex match.
            # For filename search (no positions): one call with root, line_number=None.
            if match_positions:
                positions_to_eval: List[Tuple[Optional[int], Optional[str]]] = [
                    (ln, lc) for ln, lc in match_positions
                ]
            else:
                positions_to_eval = [(None, None)]

            matches: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []

            for line_number, line_content in positions_to_eval:
                # Locate the enclosing AST node for this position.
                if line_number is not None:
                    byte_offset = _line_to_byte_offset(source, line_number)
                    node = find_enclosing_node(root, byte_offset)
                else:
                    node = root

                eval_result = self.sandbox.run(
                    evaluator_code,
                    node=node,
                    root=root,
                    source=source,
                    lang=lang,
                    file_path=str(file_path),
                )

                err_line = line_number if line_number is not None else 0

                if eval_result.failure is None and eval_result.value is True:
                    match_entry: Dict[str, Any] = {
                        "file_path": str(file_path),
                        "line_number": line_number,
                        "code_snippet": line_content,
                        "language": lang,
                        "evaluator_decision": True,
                    }
                    if include_ast_debug:
                        match_entry["ast_debug"] = self._serialize_ast(
                            root, max_debug_nodes
                        )
                    matches.append(match_entry)

                elif eval_result.failure == "evaluator_timeout":
                    errors.append(
                        {
                            "file_path": str(file_path),
                            "line_number": err_line,
                            "error_type": "EvaluatorTimeout",
                            "error_message": "evaluator exceeded 5s sandbox limit",
                        }
                    )

                elif eval_result.failure == "evaluator_subprocess_died":
                    errors.append(
                        {
                            "file_path": str(file_path),
                            "line_number": err_line,
                            "error_type": "EvaluatorCrash",
                            "error_message": eval_result.detail or "Subprocess died",
                        }
                    )

                elif eval_result.failure == "evaluator_returned_non_bool":
                    errors.append(
                        {
                            "file_path": str(file_path),
                            "line_number": err_line,
                            "error_type": "NonBoolReturn",
                            "error_message": (
                                eval_result.detail or "Evaluator did not return bool"
                            ),
                        }
                    )
                # eval_result.value is False (or falsy non-failure) — not a match.

            return matches, errors

        except Exception as exc:  # noqa: BLE001
            return [], [
                {
                    "file_path": str(file_path),
                    "line_number": 0,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            ]

    def _run_phase1_driver(
        self,
        repo_path: Path,
        driver_regex: str,
        search_target: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
    ) -> List[Path]:
        """Phase 1: regex-based candidate file selection.

        For ``search_target="content"``, delegates to ``RegexSearchService``
        (ripgrep-backed) for performance and PCRE2 support.  Per-match line
        positions are stored in ``self._last_phase1_positions`` for use by
        issue #983 without changing the Phase 2 call-sites.

        For ``search_target="filename"``, uses the inline path-match walker
        because ``RegexSearchService`` is a content-only service and has no
        filename-search mode.

        Args:
            repo_path: Root directory to walk.
            driver_regex: Regular expression applied in Phase 1.
            search_target: ``"filename"`` or ``"content"``.
            include_patterns: Glob include filters (empty = include all).
            exclude_patterns: Glob exclude filters (empty = exclude none).

        Returns:
            Ordered, deduplicated list of matching Path objects.
        """
        # Reset per-call side-channel consumed by issue #983.
        self._last_phase1_positions: Dict[Path, List[Tuple[int, str]]] = {}

        if search_target == "filename":
            return self._run_phase1_filename(
                repo_path, driver_regex, include_patterns, exclude_patterns
            )

        return self._run_phase1_content(
            repo_path, driver_regex, include_patterns, exclude_patterns
        )

    def _run_phase1_filename(
        self,
        repo_path: Path,
        driver_regex: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
    ) -> List[Path]:
        """Inline filename-path walker (RegexSearchService has no filename mode)."""
        pattern = re.compile(driver_regex)
        candidates: List[Path] = []

        for p in sorted(repo_path.rglob("*")):
            if not p.is_file():
                continue

            rel = str(p.relative_to(repo_path))

            if include_patterns and not any(
                fnmatch.fnmatch(rel, ip) for ip in include_patterns
            ):
                continue

            if exclude_patterns and any(
                fnmatch.fnmatch(rel, ep) for ep in exclude_patterns
            ):
                continue

            if pattern.search(rel):
                candidates.append(p)

        return candidates

    def _run_phase1_content(
        self,
        repo_path: Path,
        driver_regex: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
    ) -> List[Path]:
        """Content driver via RegexSearchService (ripgrep-backed, async-bridged)."""
        service = RegexSearchService(repo_path)
        search_result = _run_async_in_sync(
            service.search(
                pattern=driver_regex,
                include_patterns=include_patterns or None,
                exclude_patterns=exclude_patterns or None,
                max_results=100_000,
            )
        )

        # Build deduplicated candidate list and populate positions side-channel.
        seen: Dict[Path, bool] = {}
        candidates: List[Path] = []
        for m in search_result.matches:
            abs_path = repo_path / m.file_path
            if abs_path not in seen:
                seen[abs_path] = True
                candidates.append(abs_path)
                self._last_phase1_positions[abs_path] = []
            self._last_phase1_positions[abs_path].append(
                (m.line_number, m.line_content)
            )

        return candidates
