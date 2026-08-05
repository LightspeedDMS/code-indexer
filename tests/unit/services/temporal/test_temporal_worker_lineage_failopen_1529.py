"""Bug #1529 round-4 finding #2: the lineage LOOKUP must not fail open.

Round 2 fixed the PATH-DERIVATION half of the MCP temporal seam (deriving the
fixed root happens outside the fail-open try, and raises). The LOOKUP half was
left fail-open, and that alone reintroduces the whole hazard: any exception
became ``None``, ``None`` became an all-None context, and
``reconstruct_temporal_backend(repo_path, ...)`` then read the ACTIVATION'S own
CoW clone -- the frozen-at-clone-time duplicate this bug exists to eliminate.
This is the LIVE MCP temporal front door (Story #1400), so it is the primary
read path being silently wrong.

The two cases must be told apart, and ``ActivatedRepoManager.get_repository``
already distinguishes them for us:

  - GENUINE ABSENCE -> returns ``None`` (no activated-repo record, or a record
    with no ``golden_repo_alias``). There is legitimately no golden lineage,
    so ``None`` is right and the in-repo derivation is correct.
  - FAILURE -> raises (metadata load/refresh failed). "I could not determine
    the lineage" is NOT "there is no lineage", and must fail the job rather
    than quietly serve stale local data.

Only the EXTERNAL dependency is substituted here (the repo manager). Both
resolution functions under test run for real.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from code_indexer.server.services.temporal_worker import (
    _resolve_golden_repo_alias,
    _resolve_golden_temporal_context,
)

USERNAME = "alice"
ACTIVATED_ALIAS = "myclone"
GOLDEN_ALIAS = "evolution"
MANAGER_MODULE = "code_indexer.server.repositories.activated_repo_manager"


class _RaisingManager:
    """get_repository fails -- a real backend/metadata error."""

    activated_repos_dir = "/unused/activated-repos"

    def get_repository(self, username: str, user_alias: str, touch: bool = True):
        raise RuntimeError("metadata backend unavailable")


class _AbsentManager:
    """get_repository finds nothing -- genuine absence, not a failure."""

    activated_repos_dir = "/unused/activated-repos"

    def get_repository(
        self, username: str, user_alias: str, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        return None


class _NoLineageManager:
    """A real record that simply carries no golden_repo_alias."""

    activated_repos_dir = "/unused/activated-repos"

    def get_repository(
        self, username: str, user_alias: str, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        return {"user_alias": ACTIVATED_ALIAS}


class _WorkerInput:
    def __init__(self) -> None:
        self.username = USERNAME
        self.repository_alias = ACTIVATED_ALIAS
        self.repo_path = "/unused/activated-repos/alice/myclone"


def _install_manager(monkeypatch, manager_cls) -> None:
    """Substitute the EXTERNAL ActivatedRepoManager dependency.

    Patched at its DEFINING module because temporal_worker imports it locally
    inside the function under test, so a module-attribute patch on the worker
    would never be consulted.
    """
    import importlib

    manager_mod = importlib.import_module(MANAGER_MODULE)
    monkeypatch.setattr(
        manager_mod, "ActivatedRepoManager", lambda *a, **k: manager_cls()
    )


# ---------------------------------------------------------------------------
# A lookup FAILURE must propagate
# ---------------------------------------------------------------------------


def test_lookup_failure_propagates_instead_of_returning_none() -> None:
    """The core fix: an exception must NOT be laundered into 'no lineage'."""
    with pytest.raises(Exception) as exc_info:
        _resolve_golden_repo_alias(USERNAME, ACTIVATED_ALIAS, _RaisingManager())

    assert "metadata backend unavailable" in str(exc_info.value), (
        "the original backend error must reach the caller, not be swallowed "
        "into a None that permits an activation-local temporal read"
    )


def test_context_resolution_propagates_a_lookup_failure(monkeypatch) -> None:
    """One level up -- the all-None context is what actually sends the caller
    to the activation's own clone, so it must not be produced by a failure."""
    _install_manager(monkeypatch, _RaisingManager)

    with pytest.raises(Exception) as exc_info:
        _resolve_golden_temporal_context(_WorkerInput(), "job-1")

    assert "metadata backend unavailable" in str(exc_info.value)


# ---------------------------------------------------------------------------
# GENUINE absence is still a legitimate None (must NOT become an error)
# ---------------------------------------------------------------------------


def test_absent_activated_repo_record_is_a_legitimate_none() -> None:
    assert (
        _resolve_golden_repo_alias(USERNAME, ACTIVATED_ALIAS, _AbsentManager()) is None
    )


def test_record_without_golden_alias_is_a_legitimate_none() -> None:
    assert (
        _resolve_golden_repo_alias(USERNAME, ACTIVATED_ALIAS, _NoLineageManager())
        is None
    )


def test_context_resolution_tolerates_genuine_absence(monkeypatch) -> None:
    """No lineage is a legitimate outcome: alias None, no exception, and the
    caller keeps the in-repo derivation."""
    _install_manager(monkeypatch, _AbsentManager)

    ctx = _resolve_golden_temporal_context(_WorkerInput(), "job-2")

    assert ctx.alias is None
    assert ctx.temporal_index_dir is None


def test_global_alias_is_its_own_golden_alias_without_any_lookup() -> None:
    """An is_global alias needs no lookup at all, so it cannot fail open."""
    assert (
        _resolve_golden_repo_alias(
            USERNAME, f"{GOLDEN_ALIAS}-global", _RaisingManager()
        )
        == f"{GOLDEN_ALIAS}-global"
    )
