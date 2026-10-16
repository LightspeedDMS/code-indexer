"""``is_versioned_snapshot_in_running_server`` recognises every snapshot
layout the running server's wired clone backend produces, and only the
canonical layout where no snapshot manager is wired."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from code_indexer.server.storage.shared.snapshot_manager import (
    is_versioned_snapshot_in_running_server,
)
from tests.unit.server.git._running_server_snapshot_manager import (
    wire_running_server_snapshot_manager,
)

_MOUNT = "/srv/example-mount"
_CANONICAL = "/srv/golden-repos/.versioned/example-repo/v_1700000000"
_FLAT_ONTAP = f"{_MOUNT}/v_1700000000"
_LEGACY_COW = f"{_MOUNT}/example-repo/v_1700000000"


def _app_namespace() -> Dict[str, Any]:
    return vars(sys.modules["code_indexer.server.app"])


@pytest.mark.parametrize("path", [_CANONICAL, _FLAT_ONTAP, _LEGACY_COW])
def test_wired_manager_recognises_every_snapshot_layout(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    wire_running_server_snapshot_manager(monkeypatch, _MOUNT)

    assert is_versioned_snapshot_in_running_server(path) is True


@pytest.mark.parametrize(
    "path",
    [
        f"{_MOUNT}/activated-repos/v_1700000000",
        f"{_MOUNT}/example-repo",
        "/srv/other-mount/v_1700000000",
        "/srv/activated/admin-a/example-repo",
    ],
)
def test_wired_manager_rejects_non_snapshot_paths(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    wire_running_server_snapshot_manager(monkeypatch, _MOUNT)

    assert is_versioned_snapshot_in_running_server(path) is False


def test_without_running_server_only_canonical_layout_is_recognised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(_app_namespace(), "app", raising=False)

    assert is_versioned_snapshot_in_running_server(_CANONICAL) is True
    assert is_versioned_snapshot_in_running_server(_FLAT_ONTAP) is False
    assert is_versioned_snapshot_in_running_server(_LEGACY_COW) is False


def test_running_server_without_snapshot_manager_uses_canonical_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_app = SimpleNamespace(state=SimpleNamespace(snapshot_manager=None))
    monkeypatch.setitem(_app_namespace(), "app", running_app)

    assert is_versioned_snapshot_in_running_server(_CANONICAL) is True
    assert is_versioned_snapshot_in_running_server(_FLAT_ONTAP) is False
