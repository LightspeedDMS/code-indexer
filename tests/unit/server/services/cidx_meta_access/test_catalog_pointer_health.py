"""An unusable catalog alias pointer is visible on health and re-warned.

While the catalog pointer exists but cannot be used, restricted callers are
served the restricted view everywhere; health reports that as DEGRADED with
one neutral reason, and the operator warning is logged once per episode
(again after the pointer has read cleanly in between). Real files on disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from code_indexer.server.services.cidx_meta_access import served_identity
from code_indexer.server.services.cidx_meta_access.served_identity import (
    CATALOG_POINTER_UNREADABLE_REASON,
    catalog_locations,
    catalog_pointer_health_reasons,
)

UNUSABLE = ["[]", "{not json", "<dangling>"]


@pytest.fixture
def golden(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "srv" / "data" / "golden-repos"
    (root / "aliases").mkdir(parents=True)
    served_identity._unusable_warned.clear()
    yield root
    served_identity._unusable_warned.clear()


def _pointer(golden: Path, state: str) -> None:
    pointer = golden / "aliases" / "cidx-meta-global.json"
    if pointer.is_symlink() or pointer.exists():
        pointer.unlink()
    if state == "valid":
        pointer.write_text(json.dumps({"target_path": str(golden / "published")}))
    elif state == "<dangling>":
        pointer.symlink_to(golden / "aliases" / "missing.json")
    elif state != "missing":
        pointer.write_text(state)


@pytest.mark.parametrize("state", UNUSABLE)
def test_unusable_pointer_gives_one_neutral_reason(golden: Path, state: str) -> None:
    _pointer(golden, state)

    assert catalog_pointer_health_reasons(str(golden)) == [
        CATALOG_POINTER_UNREADABLE_REASON
    ]


@pytest.mark.parametrize("state", ["valid", "missing"])
def test_usable_or_missing_pointer_gives_no_reason(golden: Path, state: str) -> None:
    _pointer(golden, state)

    assert catalog_pointer_health_reasons(str(golden)) == []
    assert catalog_pointer_health_reasons(None) == []


def test_health_collector_reports_degraded_only_while_unusable(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server import app as app_module
    from code_indexer.server.services.health_service import HealthCheckService

    service: Any = HealthCheckService.__new__(HealthCheckService)
    state = SimpleNamespace(golden_repos_dir=str(golden))
    monkeypatch.setattr(app_module, "app", SimpleNamespace(state=state), raising=False)

    _pointer(golden, "[]")
    assert service._collect_catalog_pointer_failures() == (
        True,
        False,
        [CATALOG_POINTER_UNREADABLE_REASON],
    )
    _pointer(golden, "valid")
    assert service._collect_catalog_pointer_failures() == (False, False, [])


def test_warning_is_logged_once_per_episode(
    golden: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def warnings() -> int:
        return sum(
            1
            for r in caplog.records
            if r.levelno == logging.WARNING and "pointer" in r.getMessage()
        )

    with caplog.at_level(logging.WARNING):
        _pointer(golden, "[]")
        catalog_locations(str(golden))
        catalog_locations(str(golden))
        assert warnings() == 1
        _pointer(golden, "valid")
        catalog_locations(str(golden))
        _pointer(golden, "[]")
        catalog_locations(str(golden))
        assert warnings() == 2
