"""XRaySearchEngine: orchestrates two-phase X-Ray search.

Phase 1 (driver): regex-based candidate file selection via file walk + re.search.
Phase 2 (evaluator): AST-based per-match evaluation via PythonEvaluatorSandbox,
executed in parallel via ThreadPoolExecutor with wall-clock timeout enforcement.

Story #978 adds ThreadPoolExecutor parallelism and COMPLETED_PARTIAL contract.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    RipgrepExecutionError,
)
from code_indexer.xray.sandbox import _line_to_byte_offset_bytes as _line_to_byte_offset

logger = logging.getLogger(__name__)


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
        case_sensitive: bool = True,
        context_lines: int = 0,
        multiline: bool = False,
        pcre2: bool = False,
        path: Optional[str] = None,
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
            case_sensitive: Whether Phase 1 regex matching is case-sensitive.
                Passed to RegexSearchService for content searches. Default True.
            context_lines: Number of context lines before/after each match to
                include in match envelopes. Range 0..10; default 0.
            multiline: Enable multi-line regex matching in Phase 1 content
                driver. Passed to RegexSearchService. Default False.
            pcre2: Enable PCRE2 regex engine for advanced features (lookahead,
                lookbehind) in Phase 1 content driver. Default False.
            path: Subdirectory within the repository to restrict the search to.
                Relative to repo root. None means the full repository. Default None.
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

        try:
            candidate_files = self._run_phase1_driver(
                repo_path,
                driver_regex,
                search_target,
                include_patterns,
                exclude_patterns,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                multiline=multiline,
                pcre2=pcre2,
                path=path,
            )
        except RipgrepExecutionError as exc:
            # Finding 3.1 (v10.4.4): surface Phase 1 errors (e.g. invalid regex)
            # instead of completing with silently empty results. Log then return
            # a partial result so the background job shows phase1_failed.
            logger.warning("XRaySearchEngine: Phase 1 driver failed: %s", exc)
            return {
                "matches": [],
                "evaluation_errors": [],
                "files_processed": 0,
                "files_total": 0,
                "elapsed_seconds": time.monotonic() - start,
                "phase1_failed": True,
                "phase1_error": str(exc),
                "partial": True,
            }

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
        file_metadata: List[Dict[str, Any]] = []
        files_processed = 0
        timeout_hit = False

        def _elapsed() -> float:
            return time.monotonic() - start

        def _timed_out() -> bool:
            return _elapsed() > timeout_seconds

        # Build serializable file specs for spawn-driver batch (Bug #994).
        # Language detection stays in parent (no tree-sitter needed — just extension map).
        # Parsing + evaluation moves to the spawned driver process.
        file_specs: List[Dict[str, Any]] = []
        for fp in candidate_files:
            fp_lang = self.ast_engine.detect_language(fp)
            if fp_lang is None:
                evaluation_errors.append(
                    {
                        "file_path": str(fp),
                        "line_number": 0,
                        "error_type": "UnsupportedLanguage",
                        "error_message": f"No grammar for extension {fp.suffix!r}",
                    }
                )
                files_processed += 1
                continue
            try:
                fp_source = fp.read_bytes().decode("utf-8", errors="replace")
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
                continue
            positions = self._last_phase1_positions.get(fp, [])
            clean_positions = [
                {k: v for k, v in p.items() if k != "ast_node"} for p in positions
            ]
            file_specs.append(
                {
                    "file_path": str(fp),
                    "source": fp_source,
                    "lang": fp_lang,
                    "match_positions": clean_positions,
                }
            )

        if file_specs:
            remaining = max(1, timeout_seconds - int(_elapsed()))
            batch_results = self.sandbox.run_batch(
                evaluator_code=evaluator_code,
                file_specs=file_specs,
                worker_threads=worker_threads,
                timeout_seconds=remaining,
            )
            for file_matches, file_errors, file_meta in batch_results:
                if _timed_out():
                    timeout_hit = True
                    break
                matches.extend(file_matches)
                evaluation_errors.extend(file_errors)
                if file_meta is not None:
                    file_metadata.append(file_meta)
                files_processed += 1

        # Enrich matches with ast_debug and matched_node when requested.
        # Re-parses each matched file once; safe since include_ast_debug is a
        # debug/development flag (not a production hot path).
        # matched_node uses the file-level root node (same contract as the
        # original _evaluate_file implementation at lines 694-705).
        if include_ast_debug and matches:
            spec_by_path = {s["file_path"]: s for s in file_specs}
            parsed_roots: Dict[str, Any] = {}
            for match in matches:
                fp_str = match.get("file_path", "")
                if fp_str not in parsed_roots:
                    spec = spec_by_path.get(fp_str)
                    if spec is not None:
                        try:
                            root = self.ast_engine.parse(
                                spec["source"].encode("utf-8"), spec["lang"]
                            )
                            parsed_roots[fp_str] = root
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "ast_debug: failed to parse %s for enrichment: %s",
                                fp_str,
                                exc,
                            )
                            parsed_roots[fp_str] = None
                    else:
                        parsed_roots[fp_str] = None
                root = parsed_roots.get(fp_str)
                if root is not None:
                    raw_root = getattr(root, "_node", root)
                    match["matched_node"] = {
                        "type": raw_root.type,
                        "start_byte": raw_root.start_byte,
                        "end_byte": raw_root.end_byte,
                        "start_point": list(raw_root.start_point),
                        "end_point": list(raw_root.end_point),
                    }
                    match["ast_debug"] = self._serialize_ast(root, max_debug_nodes)

        elapsed = time.monotonic() - start

        result: Dict[str, Any] = {
            "matches": matches,
            "evaluation_errors": evaluation_errors,
            "file_metadata": file_metadata,
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

        # Surface zero-match include_pattern warnings (fix #3).
        phase1_warnings = getattr(self, "_last_phase1_warnings", [])
        if phase1_warnings:
            result["warnings"] = phase1_warnings

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

    # Lower-level single-file evaluation API used directly by unit tests (not dead code).
    def _evaluate_file(
        self,
        file_path: Path,
        evaluator_code: str,
        include_ast_debug: bool,
        max_debug_nodes: int,
        match_positions: Optional[List[Dict[str, Any]]] = None,
        lang: Optional[str] = None,
        source: Optional[str] = None,
        context_lines: int = 0,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Evaluate a candidate file ONCE with the file-as-unit contract (v10.4.0).

        Parses the file once, then calls sandbox.run ONCE with the full
        ``match_positions`` list (all Phase 1 hits for this file).  The
        evaluator receives:
          - ``node`` / ``root``: file root AST node
          - ``source``: raw file text
          - ``lang``: language string
          - ``file_path``: absolute file path
          - ``match_positions``: list of dicts, one per Phase 1 hit:
              {line_number, line_content, column, byte_offset,
               context_before, context_after}
            Empty list in filename-target mode.

        Evaluator MUST return a dict with shape:
          ``{"matches": [{"line_number": int, ...}, ...], "value": <any>}``

        Optional evaluator extensions (ignored if absent):
          - ``"skip": True`` — signals the evaluator wants this file skipped
            entirely; returns ([], [], None) immediately.
          - ``"file_role": str`` — per-file tag surfaced in file_metadata.

        Server enriches each match with server-provided fields:
          - ``file_path`` (always — evaluator sees one file)
          - ``language`` (always)
          - ``line_content`` (derived from source if evaluator omits it)

        Args:
            lang: Language string override.  When provided, bypasses
                ``ast_engine.detect_language``; used by unit tests and callers
                that already know the language.
            source: Source text override.  When provided, bypasses file I/O;
                the file is still used for ``file_path`` in results.
            context_lines: Reserved for future context expansion; accepted but
                not used at this layer (context is already embedded in
                ``match_positions`` by Phase 1).

        Returns:
            Tuple of (matches, errors, file_meta_or_none) where:
            - matches: list of enriched match dicts
            - errors: list of error dicts
            - file_meta_or_none: {"file_path": ..., "value": ...} or None
        """
        # Allow callers to supply lang/source directly (e.g. unit tests).
        if lang is None:
            lang = self.ast_engine.detect_language(file_path)
        if lang is None:
            return (
                [],
                [
                    {
                        "file_path": str(file_path),
                        "line_number": 0,
                        "error_type": "UnsupportedLanguage",
                        "error_message": f"No grammar for extension {file_path.suffix!r}",
                    }
                ],
                None,
            )

        try:
            if source is None:
                source_bytes = file_path.read_bytes()
                source = source_bytes.decode("utf-8", errors="replace")
            else:
                source_bytes = source.encode("utf-8")
            root = self.ast_engine.parse(source_bytes, lang)

            # Normalize match_positions to list of dicts (file-as-unit contract).
            # In filename-target mode, match_positions is None → empty list.
            positions: List[Dict[str, Any]] = match_positions if match_positions else []

            # Enrich each position with byte_offset derived from source.
            # _run_phase1_content stored 0 as a sentinel; re-derive here.
            for pos in positions:
                ln = pos.get("line_number", 1) or 1
                pos["byte_offset"] = _line_to_byte_offset(source, ln)

            # Enrich each position with ast_node (smallest named AST node at byte_offset).
            for pos in positions:
                byte_off = pos.get("byte_offset")
                if byte_off is not None:
                    pos["ast_node"] = root.node_at_byte_offset(byte_off)
                else:
                    pos["ast_node"] = None

            # Call evaluator ONCE per file with the full positions list.
            eval_result = self.sandbox.run(
                evaluator_code,
                node=root,
                root=root,
                source=source,
                lang=lang,
                file_path=str(file_path),
                match_positions=positions,
            )

            if eval_result.failure == "evaluator_timeout":
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "EvaluatorTimeout",
                            "error_message": "evaluator exceeded 5s sandbox limit",
                        }
                    ],
                    None,
                )

            if eval_result.failure == "evaluator_subprocess_died":
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "EvaluatorCrash",
                            "error_message": eval_result.detail or "Subprocess died",
                        }
                    ],
                    None,
                )

            if eval_result.failure == "validation_failed":
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "ValidationFailed",
                            "error_message": eval_result.detail
                            or "Evaluator validation failed",
                        }
                    ],
                    None,
                )

            if eval_result.failure is not None:
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "EvaluatorCrash",
                            "error_message": eval_result.detail or eval_result.failure,
                        }
                    ],
                    None,
                )

            # Validate dict return contract.
            raw_value = eval_result.value
            if not isinstance(raw_value, dict):
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "InvalidEvaluatorReturn",
                            "error_message": (
                                f"Evaluator must return a dict "
                                f'{{"matches": [...], "value": ...}}, '
                                f"got {type(raw_value).__name__!r}. "
                                f"Note: bool return (legacy contract) is no longer accepted."
                            ),
                        }
                    ],
                    None,
                )

            # Early bail-out: evaluator signals "skip this file".
            if raw_value.get("skip") is True:
                return ([], [], None)

            if "matches" not in raw_value:
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "InvalidEvaluatorReturn",
                            "error_message": (
                                "Evaluator dict missing required 'matches' key. "
                                'Return: {"matches": [...], "value": ...}'
                            ),
                        }
                    ],
                    None,
                )

            evaluator_matches = raw_value["matches"]
            per_file_value = raw_value.get("value", None)

            if not isinstance(evaluator_matches, list):
                return (
                    [],
                    [
                        {
                            "file_path": str(file_path),
                            "line_number": 0,
                            "error_type": "InvalidEvaluatorReturn",
                            "error_message": (
                                f"'matches' must be a list, got {type(evaluator_matches).__name__!r}"
                            ),
                        }
                    ],
                    None,
                )

            # Build source lines once for line_content derivation.
            source_lines = source.splitlines()

            matches: List[Dict[str, Any]] = []
            for em in evaluator_matches:
                if not isinstance(em, dict):
                    continue  # Skip malformed entries silently

                # Finding 3.3: line_number is required in every match dict.
                if "line_number" not in em:
                    return (
                        [],
                        [
                            {
                                "file_path": str(file_path),
                                "line_number": 0,
                                "error_type": "InvalidEvaluatorReturn",
                                "error_message": (
                                    "each match must contain 'line_number'; "
                                    f"got keys: {list(em.keys())!r}"
                                ),
                            }
                        ],
                        None,
                    )

                match_entry: Dict[str, Any] = dict(em)  # copy evaluator fields

                # Finding 3.4: coerce line_number to int; non-numeric → InvalidEvaluatorReturn.
                ln_raw = match_entry["line_number"]
                try:
                    ln = int(ln_raw)
                except (TypeError, ValueError):
                    return (
                        [],
                        [
                            {
                                "file_path": str(file_path),
                                "line_number": 0,
                                "error_type": "InvalidEvaluatorReturn",
                                "error_message": (
                                    f"line_number must be an int, got {ln_raw!r}"
                                ),
                            }
                        ],
                        None,
                    )
                match_entry["line_number"] = ln

                # Server always provides file_path and language.
                match_entry["file_path"] = str(file_path)
                match_entry["language"] = lang

                # Server derives line_content from source if evaluator omits it.
                if "line_content" not in match_entry:
                    idx = ln - 1  # 1-based → 0-based
                    if 0 <= idx < len(source_lines):
                        match_entry["line_content"] = source_lines[idx]
                    else:
                        match_entry["line_content"] = ""

                if include_ast_debug:
                    raw_root = getattr(root, "_node", root)
                    match_entry["matched_node"] = {
                        "type": raw_root.type,
                        "start_byte": raw_root.start_byte,
                        "end_byte": raw_root.end_byte,
                        "start_point": list(raw_root.start_point),
                        "end_point": list(raw_root.end_point),
                    }
                    match_entry["ast_debug"] = self._serialize_ast(
                        root, max_debug_nodes
                    )

                matches.append(match_entry)

            # Build file_metadata entry (value and/or file_role, when present).
            per_file_role = raw_value.get("file_role", None)
            file_meta: Optional[Dict[str, Any]] = None
            if per_file_value is not None or per_file_role is not None:
                file_meta = {"file_path": str(file_path)}
                if per_file_value is not None:
                    file_meta["value"] = per_file_value
                if per_file_role is not None:
                    file_meta["file_role"] = per_file_role

            return matches, [], file_meta

        except Exception as exc:  # noqa: BLE001
            return (
                [],
                [
                    {
                        "file_path": str(file_path),
                        "line_number": 0,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                ],
                None,
            )

    @staticmethod
    def _check_zero_match_patterns(
        include_patterns: List[str],
        all_rel_paths: List[str],
    ) -> List[Dict[str, Any]]:
        """Return a warning dict for each include_pattern that matched zero files.

        Args:
            include_patterns: The caller-supplied glob patterns.  Empty list
                means "include all" — no warnings are emitted.
            all_rel_paths: Relative paths (from repo root) of every file that
                existed in the repo before regex / include filtering.  These are
                used to determine whether each pattern is capable of matching
                anything in this repository at all.

        Returns:
            List of warning dicts (may be empty).  Each dict has keys:
            ``type``, ``pattern``, ``hint``.

        Notes:
            ``*`` matches a single path segment (no ``/`` traversal).
            ``**`` matches multiple path segments recursively.
            A pattern that matches zero files receives a hint explaining the
            difference and suggesting ``**`` as a fix.
        """
        if not include_patterns:
            return []
        warnings: List[Dict[str, Any]] = []
        hint = (
            "Pattern matched 0 files. fnmatch-style globs use `*` for a single "
            "path segment; use `**/time.py` (with **) for recursive matching "
            "across directories."
        )
        for pat in include_patterns:
            if not any(fnmatch.fnmatch(rel, pat) for rel in all_rel_paths):
                warnings.append(
                    {
                        "type": "zero_match_include_pattern",
                        "pattern": pat,
                        "hint": hint,
                    }
                )
        return warnings

    def _probe_zero_match_patterns_content(
        self,
        repo_path: Path,
        include_patterns: List[str],
    ) -> List[Dict[str, Any]]:
        """Probe each include_pattern to detect those that match zero files.

        Uses ``RegexSearchService`` with a trivial ``.*`` regex so that the
        glob semantics are identical to the main Phase 1 content search
        (ripgrep-backed), avoiding false warnings from Python fnmatch
        divergence (e.g. ``*/x`` has different semantics in ripgrep vs
        Python fnmatch).

        Args:
            repo_path: Root directory of the repository.
            include_patterns: The caller-supplied glob patterns to probe.

        Returns:
            List of zero-match warning dicts (may be empty).
        """
        if not include_patterns:
            return []

        hint = (
            "Pattern matched 0 files. fnmatch-style globs use `*` for a single "
            "path segment; use `**` for recursive matching across directories "
            "(e.g. `**/time.py` instead of `*/time.py`)."
        )
        warnings: List[Dict[str, Any]] = []
        probe_service = RegexSearchService(repo_path)

        for pat in include_patterns:
            probe_result = _run_async_in_sync(
                probe_service.search(
                    pattern=r".*",
                    include_patterns=[pat],
                    max_results=1,
                )
            )
            if not probe_result.matches:
                warnings.append(
                    {
                        "type": "zero_match_include_pattern",
                        "pattern": pat,
                        "hint": hint,
                    }
                )

        return warnings

    def _run_phase1_driver(
        self,
        repo_path: Path,
        driver_regex: str,
        search_target: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
        *,
        case_sensitive: bool = True,
        context_lines: int = 0,
        multiline: bool = False,
        pcre2: bool = False,
        path: Optional[str] = None,
    ) -> List[Path]:
        """Phase 1: regex-based candidate file selection.

        For ``search_target="content"``, delegates to ``RegexSearchService``
        (ripgrep-backed) for performance and PCRE2 support.  Per-match line
        positions are stored in ``self._last_phase1_positions`` for use by
        Phase 2 without changing the call-sites.

        For ``search_target="filename"``, uses the inline path-match walker
        because ``RegexSearchService`` is a content-only service and has no
        filename-search mode.

        Args:
            repo_path: Root directory to walk.
            driver_regex: Regular expression applied in Phase 1.
            search_target: ``"filename"`` or ``"content"``.
            include_patterns: Glob include filters (empty = include all).
            exclude_patterns: Glob exclude filters (empty = exclude none).
            case_sensitive: Whether content regex is case-sensitive.
            context_lines: Lines of context before/after each hit.
            multiline: Enable multi-line regex.
            pcre2: Enable PCRE2 engine.
            path: Optional subdirectory restriction within repo.

        Returns:
            Ordered, deduplicated list of matching Path objects.
        """
        # Reset per-call side-channels.
        # Positions are dicts: {line_number, line_content, column, byte_offset,
        # context_before, context_after}
        self._last_phase1_positions: Dict[Path, List[Dict[str, Any]]] = {}
        self._last_phase1_warnings: List[Dict[str, Any]] = []

        if search_target == "filename":
            return self._run_phase1_filename(
                repo_path, driver_regex, include_patterns, exclude_patterns
            )

        return self._run_phase1_content(
            repo_path,
            driver_regex,
            include_patterns,
            exclude_patterns,
            case_sensitive=case_sensitive,
            context_lines=context_lines,
            multiline=multiline,
            pcre2=pcre2,
            path=path,
        )

    def _run_phase1_filename(
        self,
        repo_path: Path,
        driver_regex: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
    ) -> List[Path]:
        """Inline filename-path walker (RegexSearchService has no filename mode).

        Args:
            repo_path: Root directory to walk.
            driver_regex: Regular expression applied to relative file paths.
            include_patterns: Glob include filters — ``*`` matches one path
                segment, ``**`` matches multiple segments recursively.
                Warnings are emitted for any pattern that matches zero files.
            exclude_patterns: Glob exclude filters (empty = exclude none).

        Returns:
            Ordered, deduplicated list of matching Path objects.
        """
        pattern = re.compile(driver_regex)
        candidates: List[Path] = []
        all_rel_paths: List[str] = []

        for p in sorted(repo_path.rglob("*")):
            if not p.is_file():
                continue

            rel = str(p.relative_to(repo_path))
            # Finding 3.6 (v10.4.4): skip CIDX's internal index store.
            if rel.startswith(".code-indexer/") or rel == ".code-indexer":
                continue
            # v10.4.6 (Defect 2): also exclude .git/ — git internals
            # (FETCH_HEAD, COMMIT_EDITMSG, objects/, etc.) are never code candidates.
            if rel.startswith(".git/") or rel == ".git":
                continue
            all_rel_paths.append(rel)

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

        self._last_phase1_warnings = self._check_zero_match_patterns(
            include_patterns, all_rel_paths
        )
        return candidates

    def _run_phase1_content(
        self,
        repo_path: Path,
        driver_regex: str,
        include_patterns: List[str],
        exclude_patterns: List[str],
        *,
        case_sensitive: bool = True,
        context_lines: int = 0,
        multiline: bool = False,
        pcre2: bool = False,
        path: Optional[str] = None,
    ) -> List[Path]:
        """Content driver via RegexSearchService (ripgrep-backed, async-bridged).

        Args:
            repo_path: Root directory to search.
            driver_regex: Regular expression applied to file content.
            include_patterns: Glob include filters — ``*`` matches one path
                segment, ``**`` matches multiple segments recursively.
                Warnings are emitted for any pattern that matches zero files
                in the repository, regardless of whether the regex matched.
            exclude_patterns: Glob exclude filters (empty = exclude none).
            case_sensitive: Whether the regex match is case-sensitive.
            context_lines: Lines of context before/after each match (0..10).
            multiline: Enable multi-line regex matching.
            pcre2: Enable PCRE2 regex engine.
            path: Optional subdirectory restriction relative to repo root.

        Returns:
            Ordered, deduplicated list of matching Path objects.
        """
        # Finding 3.6 (v10.4.4): always exclude CIDX's internal index store from
        # content-mode Phase 1 — ripgrep walks it otherwise and surfaces error logs
        # / vector .json files as candidates.
        effective_excludes = [
            ".code-indexer/**",
            ".git/**",
            *list(exclude_patterns or []),
        ]
        service = RegexSearchService(repo_path)
        search_result = _run_async_in_sync(
            service.search(
                pattern=driver_regex,
                path=path,
                include_patterns=include_patterns or None,
                exclude_patterns=effective_excludes,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_results=100_000,
                multiline=multiline,
                pcre2=pcre2,
            )
        )

        # Build deduplicated candidate list and populate positions side-channel.
        # Positions are dicts with line_number, line_content, column, byte_offset,
        # context_before, context_after — exposed to evaluators as match_positions.
        seen: Dict[Path, bool] = {}
        candidates: List[Path] = []
        for m in search_result.matches:
            abs_path = repo_path / m.file_path
            if abs_path not in seen:
                seen[abs_path] = True
                candidates.append(abs_path)
                self._last_phase1_positions[abs_path] = []
            self._last_phase1_positions[abs_path].append(
                {
                    "line_number": m.line_number,
                    "line_content": m.line_content,
                    "column": getattr(m, "column", 0) or 0,
                    "byte_offset": _line_to_byte_offset(
                        # Defer actual source read to Phase 2; use line_number
                        # to produce a sentinel byte offset for now. The evaluator
                        # receives the real source in its globals. Store 0 here;
                        # the engine will re-derive from source in _evaluate_file.
                        "",
                        m.line_number,
                    ),
                    "context_before": getattr(m, "context_before", []) or [],
                    "context_after": getattr(m, "context_after", []) or [],
                }
            )

        # Probe each include_pattern to detect zero-match patterns.
        # Uses ripgrep-backed probe so glob semantics match the main search.
        if include_patterns:
            self._last_phase1_warnings = self._probe_zero_match_patterns_content(
                repo_path, include_patterns
            )

        return candidates
