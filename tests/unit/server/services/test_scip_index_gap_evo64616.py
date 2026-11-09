"""issue-64616: a SCIP query on a repository whose index is absent or partial
says so, instead of answering ``success`` with 0 results.

tools-test: ``example-repo`` (a Gradle monorepo, root ``settings.gradle``) had
only a 3-file C# solution under ``code/util/nativeapp`` indexed -- the root
Gradle build failed in the sandbox -- so ``scip_definition DealTax`` answered
success / 0 results with nothing to tell it from "no such class".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock

import pytest

from cidx_scip.server.mcp import scip as scip_handlers
from cidx_scip.server import build_status as scip_build_status
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from cidx_scip.server.query_service import (
    ScipIndexGap,
    SCIPQueryService,
)

ALIAS = "example-repo-global"


def _access(repos: Set[str]) -> MagicMock:
    access = MagicMock(spec=AccessFilteringService)
    access.get_accessible_repos.return_value = repos
    return access


def _repo(golden: Path, *, root_build: bool, dbs: List[str]) -> SCIPQueryService:
    repo = golden / "example-repo"
    repo.mkdir(parents=True)
    if root_build:
        (repo / "settings.gradle").write_text("include 'code'\n")
    for rel in dbs:
        db = repo / ".code-indexer" / "scip" / rel / "index.scip.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")
    return SCIPQueryService(golden, access_filtering_service=_access({"example-repo"}))


def test_no_scip_database_is_a_missing_index(tmp_path: Path) -> None:
    service = _repo(tmp_path, root_build=True, dbs=[])
    gap = service.describe_index_gap(ALIAS, "alice")
    assert gap is not None and gap.missing
    assert "No SCIP index exists for 'example-repo-global'" in gap.message


def test_root_build_without_top_level_index_is_partial(tmp_path: Path) -> None:
    service = _repo(
        tmp_path,
        root_build=True,
        dbs=["code/util/nativeapp", "code/util/nativeapp/NativeApp"],
    )
    gap = service.describe_index_gap(ALIAS, "alice")
    assert gap is not None and not gap.missing
    assert "incomplete" in gap.message
    assert "2 sub-project index(es)" in gap.message
    assert "code/util/nativeapp, code/util/nativeapp/NativeApp" in gap.message


def test_top_level_index_is_complete(tmp_path: Path) -> None:
    service = _repo(tmp_path, root_build=True, dbs=[".", "code/util/nativeapp"])
    assert service.describe_index_gap(ALIAS, "alice") is None


def test_per_project_repo_without_root_build_is_complete(tmp_path: Path) -> None:
    service = _repo(tmp_path, root_build=False, dbs=["svc-a", "svc-b"])
    assert service.describe_index_gap(ALIAS, "alice") is None


def test_unknown_repo_is_no_gap(tmp_path: Path) -> None:
    service = _repo(tmp_path, root_build=True, dbs=[])
    service.access_filtering_service = _access({"example-repo", "unknown"})
    assert service.describe_index_gap("unknown-global", "alice") is None


def test_inaccessible_repo_reveals_nothing(tmp_path: Path) -> None:
    _repo(tmp_path, root_build=True, dbs=[])
    service = SCIPQueryService(tmp_path, access_filtering_service=_access(set()))
    assert service.describe_index_gap(ALIAS, "mallory") is None


def test_failed_root_check_invents_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _repo(tmp_path, root_build=True, dbs=["code/util/nativeapp"])

    def boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("git-mcp down")

    monkeypatch.setattr("cidx_scip.server.repo_reads.repo_has_root_build_file", boom)
    assert service.describe_index_gap(ALIAS, "alice") is None


# -- the MCP response ------------------------------------------------------------


class _Service:
    def __init__(self, gap: Optional[ScipIndexGap]) -> None:
        self.gap = gap

    def describe_index_gap(self, alias: str, username: str) -> Optional[ScipIndexGap]:
        return self.gap


def _explain(
    monkeypatch: pytest.MonkeyPatch,
    payload: Dict[str, Any],
    gap: Optional[ScipIndexGap],
    build: Optional[str] = None,
) -> Dict[str, Any]:
    monkeypatch.setattr(scip_handlers, "_get_scip_query_service", lambda: _Service(gap))
    monkeypatch.setattr(scip_build_status, "explain_empty_results", lambda a: build)
    scip_handlers._explain_no_results(payload, ALIAS, "alice")
    return payload


def test_missing_index_fails_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _explain(
        monkeypatch,
        {"success": True, "total_results": 0, "results": []},
        ScipIndexGap(missing=True, message="No SCIP index."),
        build="The last SCIP build failed: setup exit 70",
    )
    assert payload["success"] is False
    assert payload["error"] == (
        "No SCIP index. The last SCIP build failed: setup exit 70"
    )


def test_partial_index_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _explain(
        monkeypatch,
        {"success": True, "chains": []},
        ScipIndexGap(missing=False, message="incomplete."),
    )
    assert payload["success"] is True
    assert payload["scip_index"] == "incomplete."


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "results": [{"symbol": "DealTax"}]},
        {"success": True, "affected_symbols": [{"symbol": "x"}]},
        {"success": False, "error": "boom", "results": []},
    ],
)
def test_answered_or_failed_searches_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]
) -> None:
    before = dict(payload)
    _explain(monkeypatch, payload, ScipIndexGap(missing=True, message="none"))
    assert payload == before


def test_complete_index_and_unknown_gap_leave_zero_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _explain(monkeypatch, {"success": True, "results": []}, None)
    assert payload == {"success": True, "results": []}

    def boom() -> Any:
        raise RuntimeError("no service")

    monkeypatch.setattr(scip_handlers, "_get_scip_query_service", boom)
    payload = {"success": True, "results": []}
    scip_handlers._explain_no_results(payload, ALIAS, "alice")
    assert payload == {"success": True, "results": []}
