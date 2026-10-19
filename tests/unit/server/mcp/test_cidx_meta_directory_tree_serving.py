"""directory_tree serves the repository the caller is authorised for, and a
restricted caller cannot tell a hidden catalog path from a missing one.

Real catalog fixture on disk, real alias registry files read by the real
legacy resolver, real path policy and group store. Only the caller's
activation records stand in for the activation manager. Neutral names only.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files as files_module
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    MEMORY_ALPHA,
    MEMORY_GLOBAL,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)


class _Activations:
    """The caller's activation records and the directory of each."""

    def __init__(self, records: Dict[str, Dict[str, Any]], paths: Dict[str, Path]):
        self._records = records
        self._paths = paths

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        return self._records.get(user_alias)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return alias in self._records

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self._paths[user_alias])


_NO_ACTIVATIONS = _Activations({}, {})


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "catalog")


@pytest.fixture
def golden_root(tmp_path: Path, fx: CidxMetaFixture) -> Path:
    root = tmp_path / "golden-repos"
    _register(root, "cidx-meta", fx.root)
    return root


def _register(golden_root: Path, alias: str, target: Path) -> None:
    aliases = golden_root / "aliases"
    aliases.mkdir(parents=True, exist_ok=True)
    (aliases / f"{alias}-global.json").write_text(
        json.dumps({"target_path": str(target)}), encoding="utf-8"
    )


def _restricted_user() -> User:
    return User(
        username=RESTRICTED_USER,
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _tree_text(
    fx: CidxMetaFixture,
    golden_root: Path,
    args: Dict[str, Any],
    activations: _Activations = _NO_ACTIVATIONS,
    access_service: Any = None,
) -> str:
    """The raw MCP text block handle_directory_tree returns."""
    app_module = MagicMock()
    app_module.activated_repo_manager = activations
    app_module.golden_repo_manager = None
    with (
        patch.object(files_module._utils, "app_module", app_module),
        patch.object(
            files_module, "_get_golden_repos_dir", return_value=str(golden_root)
        ),
        patch.object(files_module, "_get_available_repos", return_value=[]),
        patch.object(
            files_module,
            "_get_access_filtering_service",
            return_value=access_service or fx.access_service,
        ),
    ):
        result = files_module.handle_directory_tree(dict(args), _restricted_user())
    text: str = result["content"][0]["text"]
    return text


def _tree(
    fx: CidxMetaFixture,
    golden_root: Path,
    args: Dict[str, Any],
    activations: _Activations = _NO_ACTIVATIONS,
) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(_tree_text(fx, golden_root, args, activations))
    return data


def test_directory_tree_serves_the_callers_activation_before_a_golden_repo(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    golden_gamma = tmp_path / "golden-gamma"
    golden_gamma.mkdir()
    (golden_gamma / "golden_only.txt").write_text("golden\n", encoding="utf-8")
    _register(golden_root, GAMMA, golden_gamma)

    activation_dir = tmp_path / "activation"
    activation_dir.mkdir()
    (activation_dir / "activation_only.txt").write_text("mine\n", encoding="utf-8")
    activations = _Activations(
        {GAMMA: {"user_alias": GAMMA, "golden_repo_alias": ALPHA}},
        {GAMMA: activation_dir},
    )

    data = _tree(fx, golden_root, {"repository_alias": GAMMA}, activations)

    assert data["success"] is True, data
    assert "activation_only.txt" in data["tree_string"]
    assert "golden_only.txt" not in json.dumps(data)


def test_activation_without_a_directory_is_not_found(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path
) -> None:
    golden_gamma = tmp_path / "golden-gamma"
    golden_gamma.mkdir()
    (golden_gamma / "golden_only.txt").write_text("golden\n", encoding="utf-8")
    _register(golden_root, GAMMA, golden_gamma)
    activations = _Activations(
        {GAMMA: {"user_alias": GAMMA, "golden_repo_alias": ALPHA}},
        {GAMMA: tmp_path / "removed-activation"},
    )

    data = _tree(fx, golden_root, {"repository_alias": GAMMA}, activations)

    assert data["success"] is False
    assert "not found" in data["error"]
    assert "golden_only.txt" not in json.dumps(data)


_MISSING = "no-such-directory"


def _catalog_path(path: str) -> Dict[str, Any]:
    return {"repository_alias": "cidx-meta-global", "path": path}


@pytest.mark.parametrize(
    "hidden",
    [
        f"xray-patterns/{CANARY}",
        "dependency-map/_journal.json",
        f"{CANARY}.md",
        ".code-indexer",
    ],
)
def test_hidden_catalog_path_is_answered_as_not_found(
    fx: CidxMetaFixture, golden_root: Path, hidden: str
) -> None:
    hidden_text = _tree_text(fx, golden_root, _catalog_path(hidden))
    missing_text = _tree_text(fx, golden_root, _catalog_path(_MISSING))

    assert json.loads(missing_text)["success"] is False
    assert hidden_text.replace(hidden, "<path>") == missing_text.replace(
        _MISSING, "<path>"
    )


@pytest.mark.parametrize(
    "visible, listed",
    [
        ("xray-patterns/__any__", "p.yaml"),
        ("xray-patterns", "__any__"),
        ("dependency-map", "_dep_types.md"),
        ("memories", MEMORY_GLOBAL),
        (f"{ALPHA}.md", f"{ALPHA}.md"),
    ],
)
def test_visible_catalog_path_is_listed(
    fx: CidxMetaFixture, golden_root: Path, visible: str, listed: str
) -> None:
    data = _tree(fx, golden_root, _catalog_path(visible))

    assert data["success"] is True, data
    assert listed in data["tree_string"]
    assert CANARY not in json.dumps(data)


def test_emptied_catalog_area_is_listed_empty(
    fx: CidxMetaFixture, golden_root: Path
) -> None:
    for memory_id in (MEMORY_GLOBAL, MEMORY_ALPHA):
        (fx.root / "memories" / f"{memory_id}.md").unlink()

    data = _tree(fx, golden_root, _catalog_path("memories"))

    assert data["success"] is True, data
    assert data["root"]["children"] == []
    assert data["total_files"] == 0


def test_max_depth_reached_counts_visible_directories_only(
    fx: CidxMetaFixture, golden_root: Path
) -> None:
    hidden_dir = fx.dep_map_dir / "archive" / "older"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    hidden = _tree(fx, golden_root, {**_catalog_path("dependency-map"), "max_depth": 1})
    visible = _tree(fx, golden_root, {**_catalog_path(""), "max_depth": 1})

    assert hidden["success"] is True, hidden
    assert "archive" not in json.dumps(hidden)
    assert hidden["max_depth_reached"] is False
    assert visible["max_depth_reached"] is True


def test_directory_tree_reads_grants_once(
    fx: CidxMetaFixture, golden_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    real = fx.access_service.get_accessible_repos

    def counting(user_id: str) -> Any:
        calls.append(user_id)
        return real(user_id)

    monkeypatch.setattr(fx.access_service, "get_accessible_repos", counting)

    data = _tree(fx, golden_root, _catalog_path(""))

    assert data["success"] is True, data
    assert calls == [RESTRICTED_USER]
