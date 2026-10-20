"""A real server environment for the catalog file doors.

The catalog fixture on disk, real alias pointer files, the real global-repos
registry, the real FileListingService and group store. Only the caller's
activation records and the golden-repo activity check stand in for their
managers. Neutral names only.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.file_service import FileListingService
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

CATALOG_ALIAS = "cidx-meta-global"
ALPHA_ALIAS = f"{ALPHA}-global"
CATALOG_COPY_ALIAS = "team-notes"


class Activations:
    """The caller's activation records and the directory of each."""

    def __init__(self, golden_repo_manager: "GoldenActivity") -> None:
        self.records: Dict[str, Dict[str, Any]] = {}
        self.paths: Dict[str, Path] = {}
        self.golden_repo_manager = golden_repo_manager

    def add(self, user_alias: str, golden: str, path: Path) -> None:
        self.records[user_alias] = {
            "user_alias": user_alias,
            "golden_repo_alias": golden,
            "is_composite": False,
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
        }
        self.paths[user_alias] = path

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        record = self.records.get(user_alias)
        return dict(record) if record else None

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return alias in self.records

    def _load_metadata(
        self, username: str, user_alias: str
    ) -> Optional[Dict[str, Any]]:
        return self.get_repository(username, user_alias)

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        path = self.paths.get(user_alias)
        return str(path) if path else ""

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.records.values()]


class GoldenActivity:
    """Which golden repositories are globally active (bare names)."""

    def __init__(self, active: List[str]) -> None:
        self.active = set(active)

    def is_globally_active(self, alias: str) -> bool:
        return alias in self.active

    def existing_golden_aliases(self, aliases: List[str]) -> List[str]:
        return [alias for alias in aliases if alias in self.active]


def user(name: str = RESTRICTED_USER, role: Optional[UserRole] = None) -> User:
    if role is None:
        role = UserRole.ADMIN if name == ADMIN_USER else UserRole.NORMAL_USER
    return User(
        username=name,
        password_hash="hash",
        role=role,
        created_at=datetime.now(),
    )


def point_alias(golden_root: Path, alias: str, target: Path) -> None:
    aliases = golden_root / "aliases"
    aliases.mkdir(parents=True, exist_ok=True)
    (aliases / f"{alias}.json").write_text(
        json.dumps({"target_path": str(target)}), encoding="utf-8"
    )


class CatalogFileDoorEnv:
    """Server state over the catalog fixture plus an ordinary ``alpha`` repo."""

    def __init__(self, tmp_path: Path) -> None:
        self.fx: CidxMetaFixture = build_cidx_meta_fixture(tmp_path / "data")
        self.golden_root = tmp_path / "data" / "golden-repos"
        self.alpha_dir = tmp_path / "alpha-repo"
        (self.alpha_dir / "src").mkdir(parents=True)
        (self.alpha_dir / "src" / "main.py").write_text("print('alpha')\n")
        point_alias(self.golden_root, CATALOG_ALIAS, self.fx.root)
        point_alias(self.golden_root, ALPHA_ALIAS, self.alpha_dir)
        golden_activity = GoldenActivity([ALPHA, "cidx-meta"])
        self.activations = Activations(golden_activity)
        self.file_service = FileListingService()
        self.file_service.activated_repo_manager = self.activations  # type: ignore[assignment]
        self.state = SimpleNamespace(
            golden_repos_dir=str(self.golden_root),
            backend_registry=SimpleNamespace(global_repos=self.fx.registry),
            access_filtering_service=self.fx.access_service,
            activated_repo_manager=self.activations,
            payload_cache=None,
        )
        self.app_module = SimpleNamespace(
            app=SimpleNamespace(state=self.state),
            file_service=self.file_service,
            activated_repo_manager=self.activations,
            golden_repo_manager=golden_activity,
        )

    def catalog_copy(
        self, tmp_path: Path, user_alias: str = CATALOG_COPY_ALIAS
    ) -> Path:
        """A pre-existing activation of the catalog under *user_alias*."""
        import shutil

        copy = tmp_path / "activated" / user_alias
        shutil.copytree(self.fx.root, copy)
        self.activations.add(user_alias, "cidx-meta", copy)
        return copy

    @contextmanager
    def serving(self) -> Iterator[None]:
        from code_indexer.server.mcp.handlers import _utils

        with patch.object(_utils, "app_module", self.app_module):
            yield


def mcp_data(result: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return data
