"""Bug #1529 review finding #2: read seams must not silently fall back local.

Both temporal read seams resolved the fixed temporal root inside a bare
``except Exception: return None`` and then let the caller fall back to
``repo_path`` -- the ACTIVATION'S OWN CoW clone. That silently reintroduces
the exact duplication/staleness hazard this whole bug exists to close: the
query reads a frozen-at-clone-time copy that diverges from the golden repo on
every refresh, and (before this fix) it did so with no error at all.

The distinction that matters is NOT the ``CIDX_SERVER_REFRESH_CONTEXT`` env
marker. That marker is injected only into the temporal CHILD SUBPROCESS by
``build_temporal_child_env``; it is absent from the server's own process, and
both of these seams run IN the server. Gating on it would make the fix
permanently inert -- the same half-wiring class as the original bug. The
correct discriminator is whether GOLDEN LINEAGE IS KNOWN:

  - golden alias known  -> the fixed root is the ONLY correct location, so a
    failure deriving it MUST raise. Reading the activation's local clone would
    be silently wrong.
  - golden alias absent -> genuinely no lineage (explicit-repo_path query
    shape, composite repo). Falling back is correct and stays fail-open.

The failure is forced with a REAL, deterministic trigger -- an alias that
finding #4's validation refuses -- and the worker seam is reached through a
genuine ``-global`` alias, which ``_resolve_golden_repo_alias`` returns
verbatim with no lookup. No production function is patched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager

#: Refused by server_temporal_index_root's alias-component guard (finding #4),
#: so resolution genuinely fails without stubbing anything out.
UNRESOLVABLE_ALIAS = "../escape"

#: The is_global form of the same alias. ``_resolve_golden_repo_alias`` returns
#: any ``-global`` alias as its own golden alias with NO backend lookup, so
#: this reaches the "lineage known, root underivable" branch directly.
UNRESOLVABLE_GLOBAL_ALIAS = "../escape-global"


class _StubActivatedRepoManager:
    """Minimal stand-in for the ONE attribute the seam reads."""

    def __init__(self, activated_repos_dir: str) -> None:
        self.activated_repos_dir = activated_repos_dir


def _manager(tmp_path: Path) -> SemanticQueryManager:
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.activated_repo_manager = _StubActivatedRepoManager(  # type: ignore[attr-defined]
        str(tmp_path / "data" / "activated-repos")
    )
    return manager


def test_known_alias_with_unresolvable_root_raises(tmp_path: Path) -> None:
    """A KNOWN golden alias whose root cannot be derived must fail loud."""
    manager = _manager(tmp_path)

    with pytest.raises(Exception) as exc_info:
        manager._resolve_temporal_index_dir(UNRESOLVABLE_ALIAS)

    # Not a silent None -- and the error must identify the offending alias so
    # an operator can act on it.
    assert UNRESOLVABLE_ALIAS in str(exc_info.value)


def test_absent_alias_still_fails_open_to_none(tmp_path: Path) -> None:
    """No golden lineage is a LEGITIMATE shape, not an error."""
    manager = _manager(tmp_path)

    assert manager._resolve_temporal_index_dir(None) is None
    assert manager._resolve_temporal_index_dir("") is None


def test_known_resolvable_alias_returns_the_fixed_root(tmp_path: Path) -> None:
    """The ordinary path is unchanged."""
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    manager = _manager(tmp_path)
    expected = server_temporal_index_root(
        tmp_path / "data" / "golden-repos", "evolution"
    )

    assert manager._resolve_temporal_index_dir("evolution") == expected


def test_temporal_worker_known_alias_with_unresolvable_root_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP worker seam -- the primary live temporal read path -- too.

    Story #1400 made ``run_temporal_worker`` the live MCP temporal front door,
    so leaving only the REST seam fail-loud would leave the PRIMARY read path
    silently reading the activation's local clone.
    """
    from code_indexer.server.services.temporal_worker import (
        _resolve_golden_temporal_context,
    )

    # Real configuration, not a patched production function: the worker builds
    # its own ActivatedRepoManager from this env var (Bug #1517).
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server"))

    class _Input:
        username = "someone"
        repository_alias = UNRESOLVABLE_GLOBAL_ALIAS
        repo_path = str(tmp_path / "clone")

    with pytest.raises(Exception) as exc_info:
        _resolve_golden_temporal_context(_Input(), "job-1529")

    assert UNRESOLVABLE_ALIAS in str(exc_info.value)


def test_temporal_worker_absent_lineage_still_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-global alias with no activated-repo record has no lineage.

    That is the legitimate fail-open case and must NOT raise -- otherwise this
    fix would break every composite/explicit-path temporal query.
    """
    from code_indexer.server.services.temporal_worker import (
        _resolve_golden_temporal_context,
    )

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server"))

    class _Input:
        username = "someone"
        repository_alias = "no-such-activated-repo"
        repo_path = str(tmp_path / "clone")

    context = _resolve_golden_temporal_context(_Input(), "job-1529")

    assert context.alias is None
    assert context.temporal_index_dir is None
