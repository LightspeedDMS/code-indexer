"""Bug #1529 Decision 1: deterministic server-context temporal data paths.

The locked recovery spec replaces Story #1457's versioned-snapshot + alias
pointer + resolver design with ONE fixed, deterministic path per
(golden repo alias, embedder, quarter), living OUTSIDE the golden repo's own
cloned directory tree. No `.versioned/v_{ts}` directories, no alias pointer
JSON, no resolver indirection.

Two independent processes must derive the SAME path from different starting
information, or the write side and read side silently diverge (exactly the
half-wiring class of defect #1529 exists to close):

  - the WRITE side is a `cidx index --index-commits` CHILD subprocess whose
    only knowledge is its own `codebase_dir` (the golden repo's clone) plus
    the CIDX_SERVER_REFRESH_CONTEXT marker;
  - the READ side is the SERVER process, which knows the golden repo's alias
    and its `golden_repos_dir` but never the child's codebase_dir.

Both therefore funnel through `server_temporal_index_root(golden_repos_dir,
repo_alias)`. These tests pin that contract, including the structural
detection needed to recover `(golden_repos_dir, repo_alias)` from a
codebase_dir in BOTH on-disk golden-repo layouts (flat and versioned).

Real paths on a real filesystem throughout -- no mocks.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.services.temporal.temporal_server_paths import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
    SERVER_TEMPORAL_ROOT_DIR_NAME,
    is_server_refresh_context,
    resolve_golden_repo_coordinates,
    resolve_server_temporal_index_root_for_codebase,
    resolve_temporal_index_dir,
    server_temporal_index_root,
)

REPO_ALIAS = "evolution"


# ---------------------------------------------------------------------------
# Server-context marker
# ---------------------------------------------------------------------------


def test_server_context_detected_only_when_marker_present(monkeypatch) -> None:
    monkeypatch.delenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, raising=False)
    assert is_server_refresh_context() is False

    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    assert is_server_refresh_context() is True


def test_empty_marker_is_not_server_context(monkeypatch) -> None:
    """An empty/whitespace value must not count -- a blanked-out inherited
    env var is the standalone-CLI case, not server context."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "")
    assert is_server_refresh_context() is False


# ---------------------------------------------------------------------------
# Structural golden-repo coordinate resolution
# ---------------------------------------------------------------------------


def test_flat_golden_repo_clone_resolves_its_coordinates(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    assert resolve_golden_repo_coordinates(codebase_dir) == (
        golden_repos_dir,
        REPO_ALIAS,
    )


def test_versioned_golden_repo_clone_resolves_the_same_coordinates(
    tmp_path: Path,
) -> None:
    """A versioned snapshot path must recover the SAME (golden_repos_dir,
    alias) as the flat clone -- otherwise the same repo would resolve two
    different temporal roots depending on which shape indexing ran against."""
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / ".versioned" / REPO_ALIAS / "v_1754000000"
    codebase_dir.mkdir(parents=True)

    assert resolve_golden_repo_coordinates(codebase_dir) == (
        golden_repos_dir,
        REPO_ALIAS,
    )


def test_an_ordinary_user_repo_resolves_no_coordinates(tmp_path: Path) -> None:
    """The overwhelmingly common standalone-CLI case: a user's own working
    repo is NOT a golden repo clone and must never be given a sister root."""
    codebase_dir = tmp_path / "some" / "user" / "project"
    codebase_dir.mkdir(parents=True)

    assert resolve_golden_repo_coordinates(codebase_dir) is None


def test_versioned_shape_without_a_version_leaf_is_rejected(tmp_path: Path) -> None:
    """`.versioned/<alias>` with no `v_*` leaf is not a real snapshot path --
    structural detection must not accept a lookalike."""
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / ".versioned" / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    # Not a v_* leaf: the parent is `.versioned`, which is not a golden repo.
    assert resolve_golden_repo_coordinates(codebase_dir) is None


# ---------------------------------------------------------------------------
# Deterministic fixed-path construction
# ---------------------------------------------------------------------------


def test_temporal_root_is_deterministic_and_outside_the_repo_tree(
    tmp_path: Path,
) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS

    root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)

    # Deterministic: same inputs, same answer, every time.
    assert root == server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    # Outside the golden repo's OWN tree -- the whole point: a CoW clone of
    # codebase_dir can never carry temporal data.
    assert not root.is_relative_to(codebase_dir)
    assert root.is_relative_to(golden_repos_dir)
    assert SERVER_TEMPORAL_ROOT_DIR_NAME in root.parts


def test_global_suffix_is_normalized_to_one_root(tmp_path: Path) -> None:
    """A query may arrive with the '-global' query-facing alias while the
    write side only ever knows the bare directory name -- both must resolve
    to the SAME physical root or reads miss the written data."""
    golden_repos_dir = tmp_path / "golden-repos"

    assert server_temporal_index_root(
        golden_repos_dir, f"{REPO_ALIAS}-global"
    ) == server_temporal_index_root(golden_repos_dir, REPO_ALIAS)


def test_distinct_repos_never_share_a_temporal_root(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    assert server_temporal_index_root(
        golden_repos_dir, "repo-a"
    ) != server_temporal_index_root(golden_repos_dir, "repo-b")


def test_write_side_and_read_side_derive_the_identical_root(tmp_path: Path) -> None:
    """The contract that closes #1529: the child subprocess (which only has
    a codebase_dir) and the server (which only has alias + golden_repos_dir)
    MUST land on the same directory."""
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    write_side = resolve_server_temporal_index_root_for_codebase(codebase_dir)
    read_side = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)

    assert write_side == read_side


def test_no_temporal_root_for_a_non_golden_codebase(tmp_path: Path) -> None:
    codebase_dir = tmp_path / "plain" / "repo"
    codebase_dir.mkdir(parents=True)
    assert resolve_server_temporal_index_root_for_codebase(codebase_dir) is None


# ---------------------------------------------------------------------------
# The shared seam
# ---------------------------------------------------------------------------


def test_seam_redirects_a_golden_repo_in_server_context(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    resolved = resolve_temporal_index_dir(codebase_dir)

    assert resolved == server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    assert not resolved.is_relative_to(codebase_dir)


def test_seam_is_in_repo_for_standalone_cli(tmp_path: Path, monkeypatch) -> None:
    """No server marker: byte-identical to pre-#1529 behavior, even for a
    path that happens to sit under a golden-repos directory."""
    monkeypatch.delenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, raising=False)
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    assert resolve_temporal_index_dir(codebase_dir) == (
        codebase_dir / ".code-indexer" / "index"
    )


def test_seam_is_in_repo_for_a_non_golden_repo_even_in_server_context(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    codebase_dir = tmp_path / "plain" / "repo"
    codebase_dir.mkdir(parents=True)

    assert resolve_temporal_index_dir(codebase_dir) == (
        codebase_dir / ".code-indexer" / "index"
    )
