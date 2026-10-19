"""Catalog detection by the searched repository's golden identity.

The request alias may be a custom activation alias, absent, or merely
contain the catalog's name; detection must use the golden identity of the
repository actually served. Neutral names only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.cidx_meta_access import (
    UNRESOLVED_IDENTITY,
    is_internal_meta_identity,
    meta_identity,
    request_alias_identities,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    MEMORY_ALPHA,
    MEMORY_CANARY,
    MEMORY_GLOBAL,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

from .conftest import extract_mcp_data, make_file_info


class _Activations:
    """The caller's activation records, as ActivatedRepoManager lists them."""

    def __init__(self, records: List[Dict[str, Any]]):
        self._records = records
        self.list_calls = 0
        self.get_calls = 0

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return list(self._records)

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        self.get_calls += 1
        return next((r for r in self._records if r["user_alias"] == user_alias), None)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return any(r["user_alias"] == alias for r in self._records)


CATALOG_ACTIVATION = {
    "user_alias": "team-catalog",
    "golden_repo_alias": "cidx-meta",
    "is_composite": False,
}


def test_identity_of_each_searched_repository_shape() -> None:
    assert meta_identity({"user_alias": "cidx-meta-global", "is_global": True}) == (
        "cidx-meta-global",
    )
    assert meta_identity(
        {"user_alias": "x-global", "repo_path": "/p", "actual_repo_id": "cidx-meta"}
    ) == ("cidx-meta",)
    assert meta_identity({"user_alias": "x-global", "repo_path": "/p"}) == ("x-global",)
    assert meta_identity(CATALOG_ACTIVATION) == ("cidx-meta",)
    assert meta_identity(
        {
            "user_alias": "combo",
            "is_composite": True,
            "golden_repo_aliases": [ALPHA, "cidx-meta"],
        }
    ) == (ALPHA, "cidx-meta")
    assert meta_identity({"user_alias": "orphan"}) == (UNRESOLVED_IDENTITY,)


def test_activation_of_catalog_under_custom_alias_is_detected() -> None:
    my_notes = {"user_alias": "my-notes", "golden_repo_alias": "cidx-meta-global"}
    composite = {
        "user_alias": "bundle",
        "is_composite": True,
        "golden_repo_aliases": [ALPHA, "cidx-meta"],
    }
    assert is_internal_meta_identity(meta_identity(CATALOG_ACTIVATION)) is True
    assert is_internal_meta_identity(meta_identity(my_notes)) is True
    assert is_internal_meta_identity(meta_identity(composite)) is True
    assert is_internal_meta_identity(("cidx-metadata",)) is False
    assert is_internal_meta_identity(("cidx-metadata-global",)) is False
    assert is_internal_meta_identity((ALPHA,)) is False


def test_path_shaped_alias_is_its_own_identity() -> None:
    class _Failing(_Activations):
        def get_repository(
            self, username: str, user_alias: str, *, touch: bool = True
        ) -> Optional[Dict[str, Any]]:
            self.get_calls += 1
            raise RuntimeError("store unavailable")

    failing = _Failing([])
    assert request_alias_identities("/srv/repos/sample", "u", failing) == (
        "/srv/repos/sample",
    )
    assert failing.get_calls == 0
    assert request_alias_identities("team-catalog", "u", failing) == (
        UNRESOLVED_IDENTITY,
    )
    assert is_internal_meta_identity((UNRESOLVED_IDENTITY,)) is True


def test_unresolvable_identity_is_treated_as_the_catalog() -> None:
    assert is_internal_meta_identity(meta_identity({"user_alias": "orphan"})) is True
    assert (
        is_internal_meta_identity(
            meta_identity({"user_alias": "c", "is_composite": True})
        )
        is True
    )
    assert is_internal_meta_identity(()) is True
    assert is_internal_meta_identity(request_alias_identities("x", "u", None)) is True


def test_request_alias_resolves_through_the_callers_activation() -> None:
    activations = _Activations([CATALOG_ACTIVATION])
    assert request_alias_identities("team-catalog", "u", activations) == ("cidx-meta",)
    assert request_alias_identities("cidx-meta-global", "u", activations) == (
        "cidx-meta-global",
    )
    assert request_alias_identities(ALPHA, "u", activations) == (ALPHA,)
    assert request_alias_identities("team-catalog", "u", None) == (UNRESOLVED_IDENTITY,)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


_LISTED = [
    f"{ALPHA}.md",
    f"{CANARY}.md",
    "gamma.md",
    "README.md",
    "dependency-map/payments.md",
    f"memories/{MEMORY_GLOBAL}.md",
    f"memories/{MEMORY_ALPHA}.md",
    f"memories/{MEMORY_CANARY}.md",
]


def _list_files(
    alias: str, activations: _Activations, access_service: Optional[Any]
) -> List[str]:
    from code_indexer.server.mcp.handlers import files as files_module

    user = User(
        username=RESTRICTED_USER,
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )
    file_service = MagicMock()
    file_service.list_files.return_value = MagicMock(
        files=[make_file_info(p) for p in _LISTED]
    )
    app_module = MagicMock()
    app_module.file_service = file_service
    app_module.activated_repo_manager = activations
    app_module.golden_repo_manager = None
    with (
        patch.object(files_module._utils, "app_module", app_module),
        patch.object(
            files_module, "_get_access_filtering_service", return_value=access_service
        ),
    ):
        result = files_module.list_files({"repository_alias": alias}, user)
    data = extract_mcp_data(result)
    assert data["success"] is True, data
    return [f["path"] for f in data["files"]]


def test_list_files_on_catalog_activation_with_custom_alias_is_restricted(
    fx: CidxMetaFixture,
) -> None:
    paths = _list_files(
        "team-catalog", _Activations([CATALOG_ACTIVATION]), fx.access_service
    )
    assert paths == [
        f"{ALPHA}.md",
        f"memories/{MEMORY_GLOBAL}.md",
        f"memories/{MEMORY_ALPHA}.md",
    ]


def test_list_files_on_repo_whose_name_contains_catalog_is_not_filtered(
    fx: CidxMetaFixture,
) -> None:
    activation = {
        "user_alias": "cidx-metadata",
        "golden_repo_alias": "cidx-metadata",
        "is_composite": False,
    }
    paths = _list_files("cidx-metadata", _Activations([activation]), fx.access_service)
    assert paths == _LISTED


def test_list_files_on_catalog_fails_closed_without_access_service() -> None:
    assert _list_files("team-catalog", _Activations([CATALOG_ACTIVATION]), None) == []


def _restricted_user() -> User:
    return User(
        username=RESTRICTED_USER,
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _app_module(activations: _Activations) -> MagicMock:
    app_module = MagicMock()
    app_module.activated_repo_manager = activations
    app_module.golden_repo_manager = None
    app_module.file_service.list_files_by_path.return_value = MagicMock(
        files=[make_file_info(p) for p in _LISTED]
    )
    return app_module


@pytest.mark.parametrize(
    "file_path, denied",
    [
        ("dependency-map/payments.md", True),
        ("gamma.md", True),
        (f"memories/{MEMORY_CANARY}.md", True),
        (f"{ALPHA}.md", False),
    ],
)
def test_get_file_content_on_catalog_uses_full_path_and_identity(
    fx: CidxMetaFixture, file_path: str, denied: bool
) -> None:
    from code_indexer.server.mcp.handlers import files as files_module

    app_module = _app_module(_Activations([CATALOG_ACTIVATION]))
    app_module.file_service.get_file_content.return_value = {
        "content": "x",
        "metadata": {},
    }
    with (
        patch.object(files_module._utils, "app_module", app_module),
        patch.object(
            files_module,
            "_get_access_filtering_service",
            return_value=fx.access_service,
        ),
    ):
        result = files_module.get_file_content(
            {"repository_alias": "team-catalog", "file_path": file_path},
            _restricted_user(),
        )
    data = extract_mcp_data(result)
    is_denied = "Access denied" in str(data.get("error", ""))
    assert is_denied is denied
    assert app_module.file_service.get_file_content.called is (not denied)


def test_browse_directory_on_global_catalog_is_restricted(fx: CidxMetaFixture) -> None:
    from code_indexer.server.mcp.handlers import files as files_module

    app_module = _app_module(_Activations([]))
    with (
        patch.object(files_module._utils, "app_module", app_module),
        patch.object(
            files_module,
            "_get_access_filtering_service",
            return_value=fx.access_service,
        ),
        patch.object(
            files_module,
            "_resolve_global_repo_target",
            return_value=(str(fx.root), None),
        ),
    ):
        result = files_module.browse_directory(
            {"repository_alias": "cidx-meta-global"}, _restricted_user()
        )
    data = extract_mcp_data(result)
    assert data["success"] is True, data
    paths = [f["path"] for f in data["structure"]["files"]]
    assert paths == [
        f"{ALPHA}.md",
        f"memories/{MEMORY_GLOBAL}.md",
        f"memories/{MEMORY_ALPHA}.md",
    ]


def _real_tree(fx: CidxMetaFixture) -> Any:
    from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

    return DirectoryExplorerService(fx.root).generate_tree(max_depth=1)


def test_directory_tree_filter_uses_full_node_paths(fx: CidxMetaFixture) -> None:
    from code_indexer.server.mcp.handlers import files as files_module

    with patch.object(
        files_module, "_get_access_filtering_service", return_value=fx.access_service
    ):
        result = files_module._filter_cidx_meta_tree(_real_tree(fx), _restricted_user())
    files = [n.path for n in result.root.children if not n.is_directory]
    assert files == [f"{ALPHA}.md"]
    assert CANARY not in result.tree_string
    assert "README.md" not in result.tree_string

    with patch.object(files_module, "_get_access_filtering_service", return_value=None):
        closed = files_module._filter_cidx_meta_tree(_real_tree(fx), _restricted_user())
    assert [n for n in closed.root.children if not n.is_directory] == []


def test_directory_tree_filter_prunes_nested_paths(fx: CidxMetaFixture) -> None:
    from code_indexer.global_repos.directory_explorer import (
        DirectoryExplorerService,
        TreeNode,
    )
    from code_indexer.server.mcp.handlers import files as files_module
    from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER, GAMMA

    def _walk(node: TreeNode, files: List[str], dirs: List[str]) -> None:
        for child in node.children or []:
            if child.is_directory:
                dirs.append(child.path)
                _walk(child, files, dirs)
            else:
                files.append(child.path)

    with patch.object(
        files_module, "_get_access_filtering_service", return_value=fx.access_service
    ):
        tree = DirectoryExplorerService(fx.root).generate_tree(max_depth=4)
        result = files_module._filter_cidx_meta_tree(tree, _restricted_user())
        admin_tree = DirectoryExplorerService(fx.root).generate_tree(max_depth=4)
        admin_string = admin_tree.tree_string
        admin = User(
            username=ADMIN_USER,
            password_hash="hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(),
        )
        admin_result = files_module._filter_cidx_meta_tree(admin_tree, admin)

    files: List[str] = []
    dirs: List[str] = []
    _walk(result.root, files, dirs)
    assert sorted(files) == sorted(
        [
            f"{ALPHA}.md",
            "dependency-map/_analysis_guidelines.md",
            "dependency-map/_dep_types.md",
            f"memories/{MEMORY_ALPHA}.md",
            f"memories/{MEMORY_GLOBAL}.md",
            "xray-patterns/__any__/p.yaml",
        ]
    )
    assert sorted(dirs) == [
        "dependency-map",
        "memories",
        "xray-patterns",
        "xray-patterns/__any__",
    ]
    assert result.total_files == len(files)
    assert result.total_directories == len(dirs)
    for hidden in (CANARY, GAMMA, "payments", "_index.md", MEMORY_CANARY):
        assert hidden not in result.tree_string
    assert admin_result.tree_string == admin_string


async def test_regex_catalog_detection_runs_off_the_event_loop(
    fx: CidxMetaFixture,
) -> None:
    import threading

    loop_thread = threading.get_ident()
    seen: List[int] = []

    class _Recording(_Activations):
        def get_repository(
            self, username: str, user_alias: str, *, touch: bool = True
        ) -> Optional[Dict[str, Any]]:
            seen.append(threading.get_ident())
            return super().get_repository(username, user_alias, touch=touch)

    kept = await _filter_regex(
        "team-catalog", fx.access_service, _Recording([CATALOG_ACTIVATION])
    )
    assert kept == [
        f"{ALPHA}.md",
        f"memories/{MEMORY_GLOBAL}.md",
        f"memories/{MEMORY_ALPHA}.md",
    ]
    assert seen and all(thread != loop_thread for thread in seen)


async def _filter_regex(
    alias: str,
    access_service: Optional[Any],
    activations: Optional[_Activations] = None,
) -> List[str]:
    from code_indexer.server.mcp.handlers.search import regex_search

    matches = [{"file_path": p, "line_content": "x"} for p in _LISTED]
    with (
        patch.object(
            regex_search._utils,
            "app_module",
            _app_module(activations or _Activations([CATALOG_ACTIVATION])),
        ),
        patch.object(
            regex_search, "_get_access_filtering_service", return_value=access_service
        ),
        patch.object(
            regex_search._mcp_reranking,
            "_apply_reranking_sync",
            side_effect=lambda **kw: (kw["results"], {}),
        ),
        patch.object(
            regex_search,
            "_apply_regex_payload_truncation",
            side_effect=lambda m, owner: m,
        ),
    ):
        kept, meta = await regex_search._rerank_truncate_and_filter_regex_matches(
            matches, {}, alias, _restricted_user()
        )
    assert meta["cidx_meta_access_filtered"] is True
    return [m["file_path"] for m in kept]


async def test_regex_matches_on_catalog_activation_are_restricted_by_full_path(
    fx: CidxMetaFixture,
) -> None:
    assert await _filter_regex("team-catalog", fx.access_service) == [
        f"{ALPHA}.md",
        f"memories/{MEMORY_GLOBAL}.md",
        f"memories/{MEMORY_ALPHA}.md",
    ]
    assert await _filter_regex("team-catalog", None) == []


def test_no_substring_catalog_checks_remain() -> None:
    server_root = (
        Path(__file__).resolve().parents[4] / "src" / "code_indexer" / "server"
    )
    offenders = []
    for source in server_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for needle in (
            '"cidx-meta" in',
            '"cidx-meta" not in',
            "filter_cidx_meta_results",
        ):
            if needle in text:
                offenders.append(f"{source.relative_to(server_root)}: {needle}")
    assert offenders == []


def _tree_call(
    fx: CidxMetaFixture, args: Dict[str, Any], activations: _Activations
) -> Dict[str, Any]:
    from types import SimpleNamespace

    from code_indexer.server.mcp.handlers import files as files_module

    legacy = SimpleNamespace(_resolve_repo_path=lambda alias, root: str(fx.root))
    with (
        patch.object(files_module._utils, "app_module", _app_module(activations)),
        patch.object(
            files_module,
            "_get_access_filtering_service",
            return_value=fx.access_service,
        ),
        patch.object(files_module, "_get_golden_repos_dir", return_value="/unused"),
        patch.object(files_module, "_get_legacy", return_value=legacy),
    ):
        result = files_module.handle_directory_tree(args, _restricted_user())
    data = extract_mcp_data(result)
    assert data["success"] is True, data
    return data


def test_directory_tree_on_alias_spelled_as_catalog_is_restricted(
    fx: CidxMetaFixture,
) -> None:
    # The caller named an activation of another repository like the catalog;
    # the tree served for that alias is the catalog itself.
    misleading = {
        "user_alias": "cidx-meta",
        "golden_repo_alias": ALPHA,
        "is_composite": False,
    }
    data = _tree_call(
        fx,
        {"repository_alias": "cidx-meta", "max_depth": 4},
        _Activations([misleading]),
    )
    assert CANARY not in data["tree_string"]
    assert f"{ALPHA}.md" in data["tree_string"]


def test_directory_tree_subdirectory_is_judged_by_full_path(
    fx: CidxMetaFixture,
) -> None:
    (fx.dep_map_dir / f"{ALPHA}.md").write_text("# stray\n", encoding="utf-8")
    data = _tree_call(
        fx,
        {"repository_alias": "cidx-meta-global", "path": "dependency-map"},
        _Activations([]),
    )
    names = [child["name"] for child in data["root"]["children"]]
    assert f"{ALPHA}.md" not in names
    assert "_dep_types.md" in names
    assert "payments.md" not in names


def test_catalog_detection_reads_one_activation_and_none_for_admins(
    fx: CidxMetaFixture,
) -> None:
    from code_indexer.server.mcp.handlers import files as files_module
    from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER

    restricted_activations = _Activations([CATALOG_ACTIVATION])
    _list_files("team-catalog", restricted_activations, fx.access_service)
    assert restricted_activations.get_calls == 1
    assert restricted_activations.list_calls == 0

    admin_activations = _Activations([CATALOG_ACTIVATION])
    admin = User(
        username=ADMIN_USER,
        password_hash="hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )
    file_service = MagicMock()
    file_service.list_files.return_value = MagicMock(
        files=[make_file_info(p) for p in _LISTED]
    )
    app_module = MagicMock()
    app_module.file_service = file_service
    app_module.activated_repo_manager = admin_activations
    app_module.golden_repo_manager = None
    with (
        patch.object(files_module._utils, "app_module", app_module),
        patch.object(
            files_module,
            "_get_access_filtering_service",
            return_value=fx.access_service,
        ),
    ):
        result = files_module.list_files({"repository_alias": "team-catalog"}, admin)
    assert [f["path"] for f in extract_mcp_data(result)["files"]] == _LISTED
    assert admin_activations.get_calls == 0
    assert admin_activations.list_calls == 0
