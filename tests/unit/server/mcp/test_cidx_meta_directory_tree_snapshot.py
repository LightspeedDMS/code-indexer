"""directory_tree resolves a request once: the repository served and the
catalog decision come from one read of the caller's activation, and a bare
alias with no activation is served from the registry only when the caller is
granted that repository.

Real ActivatedRepoManager over an on-disk activations directory, real alias
registry files, real catalog fixture and group store. Neutral names only.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)
from tests.unit.server.mcp.test_cidx_meta_directory_tree_serving import (
    _register,
    _tree,
)


def _manager(root: Path) -> ActivatedRepoManager:
    """A real manager reading activations from *root* (file mode)."""
    root.mkdir(parents=True, exist_ok=True)
    manager = ActivatedRepoManager.__new__(ActivatedRepoManager)
    manager.activated_repos_dir = str(root.resolve())
    manager._pool = None
    manager.logger = logging.getLogger(__name__)
    return manager


def _activate(
    manager: ActivatedRepoManager,
    user_alias: str,
    golden: str,
    content: Optional[Path] = None,
) -> Path:
    user_dir = Path(manager.activated_repos_dir) / RESTRICTED_USER
    user_dir.mkdir(parents=True, exist_ok=True)
    repo = user_dir / user_alias
    if content is not None:
        shutil.copytree(content, repo)
    else:
        repo.mkdir()
        (repo / "activation_only.txt").write_text("mine\n", encoding="utf-8")
    (user_dir / f"{user_alias}_metadata.json").write_text(
        json.dumps(
            {
                "user_alias": user_alias,
                "golden_repo_alias": golden,
                "is_composite": False,
            }
        ),
        encoding="utf-8",
    )
    return repo


class _Counting(ActivatedRepoManager):
    """Counts every activation read the handler makes."""

    reads: List[str]

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        self.reads.append("get_repository")
        return super().get_repository(username, user_alias, touch=touch)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        self.reads.append("user_has_activated_repo")
        return super().user_has_activated_repo(username, alias)


class _DeactivatedDuringRequest(ActivatedRepoManager):
    """The caller's activation is removed right after its path is read, as a
    concurrent deactivation would do."""

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        path = super().get_activated_repo_path(username, user_alias)
        user_dir = Path(self.activated_repos_dir) / username
        (user_dir / f"{user_alias}_metadata.json").unlink(missing_ok=True)
        return path


def _cast(manager: ActivatedRepoManager, cls: type) -> Any:
    manager.__class__ = cls
    return manager


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "catalog")


@pytest.fixture
def golden_root(tmp_path: Path, fx: CidxMetaFixture) -> Path:
    root = tmp_path / "golden-repos"
    _register(root, "cidx-meta", fx.root)
    return root


def _golden(tmp_path: Path, golden_root: Path, alias: str) -> None:
    repo = tmp_path / f"golden-{alias}"
    repo.mkdir()
    (repo / "golden_only.txt").write_text("golden\n", encoding="utf-8")
    _register(golden_root, alias, repo)


def test_catalog_decision_comes_from_the_read_that_chose_the_directory(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    manager = _manager(tmp_path / "activated-repos")
    _activate(manager, "team-catalog", "cidx-meta", content=fx.root)
    racing = _cast(manager, _DeactivatedDuringRequest)

    data = _tree(fx, golden_root, {"repository_alias": "team-catalog"}, racing)

    assert data["success"] is True, data
    assert f"{ALPHA}.md" in data["tree_string"]
    assert CANARY not in json.dumps(data)


def test_bare_alias_without_activation_serves_only_a_granted_repository(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    manager = _manager(tmp_path / "activated-repos")
    _golden(tmp_path, golden_root, GAMMA)
    _golden(tmp_path, golden_root, ALPHA)

    ungranted = _tree(fx, golden_root, {"repository_alias": GAMMA}, manager)
    granted = _tree(fx, golden_root, {"repository_alias": ALPHA}, manager)

    assert ungranted["success"] is False
    assert ungranted["error"] == f"Repository '{GAMMA}' not found"
    assert "golden_only.txt" not in json.dumps(ungranted)
    assert granted["success"] is True, granted
    assert "golden_only.txt" in granted["tree_string"]


class _UnreadableStore(ActivatedRepoManager):
    """The activation store cannot be read."""

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        raise OSError("activation store unavailable")


def test_unreadable_activation_store_answers_not_found(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    _golden(tmp_path, golden_root, ALPHA)
    unreadable = _cast(_manager(tmp_path / "activated-repos"), _UnreadableStore)

    data = _tree(fx, golden_root, {"repository_alias": ALPHA}, unreadable)

    assert data["success"] is False
    assert data["error"] == f"Repository '{ALPHA}' not found"
    assert "golden_only.txt" not in json.dumps(data)


def test_activation_is_read_once_per_request(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    manager = _manager(tmp_path / "activated-repos")
    _activate(manager, "team-catalog", "cidx-meta", content=fx.root)
    counting = _cast(manager, _Counting)
    counting.reads = []

    data = _tree(fx, golden_root, {"repository_alias": "team-catalog"}, counting)

    assert data["success"] is True, data
    assert counting.reads == ["get_repository"]
