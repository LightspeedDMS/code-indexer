"""EVO-66445: reliable Tantivy FTS index builder for golden repos.

Golden repos are indexed IN PLACE. Registration's ``cidx index --fts`` builds FTS
through ``smart_indexer``, which **silently degrades to semantic-only** when FTS
init/build fails (its ``except Exception -> fts_manager = None`` "graceful
degradation"). That left the entire golden fleet without a ``tantivy_index``:
semantic search worked, but ``search_code(search_mode="fts")`` errored fleet-wide
("FTS index not available ... Build FTS index with 'cidx index --fts'").

This module is the ONE reliable primitive shared by both halves of the fix:

* forward  -- the golden-repo registration/refresh path calls it after semantic
  indexing so a missing FTS index is (re)built and any failure is surfaced, not
  swallowed;
* reverse  -- the FTS backfill sweep calls it per golden repo to build the index
  on the ~800+ already-registered repos.

It verifies the index and, when absent, (init-guards then) rebuilds it via
``cidx index --rebuild-fts-index`` -- the same reliable command the admin
add-index path uses. That command is FTS-only, so it performs **no embedding
calls** (no VoyageAI cost). The outcome is structured and a build failure is
ALWAYS logged at ERROR and reported as ``FAILED`` -- never returned as success.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from code_indexer.server.utils.index_command_layout import (
    append_server_layout_args,
    is_pod_local_repo,
)
from code_indexer.utils.index_store import index_root
from code_indexer.server.services.index_freshness import (
    build_commit_is_current,
    record_build_commit,
)
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Tantivy FTS index directory (sibling of the semantic ``index`` dir).
FTS_INDEX_DIRNAME = "tantivy_index"
#: Tantivy writes ``meta.json`` inside the index dir; ``smart_indexer`` uses it as
#: the canonical "index already exists" marker, so we treat it as the built-marker.
_FTS_MARKER = "meta.json"
#: ``cidx index`` requires an initialized project (``config.json``).
_CONFIG_MARKER = "config.json"
#: Marker written when a repo has NO indexable source files, so ``cidx index
#: --rebuild-fts-index`` legitimately finds nothing to index. Discovery treats a
#: marked repo as "not missing" so the sweep stops re-attempting a permanently
#: file-less repo (config/infra mirrors) every tick. Cleared on a successful
#: rebuild so a repo that later gains code is picked up again (no stale skip).
_SKIP_MARKER = "fts_backfill_no_files.skip"

# Truncate captured subprocess output in log/detail lines (avoid dumping MBs).
_DETAIL_MAX = 500
#: Substring (case-folded) the CLI/daemon rebuild emits when FileFinder discovers no
#: indexable files -- the ONE non-failure reason a rebuild exits non-zero.
_NO_FILES_SIGNAL = "no files found to index"


class FtsBuildStatus(str, Enum):
    """Outcome of :func:`ensure_fts_index`."""

    PRESENT = "present"  # index already existed; no work performed
    REBUILT = "rebuilt"  # index was missing and was successfully (re)built
    FAILED = "failed"  # index missing and (re)build failed -- surfaced, never swallowed
    SKIPPED = (
        "skipped"  # repo has no indexable files -- nothing to build (not a failure)
    )


@dataclass(frozen=True)
class FtsBuildOutcome:
    """Structured, non-silent result of an FTS ensure/rebuild attempt."""

    status: FtsBuildStatus
    alias: Optional[str]
    detail: str

    @property
    def ok(self) -> bool:
        """True iff the repo ended with a usable FTS index (present or rebuilt)."""
        return self.status in (FtsBuildStatus.PRESENT, FtsBuildStatus.REBUILT)


def fts_index_present(repo_path: Path) -> bool:
    """Return True iff a *built* Tantivy FTS index exists at ``repo_path``.

    Presence is defined by the Tantivy ``meta.json`` marker, mirroring
    ``smart_indexer``'s own existing-index check -- an empty ``tantivy_index/``
    directory (e.g. a stale mktemp) does NOT count as built.
    """
    return (index_root(repo_path) / FTS_INDEX_DIRNAME / _FTS_MARKER).exists()


def fts_backfill_skip_present(repo_path: Path) -> bool:
    """Return True iff ``repo_path`` was marked as having no indexable files.

    Set by :func:`ensure_fts_index` when a rebuild reports no files to index; read by
    the backfill discovery so a permanently file-less repo is not re-attempted every
    tick. A successful rebuild clears it, so the mark never wrongly sticks.
    """
    return (index_root(repo_path) / _SKIP_MARKER).exists()


def _combined_output(result: "subprocess.CompletedProcess[str]") -> str:
    """stdout + stderr joined. The CLI prints its banner (``🔧 Running in local
    mode``) to stderr but the actual error (``❌ No files found to index``) to
    stdout, so neither stream alone is sufficient."""
    parts = [s.strip() for s in (result.stdout, result.stderr) if s and s.strip()]
    return "\n".join(parts)


def _detail(result: "subprocess.CompletedProcess[str]") -> str:
    text = _combined_output(result)
    if not text:
        return f"exit {result.returncode}"
    # Surface the meaningful error, not the leading ``🔧 Running in local mode``
    # banner: prefer the CLI's explicit ``❌``-prefixed error line(s), else the
    # trailing output (the error is emitted last, after any banners).
    errs = [ln.strip() for ln in text.splitlines() if ln.lstrip().startswith("❌")]
    detail = " / ".join(errs) if errs else text.splitlines()[-1]
    return detail[:_DETAIL_MAX]


def _run_or_fail(
    runner: Callable[..., "subprocess.CompletedProcess[str]"],
    cmd: list,
    *,
    cwd: str,
    env: Optional[dict],
    timeout: Optional[int],
    alias: Optional[str],
    phase: str,
) -> "tuple[Optional[subprocess.CompletedProcess[str]], Optional[FtsBuildOutcome]]":
    """Run a ``cidx`` subprocess, converting a LAUNCH failure into a FAILED outcome.

    A subprocess that fails to *start* (``FileNotFoundError`` -- ``cidx`` not on
    PATH, ``PermissionError``, any other ``OSError``) or that exceeds ``timeout``
    (``subprocess.TimeoutExpired``, a ``SubprocessError``) must NEVER propagate out
    of :func:`ensure_fts_index`. The sole registration-path caller
    (``GoldenRepoManager._execute_post_clone_workflow``) wraps its whole body in
    ``except (OSError, subprocess.TimeoutExpired, ...)`` -> ``GitOperationError``,
    which rolls back the entire golden-repo registration (deleting the just-cloned
    repo) -- the exact opposite of this fix's "non-fatal, semantic is load-bearing"
    guarantee. So we catch launch failures here and report them as ``FAILED``.

    Returns ``(result, None)`` on a clean launch (caller inspects
    ``result.returncode``), or ``(None, outcome)`` when the launch itself failed.
    """
    try:
        result = runner(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(
            "EVO-66445 FTS %s subprocess did not run for alias=%s at %s: %r",
            phase,
            alias,
            cwd,
            exc,
        )
        return None, FtsBuildOutcome(
            FtsBuildStatus.FAILED, alias, f"{phase} subprocess error: {exc}"
        )
    return result, None


def ensure_fts_index(
    repo_path: Path,
    *,
    alias: Optional[str] = None,
    subprocess_env: Optional[dict] = None,
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    timeout: Optional[int] = None,
) -> FtsBuildOutcome:
    """Guarantee a Tantivy FTS index for the in-place golden repo at ``repo_path``.

    Behaviour:

    * index already present -> :attr:`FtsBuildStatus.PRESENT` (no subprocess run);
    * index missing -> init-guard (run ``cidx init --no-override-file`` iff
      ``config.json`` is absent), then ``cidx index --rebuild-fts-index``, then
      re-verify the marker -> :attr:`FtsBuildStatus.REBUILT`;
    * any failure -- the subprocess failing to launch / timing out, init non-zero,
      rebuild non-zero, or the rebuild exiting 0 but writing no index -- ->
      :attr:`FtsBuildStatus.FAILED`, logged at ERROR.

    Crucially this NEVER raises for a build/launch failure: it always returns a
    structured outcome, so a caller (e.g. golden-repo registration) can treat a
    failed FTS build as non-fatal without risking an exception that aborts/rolls
    back unrelated work. The "exit 0 but no index" case is deliberately FAILED --
    exactly the silent degradation this fix exists to eliminate.

    Args:
        repo_path: the in-place golden repo root (parent of ``.code-indexer``).
        alias: golden-repo alias, for log/outcome attribution only.
        subprocess_env: environment for the child ``cidx`` processes (typically
            ``build_cidx_subprocess_env()``); ``None`` inherits the parent env.
        runner: injection seam for tests; defaults to :func:`subprocess.run`.
        timeout: optional per-subprocess wall-clock timeout in seconds. The
            registration path leaves this ``None`` (Bug #1218: the indexing path
            carries no wall-clock clock -- a large repo legitimately takes a while,
            and this mirrors the sibling ``cidx index --fts`` step); the
            maintenance backfill sweep passes a bounded value so one stuck repo
            can't wedge a tick. A ``TimeoutExpired`` becomes a ``FAILED`` outcome.
    """
    ci = index_root(repo_path)

    # Present is not enough to skip: an index built at a commit the clone has moved
    # past answers queries with content that no longer exists. Short-circuiting on
    # presence alone is what made the backfill sweep's staleness rebuild inert --
    # the engine enqueued the job and this returned PRESENT without building.
    if fts_index_present(repo_path) and build_commit_is_current(
        repo_path, ci / FTS_INDEX_DIRNAME
    ):
        return FtsBuildOutcome(
            FtsBuildStatus.PRESENT,
            alias,
            f"FTS index already present at {ci / FTS_INDEX_DIRNAME}",
        )

    # init-guard: a bare in-place golden dir may lack config.json (observed on the
    # live fleet), and ``cidx index`` refuses to run on an uninitialized project.
    if not (ci / _CONFIG_MARKER).exists():
        init, launch_outcome = _run_or_fail(
            runner,
            ["cidx", "init", "--no-override-file"],
            cwd=str(repo_path),
            env=subprocess_env,
            timeout=timeout,
            alias=alias,
            phase="init",
        )
        if launch_outcome is not None:
            return launch_outcome
        assert init is not None  # narrow for mypy: launch_outcome is None here
        if init.returncode != 0:
            detail = _detail(init)
            logger.error(
                "EVO-66445 FTS backfill: cidx init failed for alias=%s at %s: %s",
                alias,
                repo_path,
                detail,
            )
            return FtsBuildOutcome(
                FtsBuildStatus.FAILED, alias, f"init failed: {detail}"
            )

    result, launch_outcome = _run_or_fail(
        runner,
        append_server_layout_args(
            ["cidx", "index", "--rebuild-fts-index"],
            # cidx-meta's files are on this pod, never with workspace-mcp.
            local_content=is_pod_local_repo(None, alias),
        ),
        cwd=str(repo_path),
        env=subprocess_env,
        timeout=timeout,
        alias=alias,
        phase="rebuild",
    )
    if launch_outcome is not None:
        return launch_outcome
    assert result is not None  # narrow for mypy: launch_outcome is None here
    if result.returncode != 0:
        detail = _detail(result)
        # A repo with no indexable source files (config/infra mirror) legitimately has
        # nothing to build -- that is a SKIP, not a failure. Mark it so discovery stops
        # re-attempting it every tick; log at INFO (expected), never ERROR.
        if _NO_FILES_SIGNAL in _combined_output(result).casefold():
            try:
                (ci / _SKIP_MARKER).touch()
            except OSError as exc:  # marking is best-effort; never fail the sweep on it
                logger.warning(
                    "EVO-66445 FTS backfill: could not write skip marker for alias=%s at %s: %s",
                    alias,
                    repo_path,
                    exc,
                )
            logger.info(
                "EVO-66445 FTS backfill: alias=%s at %s has no indexable files -- skipping",
                alias,
                repo_path,
            )
            return FtsBuildOutcome(
                FtsBuildStatus.SKIPPED,
                alias,
                "no indexable files to build an FTS index",
            )
        logger.error(
            "EVO-66445 FTS (re)build failed for alias=%s at %s: %s",
            alias,
            repo_path,
            detail,
        )
        return FtsBuildOutcome(
            FtsBuildStatus.FAILED, alias, f"rebuild failed: {detail}"
        )

    if not fts_index_present(repo_path):
        # Non-silent guard: the command "succeeded" but produced no index -- the
        # exact silent-degradation mode this whole change eliminates.
        logger.error(
            "EVO-66445 FTS (re)build produced no index for alias=%s at %s despite exit 0",
            alias,
            repo_path,
        )
        return FtsBuildOutcome(
            FtsBuildStatus.FAILED,
            alias,
            "rebuild exited 0 but no tantivy_index marker was written",
        )

    # Success: clear any stale "no files" mark so a repo that has since gained code is
    # not wrongly skipped by discovery on the next tick.
    (ci / _SKIP_MARKER).unlink(missing_ok=True)
    # Record WHICH revision this index was built from, so the backfill engine can tell
    # later that the clone has moved on. Best-effort: not recording costs one extra
    # rebuild, never a wrong answer.
    record_build_commit(ci / FTS_INDEX_DIRNAME, repo_path)
    return FtsBuildOutcome(
        FtsBuildStatus.REBUILT, alias, f"FTS index rebuilt at {ci / FTS_INDEX_DIRNAME}"
    )
