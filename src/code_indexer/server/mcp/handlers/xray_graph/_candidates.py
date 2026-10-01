"""Whole-repo candidate-file collection for analyze_graph (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray_graph.py -- pure relocation, zero
behaviour change. See that package's __init__.py module docstring for the
overall analyze_graph tool context.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

# R3-2 (Codex re-review, ROUND 3): the SAME whole-repo candidate-file cap
# Rust's `GRAPH_INDEX_MAX_FILES` (rust/xray-cli/src/main.rs) enforces --
# must stay in sync with that constant. Capping HERE, during the Python-
# side walk that produces the `--files-from` list, is what actually closes
# the "millions of matching files" OOM vector: Rust's own `max_files`
# bound only ever sees what Python already wrote to that file, so without
# this the full unbounded path list would already be materialized (and
# handed to Rust) before Rust's limit ever gets a chance to engage.
_GRAPH_CANDIDATE_FILES_CAP = 50_000

# Directory names skipped during the whole-repo candidate walk -- common
# VCS/build-artifact directories every other xray tool's own file
# discovery implicitly avoids too.
_SKIP_DIR_NAMES = {
    ".git",
    ".code-indexer",
    "node_modules",
    ".venv",
    "__pycache__",
    "target",
    "dist",
    "build",
}


def _collect_graph_candidate_files(
    repo_path: Path,
    include_patterns: List[str],
    exclude_patterns: List[str],
    max_files: int,
    extractor_extensions: Dict[str, str],
) -> Tuple[List[str], bool, int, int, List[str]]:
    """Whole-repo, glob-filtered file walk producing repo-relative POSIX
    paths for `--build-graph`'s `--files-from` list.

    Bug #1907 (the most dangerous defect in epic #1906): scoping
    `include_patterns`/`exclude_patterns` to one extractable language on a
    mixed-language repo used to make `analyze_graph` report
    `fact_graph_complete: true` with every degradation counter at zero,
    while the graph was missing every call site in the excluded language
    -- an excluded file never became a candidate, so no Rust-side counter
    could ever count it. Narrowing the scope did NOT improve completeness,
    it HID the incompleteness, and a method called only from the unread
    language could be reported `is_definitely_dead_code() == Some(true)`:
    a false dead verdict on live code.

    `extractor_extensions` (obtained from `xray-cli --print-graph-
    extractor-extensions` via `rust_backend.get_graph_extractor_
    extensions`, the single source of truth Rust's extractor registry
    owns) is what lets this walk classify a file it is about to exclude:
    `files_excluded_with_extractor` counts an excluded file whose language
    genuinely COULD have contributed a real call edge had it been read --
    the caller MUST downgrade `fact_graph_complete` on a nonzero count
    here. `files_excluded_without_extractor` counts an excluded file whose
    language has no extractor at all (e.g. `README.md`) -- excluding it
    changes nothing about completeness, since including it would not have
    produced any real call edges either way. Conflating the two would
    re-create this bug in a new shape. `languages_excluded_with_extractor`
    (sorted, deduplicated) names WHICH languages went unread, so a caller
    learns what is missing, not merely that something is (AC3).
    Classification is a bare extension lookup against the already-in-hand
    `extractor_extensions` dict -- no filesystem call per file, so this
    adds no measurable cost to the walk at fleet scale (~900 repos).

    Deliberately a plain path walk, not the CLI indexing pipeline's
    `Config`-bound `FileFinder` (Rule 4 anti-duplication does not apply
    here: `FileFinder` requires constructing a full indexing `Config` this
    spontaneous MCP tool has no reason to build). `include_patterns`/
    `exclude_patterns` follow `xray_search`'s own glob semantics.

    Synchronous by design -- callers MUST run this via
    `anyio.to_thread.run_sync`, never directly on the event loop (this
    walks the real filesystem and can take real wall-clock time on a large
    repo). Bounded by the repo's own finite file count (Rule 14).

    Consolidated review finding H6 (Issue #1811/Bug #1812): uses
    `os.walk` and prunes `_SKIP_DIR_NAMES` IN PLACE on `dirnames` so a
    skipped subtree (`.git`, `node_modules`, `.venv`, `target`, `dist`,
    `build`, `__pycache__`) is never descended into at all -- the
    previous `sorted(repo_path.rglob("*"))` implementation enumerated and
    `is_file()`-stat'd EVERY entry under those directories before
    discarding it as a post-hoc filter, which at production scale (hard
    NFSv3, ~5ms/op) can burn the tool's entire `timeout_seconds` budget on
    discarded work before a single real file is parsed.

    R3-2 (Codex re-review, ROUND 3): STOPS collecting once `max_files`
    matching paths have been found, rather than walking the whole repo
    and slicing afterward -- on a repo with millions of matching files,
    materializing the full path list before any limit engages risks
    OOM/timeout. Continues scanning only long enough to detect ONE
    additional match beyond the cap (then breaks immediately), returning
    `(paths, collection_truncated)` so the caller can honestly surface
    truncation via the existing degradation path rather than silently
    dropping files. Iterates `sorted(filenames)` per directory (rather
    than os.walk's raw, filesystem-dependent order) so WHICH files get
    truncated away, once the cap engages mid-directory, is deterministic
    and reproducible -- the final aggregate `results.sort()` already
    normalizes OUTPUT order regardless; this only affects the SELECTION
    made while a cap is hit mid-directory.

    Raises:
        ValueError: if `max_files < 1` -- a non-positive cap would make
            the first matching file trigger truncation and return zero
            paths, a surprising/invalid limit rather than a real bound.
    """
    if max_files < 1:
        raise ValueError(f"max_files must be >= 1, got {max_files}")
    # Bug #1876: routes through the SAME PathPatternMatcher-backed compiled
    # selector (items 3/5/6) regex_search.py's indexed path and
    # xray_search's directory-walk (AST) path both use, keeping all three
    # X-Ray/search tools in agreement on one canonical glob normalization
    # policy (brace groups, bare-directory tokens, leading `./`/`*/`).
    # This is NOT "one glob implementation" end to end: xray_search's
    # content mode (ripgrep-backed) still hands its normalized globs to
    # ripgrep's own `-g` matcher, a separate glob engine fed by the same
    # normalization -- only directory-walk/AST callers like this one use
    # this Python selector directly. Built ONCE, outside the loop, so the
    # compiled PathSpec amortizes across every file visited during this
    # walk instead of recompiling the same include/exclude patterns per
    # file.
    from code_indexer.services.path_pattern_matcher import PathPatternMatcher

    selector = PathPatternMatcher().create_selector(include_patterns, exclude_patterns)
    results: List[str] = []
    collection_truncated = False
    files_excluded_with_extractor = 0
    files_excluded_without_extractor = 0
    languages_excluded_with_extractor: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in sorted(filenames):
            rel = Path(dirpath, filename).relative_to(repo_path).as_posix()
            if not selector.select(rel):
                # Bug #1907: this file will NEVER reach Rust -- classify it
                # NOW, while its extension is still in hand, or it becomes
                # permanently invisible to every completeness counter.
                # Extension lookup only, no filesystem call.
                ext = Path(filename).suffix.lstrip(".").lower()
                language = extractor_extensions.get(ext) if ext else None
                if language is not None:
                    files_excluded_with_extractor += 1
                    languages_excluded_with_extractor.add(language)
                else:
                    files_excluded_without_extractor += 1
                continue
            if len(results) >= max_files:
                collection_truncated = True
                break
            results.append(rel)
        if collection_truncated:
            break
    results.sort()
    return (
        results,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        sorted(languages_excluded_with_extractor),
    )
