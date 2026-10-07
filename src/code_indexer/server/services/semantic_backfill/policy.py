"""Semantic (vector) index backfill policy + its POD_PULL re-embed executor.

Rides the DRY :class:`GoldenIndexBackfillReconciler` exactly like FTS/SCIP: the leader
scans golden repos, and for any that is missing its semantic vector index (and is a
registered golden repo) enqueues ONE ``semantic_backfill_reindex`` job. That op is
POD_PULL + memory-heavy (a re-embed is as heavy as an add), so pods claim DISTINCT
repos under the same memory-admission gate as add_golden_repo.

COST: a semantic re-embed calls VoyageAI ($) — unlike the free local FTS build. So the
policy is inert unless explicitly enabled (``semantic_backfill_sweep_config.enabled``,
DEFAULT FALSE): eligibility returns None when disabled, so the engine enqueues nothing.

HARDENING (against the silent-empty failure that left 840 repos "added successfully" but
with 0 vectors): presence and success are decided by the ACTUAL ``vector_count``, never by
a "completed" status or the indexing_progress counters (which can read 0 for a full index).
``is_present`` treats a present-but-empty index as MISSING; the executor FAILS LOUD when a
repo with indexable files re-embeds to 0 vectors, and marks a genuinely file-less repo so
the sweep stops retrying it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError

logger = logging.getLogger(__name__)

#: POD_PULL operation type for a single-repo semantic re-embed (must match the dispatch
#: key + POD_PULL_OPS/_MEMORY_HEAVY_OPS membership — see background_jobs).
SEMANTIC_BACKFILL_REINDEX_OP = "semantic_backfill_reindex"

#: Where the semantic collections live under a golden clone.
_SEMANTIC_INDEX_SUBDIR = ".code-indexer/index"
#: Per-collection metadata file holding ``hnsw_index.vector_count`` (the same fast-path
#: FilesystemVectorStore.count_points reads).
_COLLECTION_META = "collection_meta.json"
#: Collections that are NOT the semantic code index.
_NON_SEMANTIC = ("multimodal", "temporal", "tantivy")
#: Marker (on the clone's .code-indexer) for a repo with no indexable code — it
#: legitimately embeds to 0 vectors, so ``is_present`` honours it to stop re-embedding.
SEMANTIC_NO_FILES_MARKER = ".semantic_backfill_no_files.skip"
#: Directories that are not the repo's own content when deciding "all files are empty".
_CONTENT_WALK_SKIP = frozenset({".git", ".code-indexer"})
#: A file larger than this is assumed to carry real content without reading it all.
_EMPTY_PROBE_BYTES = 4096


class SemanticReindexProducedNoVectorsError(RuntimeError):
    """A re-embed of a repo WITH indexable files produced 0 vectors — the exact
    silent-empty failure (index ran against an empty/removed clone → "success" with no
    vectors) that left the fleet's semantic index gone. Raising it FAILS the job loudly
    (retried after cooldown) instead of masking an empty index as done."""


def _index_base(repo_path: Path) -> Path:
    return repo_path / _SEMANTIC_INDEX_SUBDIR


def semantic_index_vector_count(index_base_path: Path) -> int:
    """Sum ``hnsw_index.vector_count`` across the repo's SEMANTIC collections (excluding
    multimodal/temporal/tantivy) by reading each ``collection_meta.json`` — the SAME
    fast-path :meth:`FilesystemVectorStore.count_points` uses.

    This is the TRUSTWORTHY completeness signal. ``detect_semantic_index`` only checks
    that an ``hnsw_index.bin`` file exists (an empty index passes), and
    ``indexing_progress.json``'s ``files_completed``/``chunks_created`` can read 0 even
    for a fully-built index (observed on evolution: 44,805 real vectors, progress said 0).
    ``vector_count`` reflects the vectors actually written. Returns 0 when absent."""
    if not index_base_path.is_dir():
        return 0
    total = 0
    for coll in sorted(index_base_path.iterdir(), key=lambda p: p.name):
        if not coll.is_dir() or any(tag in coll.name for tag in _NON_SEMANTIC):
            continue
        meta = coll / _COLLECTION_META
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text())
            vc = data.get("hnsw_index", {}).get("vector_count")
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(vc, int):
            total += vc
    return total


def _discovered_indexable_files(clone_path: Path) -> Optional[int]:
    """Count of indexable files the last ``cidx index`` DISCOVERED, from
    ``indexing_progress.json``'s ``total_files``.

    Returns **0** for a genuinely FILE-LESS repo: ``cidx index`` (smart_indexer) raises
    ``"No files found to index"`` BEFORE it ever calls ``start_session()``, and
    ``_execute_post_clone_workflow`` TOLERATES that error — so the progress file is never
    written (absent), or a stale one has ``current_session: null``. Both mean "no files",
    which must route to the skip-marker, NOT to fail-loud. Returns **None** only when the
    file exists but is genuinely UNREADABLE/corrupt (rare) — there the caller can't confirm
    file-less, so it errs on fail-loud rather than silently masking a real failure."""
    p = clone_path / ".code-indexer" / "indexing_progress.json"
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return 0  # never started a session → nothing to index → genuinely file-less
    except OSError:
        return None  # unreadable (IO/perms) → unknown
    try:
        session = json.loads(raw).get("current_session")
    except (json.JSONDecodeError, AttributeError):
        return None  # corrupt → unknown
    if not session:
        return 0  # present but no session → file-less
    tf = session.get("total_files")
    return tf if isinstance(tf, int) else None


def _clone_has_only_empty_files(clone_path: Path) -> bool:
    """True iff the clone holds AT LEAST ONE of its own files and every one of them is
    empty (no non-whitespace bytes).

    This separates two cases that both reach the 0-vector branch with ``discovered > 0``:

    * The repo really is a placeholder — e.g. a single 0-byte ``README.md``. ``cidx index``
      DISCOVERS the file (``total_files: 1``) but there is nothing to chunk, so 0 vectors is
      the CORRECT outcome. Failing loud here retries the repo forever and never converges.
    * The clone is empty/removed under a progress file that still claims files. That is the
      silent-empty failure this guard exists for, and it must keep failing loud.

    The distinction is "files present but empty" vs "files absent", so a vanished clone
    returns False (no files seen) and still raises. Anything unreadable also returns False --
    an unreadable FILE via its own OSError, and an unlistable DIRECTORY via the ``onerror``
    hook below, since ``os.walk`` would otherwise skip it silently and the subtree would
    read as "no files here". We cannot confirm emptiness, so we err toward fail-loud rather
    than mask a real failure.
    """

    def _reraise(err: OSError) -> None:
        # os.walk SWALLOWS listing errors by default, which would let an unreadable
        # subtree look like "no files here" and mask a repo that does have content.
        raise err

    seen_file = False
    try:
        for root, dirs, files in os.walk(clone_path, onerror=_reraise):
            dirs[:] = [d for d in dirs if d not in _CONTENT_WALK_SKIP]
            for name in files:
                path = Path(root) / name
                try:
                    if path.stat().st_size > _EMPTY_PROBE_BYTES:
                        return False  # too big to be empty — real content
                    if path.read_bytes().strip():
                        return False  # non-whitespace content
                except OSError:
                    return False  # unreadable → cannot confirm → fail loud
                seen_file = True
    except OSError:
        return False
    return seen_file


def semantic_index_present(repo_path: Path) -> bool:
    """Count-based presence: 'present' iff the repo has REAL vectors (vector_count > 0),
    OR it is a marked no-indexable-files repo. A present-but-empty index (hnsw file exists,
    0 vectors) is treated as MISSING so the sweep re-embeds it — closing the gap where a
    presence-only check would leave a 0-vector index stuck forever."""
    if semantic_index_vector_count(_index_base(repo_path)) > 0:
        return True
    return (repo_path / ".code-indexer" / SEMANTIC_NO_FILES_MARKER).exists()


def semantic_backfill_reindex_executor(
    golden_repo_manager: Any,
    metadata: Dict[str, Any],
    progress_callback: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Re-embed ONE golden repo's semantic index from the pod-pull ``metadata`` row
    (clone_path + alias), then VALIDATE the result by ``vector_count`` — never by a
    "completed" status.

    Uses ``_execute_post_clone_workflow(force_init=False)`` (the incremental-refresh path:
    SKIP ``cidx init`` when config exists, run ``cidx index``) — no ``--force``/``--clear``,
    so it fills the semantic gap WITHOUT clearing the co-located FTS/SCIP indexes.

    Post-check (the hardening): read the real vector_count. If > 0 → success. If 0 and the
    repo had indexable files → raise (fail loud; retried after cooldown). If 0 and the repo
    is genuinely file-less → mark it so the sweep stops re-embedding a repo that has nothing
    to embed."""
    clone_path = metadata["clone_path"]
    alias = metadata.get("alias")
    golden_repo_manager._execute_post_clone_workflow(
        clone_path,
        force_init=False,
        golden_repo_alias=alias,
        job_id=metadata.get("job_id"),
        progress_callback=progress_callback,
    )

    clone = Path(clone_path)
    vector_count = semantic_index_vector_count(_index_base(clone))
    if vector_count > 0:
        logger.info(
            "semantic_backfill: '%s' re-embedded → %d vectors", alias, vector_count
        )
        return {"status": "reindexed", "alias": alias, "vector_count": vector_count}

    # 0 vectors — distinguish a legitimately file-less repo from a real (silent) failure.
    discovered = _discovered_indexable_files(clone)
    # Two routes to "nothing to embed": cidx discovered no files at all, OR it discovered
    # files that are all empty (a placeholder repo — e.g. one 0-byte README). Both embed to
    # 0 vectors legitimately, and both must stop being retried. A clone whose files are
    # MISSING is not either case and still falls through to fail-loud below.
    if discovered == 0 or _clone_has_only_empty_files(clone):
        # Nothing to embed. Mark it so the sweep stops re-embedding this repo.
        marker = clone / ".code-indexer" / SEMANTIC_NO_FILES_MARKER
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass
        logger.info(
            "semantic_backfill: '%s' has nothing to embed (discovered=%s) → marked "
            "no-files skip",
            alias,
            discovered,
        )
        return {"status": "skipped_no_files", "alias": alias}

    # discovered > 0 (files were there, none embedded) OR None (progress unreadable, can't
    # confirm file-less): fail LOUD rather than mask an empty index as done.
    raise SemanticReindexProducedNoVectorsError(
        f"semantic re-embed of '{alias}' produced 0 vectors "
        f"(discovered indexable files: {discovered}) — failing loud rather than "
        f"masking an empty index as done"
    )


class SemanticBackfillPolicy:
    """:class:`IndexBackfillPolicy` for the semantic vector index. Enqueues per-repo
    POD_PULL re-embed jobs — but ONLY when explicitly enabled (cost gate)."""

    name = "semantic"
    attempt_marker = ".semantic_backfill_attempt"

    def __init__(
        self, golden_repo_manager: Any, background_job_manager: Any, *, enabled: bool
    ) -> None:
        self._grm = golden_repo_manager
        self._bjm = background_job_manager
        self._enabled = enabled

    def is_fresh(self, repo_path: Path) -> bool:
        """Opt OUT of the engine's freshness rebuild (always "fresh").

        A semantic re-embed calls a paid provider, which is why this whole policy is
        inert unless explicitly enabled. Rebuilding ~900 repos because their clones
        moved is not a cost the sweep should incur on its own; the normal refresh
        path re-embeds a repo whose content changed, so drift is already handled
        where the work is already being paid for.
        """
        return True

    def is_present(self, repo_path: Path) -> bool:
        return semantic_index_present(repo_path)

    def eligibility(self, repo_path: Path) -> Optional[Dict[str, Any]]:
        # COST GATE: inert unless the operator opts in — a semantic re-embed spends
        # VoyageAI $, so a wiped index is reported but never auto-refilled by default.
        if not self._enabled:
            return None
        alias = repo_path.name
        # Only re-embed REGISTERED golden repos — a stray/partial dir on the shared
        # mount is not ours to (expensively) re-embed. get_golden_repo returns None
        # when unregistered.
        try:
            if self._grm.get_golden_repo(alias) is None:
                return None
        except Exception:  # noqa: BLE001 — a registry glitch must not enqueue a spurious re-embed
            return None
        return {"clone_path": str(repo_path), "alias": alias}

    def enqueue(self, alias: str, repo_path: Path, meta: Dict[str, Any]) -> None:
        # metadata= makes the row pod-pull work-stealable (reconstructed by the
        # lifespan dispatch closure, which supplies grm); the callable is the local-pool
        # fallback (solo/CLI mode). Both run the same executor.
        try:
            self._bjm.submit_job(
                SEMANTIC_BACKFILL_REINDEX_OP,
                lambda progress_callback=None: semantic_backfill_reindex_executor(
                    self._grm, meta, progress_callback
                ),
                submitter_username="system",
                is_admin=True,
                repo_alias=alias,
                metadata=meta,
            )
        except DuplicateJobError:
            logger.debug(
                "semantic_backfill: re-embed for '%s' already queued; skipping", alias
            )
