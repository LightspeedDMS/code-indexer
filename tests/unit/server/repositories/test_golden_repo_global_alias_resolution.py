"""resolve_global_clone_path: resolve either alias form onto the golden clone.

list_repositories publishes a globally-activated repo as "<alias>-global" while the
golden repo is registered under the bare "<alias>". Handlers handed the published
alias could not look it up, so they reported the repo as missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


class _Repo:
    def __init__(self, clone_path: str) -> None:
        self.clone_path = clone_path


def _manager(
    *, globally_active: bool, repo: Optional[_Repo], seen: Optional[list] = None
) -> Any:
    mgr = GoldenRepoManager.__new__(GoldenRepoManager)

    def _is_globally_active(alias: str) -> bool:
        if seen is not None:
            seen.append(alias)
        return globally_active

    mgr.is_globally_active = _is_globally_active  # type: ignore[method-assign]
    mgr.get_golden_repo = lambda alias: repo  # type: ignore[method-assign]
    return mgr


def test_suffixed_alias_resolves_to_the_golden_clone() -> None:
    mgr = _manager(globally_active=True, repo=_Repo("/golden/example-repo"))
    assert mgr.resolve_global_clone_path("example-repo-global") == Path(
        "/golden/example-repo"
    )


def test_bare_alias_resolves_too() -> None:
    mgr = _manager(globally_active=True, repo=_Repo("/golden/example-repo"))
    assert mgr.resolve_global_clone_path("example-repo") == Path("/golden/example-repo")


def test_suffix_is_stripped_before_the_activation_check() -> None:
    # The registry keys on the bare alias; querying it with the suffix still attached
    # would never match, which is the bug this resolves.
    seen: list = []
    mgr = _manager(globally_active=True, repo=_Repo("/golden/x"), seen=seen)
    mgr.resolve_global_clone_path("example-repo-global")
    assert seen == ["example-repo"]


def test_not_globally_active_returns_none() -> None:
    mgr = _manager(globally_active=False, repo=_Repo("/golden/example-repo"))
    assert mgr.resolve_global_clone_path("example-repo-global") is None


def test_unknown_repo_returns_none() -> None:
    mgr = _manager(globally_active=True, repo=None)
    assert mgr.resolve_global_clone_path("example-repo-global") is None


def test_empty_alias_returns_none() -> None:
    mgr = _manager(globally_active=True, repo=_Repo("/golden/x"))
    assert mgr.resolve_global_clone_path("") is None


def test_bare_suffix_only_alias_returns_none() -> None:
    # "-global" alone leaves an empty bare alias; it must not resolve to anything.
    mgr = _manager(globally_active=True, repo=_Repo("/golden/x"))
    assert mgr.resolve_global_clone_path("-global") is None


def test_repo_without_clone_path_returns_none() -> None:
    mgr = _manager(globally_active=True, repo=_Repo(""))
    assert mgr.resolve_global_clone_path("example-repo-global") is None
