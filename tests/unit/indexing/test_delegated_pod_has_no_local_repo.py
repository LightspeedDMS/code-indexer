"""What a pod with no repository mount needs, found by running the seam on EKS.

Two defects only a genuinely mount-less run could surface. Locally the files are
always present, so both paths produced correct-looking answers for the wrong
reason.

* ``FileIdentifier`` decided whether a repository was git-addressable by probing
  the LOCAL filesystem. In a delegated pod that probe says "not a git repo", so
  the blob hashes the manifest was holding were never asked for. On EKS, against
  a real golden repo, every ``git_hash`` came back ``None`` while the backend
  answered perfectly.

* ``RepoManifest`` understood only paths under the repository root, while the
  fetch planner speaks repo-relative ones. Every size lookup returned ``None``,
  the planner read that as "unmeasurable" and sent each file alone -- silently
  becoming the per-file fetching it exists to prevent. 43 requests where one was
  correct.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import pytest

from code_indexer.config import Config
from code_indexer.indexing.fetch_planner import plan_fetch_batches
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
)
from code_indexer.services.file_identifier import FileIdentifier

#: Deliberately not created. A delegated pod does not have the repository.
ABSENT_REPO = Path("/nonexistent/delegated-golden-repo")


class _DelegatedBackend:
    def __init__(self, entries: Sequence[ContentEntry]) -> None:
        self._entries = tuple(entries)
        self.is_local = False

    def manifest(
        self, repo_path: Any, *, cursor: Optional[str] = None, page_size: int = 1000
    ) -> ContentPage:
        return ContentPage(entries=self._entries, next_cursor=None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
        raise AssertionError("the hash phase must not fetch bytes")


_ENTRIES = (
    ContentEntry(path="pkg/a.py", size=123, blob_sha="a" * 40, is_symlink=False),
    ContentEntry(path="pkg/b.py", size=456, blob_sha="b" * 40, is_symlink=False),
)


def _manifest(root: Path = ABSENT_REPO) -> RepoManifest:
    return RepoManifest.build(root, backend=_DelegatedBackend(_ENTRIES))


# ── git-addressability must not depend on a local disk ─────────────────────


def test_a_manifest_makes_a_repository_git_addressable() -> None:
    """Without this the seam resolves, connects, and then is never consulted."""
    identifier = FileIdentifier(
        ABSENT_REPO, Config(codebase_dir=ABSENT_REPO), manifest=_manifest()
    )
    assert identifier.git_available is True


def test_without_a_manifest_the_local_probe_still_decides() -> None:
    """The default path keeps its existing answer, including "no".

    Forcing git_available True for everyone would make every non-git directory
    claim a blob hash it cannot produce.
    """
    identifier = FileIdentifier(ABSENT_REPO, Config(codebase_dir=ABSENT_REPO))
    assert identifier.git_available is False


def test_attaching_and_detaching_a_manifest_moves_the_answer() -> None:
    """The manifest's lifetime is a pass, so this flips twice per indexer."""
    identifier = FileIdentifier(ABSENT_REPO, Config(codebase_dir=ABSENT_REPO))
    assert identifier.git_available is False
    identifier.set_manifest(_manifest())
    assert identifier.git_available is True
    identifier.set_manifest(None)
    assert identifier.git_available is False


def test_the_blob_hash_is_served_with_no_local_checkout() -> None:
    """The end the EKS run failed at: a real hash, from a path that does not exist."""
    identifier = FileIdentifier(
        ABSENT_REPO, Config(codebase_dir=ABSENT_REPO), manifest=_manifest()
    )
    meta = identifier.get_file_metadata(
        ABSENT_REPO / "pkg/a.py", defer_content_hash=True
    )
    assert meta["git_available"] is True
    assert meta["git_hash"] == "a" * 40


def test_branch_and_commit_degrade_instead_of_raising(tmp_path: Path) -> None:
    """Spawning git with a cwd that does not exist raises from the SPAWN.

    That is FileNotFoundError, not CalledProcessError, so it escaped the handler
    and took the whole metadata call with it the moment git_available became
    True without a local checkout.
    """
    identifier = FileIdentifier(
        ABSENT_REPO, Config(codebase_dir=ABSENT_REPO), manifest=_manifest()
    )
    meta = identifier.get_file_metadata(
        ABSENT_REPO / "pkg/a.py", defer_content_hash=True
    )
    assert meta["branch"] == "unknown"
    assert meta["commit_hash"] == "unknown"


# ── the manifest must accept both path conventions ─────────────────────────


@pytest.mark.parametrize(
    "query",
    ["pkg/a.py", "./pkg/a.py", "pkg/./a.py", str(ABSENT_REPO / "pkg/a.py")],
)
def test_both_path_conventions_resolve(query: str) -> None:
    """The indexer holds absolute paths; the planner and the wire hold relative."""
    manifest = _manifest()
    assert manifest.size(query) == 123
    assert manifest.blob_sha(query) == "a" * 40


@pytest.mark.parametrize(
    "query", ["/somewhere/else/pkg/a.py", "../escape.py", "../../etc/passwd"]
)
def test_a_path_outside_the_repository_resolves_to_no_key(query: str) -> None:
    """Accepting relative paths must not accept relative paths that climb out.

    Asserted on the KEY, not on size(). A climbing path has no entry either way,
    so size() returns None whether the guard fired or not -- the first version of
    this test passed with the guard deleted. Only the key distinguishes "refused"
    from "looked up and missed".
    """
    manifest = _manifest()
    assert manifest._relative(query) is None


def test_a_path_inside_the_repository_does_resolve_to_a_key() -> None:
    """Stops the test above passing because _relative returns None for everything."""
    manifest = _manifest()
    assert manifest._relative("pkg/a.py") == "pkg/a.py"
    assert manifest._relative(ABSENT_REPO / "pkg/a.py") == "pkg/a.py"


def test_the_planner_batches_instead_of_sending_each_file_alone() -> None:
    """The silent degradation: unknown sizes mean one request per file.

    That is the per-file pattern measured at 25x the round trips and one TCP
    connection per file -- reached with no error, from a path-convention
    mismatch alone.
    """
    manifest = _manifest()
    paths = ["pkg/a.py", "pkg/b.py"]
    batches = list(plan_fetch_batches(paths, manifest.size))
    assert batches == [paths], (
        f"expected one batch, got {len(batches)} -- sizes are resolving to None "
        "and every file is being sent on its own"
    )


def test_an_unknown_file_still_travels_alone() -> None:
    """The real unmeasurable case must keep working after the fix."""
    manifest = _manifest()
    batches = list(plan_fetch_batches(["pkg/a.py", "added_later.py"], manifest.size))
    assert ["added_later.py"] in batches
