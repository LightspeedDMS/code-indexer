"""Shared freshness convention for built indexes.

SCIP already had one (task #6): a ``.build_commit`` sidecar under the index
directory holding the commit the index was built at, compared against the clone's
HEAD by :func:`scip_lifecycle.scip_db_is_fresh`. FTS had nothing -- its policy
decided presence from Tantivy's ``meta.json`` alone -- so once an FTS index
existed it was never rebuilt and drifted arbitrarily far behind the clone.

This module lifts that convention into one place so both types share it:

* the SAME marker filename, so there is one thing to look for on disk rather than
  a second parallel mechanism;
* one way to read the clone's commit;
* one way to record it after a build.

SCIP keeps its own ``scip_db_is_fresh``, which layers rules this cannot know about
(the ``no-index`` token for repos where every project is an unsupported language,
and the root-vs-subproject DB shape check). It reads the same marker this writes.

Reading the commit avoids a subprocess on purpose. The backfill sweep walks every
golden repo on every tick -- ~900 of them, every 5 minutes for FTS -- and a
``git rev-parse`` per repo per tick is not affordable at that scale, so the common
case is two small file reads. ``get_current_commit`` remains the fallback for
layouts this does not recognise.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Shared with scip_lifecycle._BUILD_COMMIT_MARKER -- one convention, one filename.
BUILD_COMMIT_MARKER = ".build_commit"


def current_commit(repo_path: Path) -> Optional[str]:
    """The clone's HEAD commit, or ``None`` when it cannot be determined.

    Reads ``.git`` directly (loose ref, packed-refs, or a detached HEAD) and falls
    back to ``get_current_commit`` for anything unrecognised -- a worktree file,
    say -- so correctness never depends on the fast path covering every layout.
    """
    git_dir = repo_path / ".git"
    try:
        head = (git_dir / "HEAD").read_text(errors="ignore").strip()
    except OSError:
        head = ""

    if head and not head.startswith("ref:"):
        return head.split()[0] or None

    if head:
        ref = head[len("ref:") :].strip()
        if ref:
            try:
                value = (git_dir / ref).read_text(errors="ignore").strip()
                if value:
                    return value.split()[0]
            except OSError:
                pass
            try:
                packed = (git_dir / "packed-refs").read_text(errors="ignore")
                for line in packed.splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == ref:
                        return parts[0]
            except OSError:
                pass

    # Unrecognised layout -- pay for the subprocess rather than guess.
    from code_indexer.utils.git_runner import get_current_commit

    try:
        return get_current_commit(repo_path)
    except Exception:  # noqa: BLE001 - freshness must never abort a sweep
        return None


def recorded_build_commit(index_dir: Path) -> Optional[str]:
    """The commit an index was built at, or ``None`` if never recorded.

    Returns the bare commit, dropping any trailing token (SCIP appends
    ``no-index``), so callers that only care about identity need not parse it.
    """
    try:
        raw = (index_dir / BUILD_COMMIT_MARKER).read_text(errors="ignore").strip()
    except OSError:
        return None
    return raw.split()[0] if raw else None


def record_build_commit(index_dir: Path, repo_path: Path) -> None:
    """Record the clone's current commit beside a freshly built index.

    Best-effort: failing here must never fail the build that just succeeded. Not
    recording costs one extra rebuild later, never a wrong answer.

    Does nothing when the index directory does not exist, so a build that produced
    no index cannot leave a marker claiming otherwise.
    """
    commit = current_commit(repo_path)
    if not commit:
        return
    if not index_dir.is_dir():
        # Record BESIDE an index, never in place of one. Creating the directory
        # here would leave a phantom index dir holding only a marker when the build
        # produced nothing -- a repo that looks indexed to anything checking for the
        # directory, and a marker claiming a build that never happened.
        logger.debug(
            "index_freshness: no index at %s; not recording a build commit",
            index_dir,
        )
        return
    try:
        (index_dir / BUILD_COMMIT_MARKER).write_text(commit)
    except OSError as exc:
        logger.debug(
            "index_freshness: could not record build commit for %s: %s", repo_path, exc
        )


def build_commit_is_current(repo_path: Path, index_dir: Path) -> bool:
    """True when the index was built at the clone's current commit.

    Unknown CURRENT commit -> treated as current, so a repo whose revision cannot
    be read is left alone instead of rebuilt on every tick. Unrecorded BUILT commit
    -> NOT current: every index predating this convention is in that state, and
    that is exactly the backlog one rebuild each clears. The engine's per-cycle
    bound and attempt cooldown pace the drain.
    """
    commit = current_commit(repo_path)
    if not commit:
        return True
    recorded = recorded_build_commit(index_dir)
    if recorded is None:
        return False
    return recorded == commit
