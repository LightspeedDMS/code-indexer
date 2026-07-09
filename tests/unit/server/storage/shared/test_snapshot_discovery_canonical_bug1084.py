"""Bug #1084 review fix: cow-daemon discovery API returns CANONICAL snapshot paths.

Proven defect (review-grade, REAL components — the daemon-HTTP boundary is the
ONLY thing stubbed):

``VersionedSnapshotManager._list_cow_daemon_snapshots`` built the snapshot path
as ``{mount}/{ns}/{name}`` (LEGACY shape). But ``CowDaemonBackend.create_clone``
stores snapshots at ``{mount}/.versioned/{ns}/{name}`` (CANONICAL shape — also
what alias ``target_path``/``previous_path`` contain). The mismatch broke the two
path-consuming discovery clients on cow-daemon:

1. Retention protection no-op (``_enforce_retention`` protects current/previous by
   STRING match against ``list_snapshots`` output — legacy strings never matched
   canonical alias targets, so the rollback snapshot could be scheduled for
   deletion at ``keep_last=1``).
2. Defect E restore broken (``latest_snapshot`` returned the legacy path as the
   ``cp`` source — that path does not exist on disk).

These tests drive a REAL ``CowDaemonBackend`` (its ``list_clones`` /
sanitization / param-building code runs) and a REAL ``VersionedSnapshotManager``;
only the ``requests`` module is stubbed (mirrors the existing CowDaemonBackend
HTTP-boundary test pattern in ``test_clone_backend.py``). No
``list_snapshots`` / ``latest_snapshot`` mocking — those are the methods under
test.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils.config_manager import CowDaemonConfig

_MOUNT = "/mnt/cow-storage"


def _make_response(status_code: int, json_data=None):
    """Build a MagicMock HTTP response (mirrors test_clone_backend._make_response)."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data if json_data is not None else {}
    mock.raise_for_status = MagicMock()
    return mock


def _make_real_cow_manager(list_clones_json):
    """Real CowDaemonBackend + real VersionedSnapshotManager.

    The ONLY seam is the ``requests`` module: its GET returns *list_clones_json*
    (the daemon's ``GET /api/v1/clones`` payload). The backend's real
    ``list_clones`` code path — sanitization, param building, JSON parsing —
    executes unchanged.
    """
    backend = CowDaemonBackend(
        config=CowDaemonConfig(
            daemon_url="http://daemon:8081",
            api_key="k",
            mount_point=_MOUNT,
        )
    )
    manager = VersionedSnapshotManager(clone_backend=backend)
    mock_req = MagicMock()
    mock_req.get.return_value = _make_response(200, list_clones_json)
    return manager, backend, mock_req


class TestCowDaemonDiscoveryReturnsCanonicalPath:
    """list_snapshots / latest_snapshot map daemon clones to the CANONICAL shape
    ``{mount}/.versioned/{ns}/v_<ts>`` — the same shape create_clone writes and
    the same shape alias target_path/previous_path carry."""

    def test_list_snapshots_returns_canonical_path_real_backend(self):
        # The daemon registry namespace is the sanitized (dots->underscores) ns.
        list_json = [
            {"namespace": "my_repo", "name": "v_1700000200"},
            {"namespace": "my_repo", "name": "v_1700000100"},
            {"namespace": "my_repo", "name": "main"},  # not a snapshot -> ignored
        ]
        manager, _backend, mock_req = _make_real_cow_manager(list_json)

        with patch.dict(sys.modules, {"requests": mock_req}):
            snaps = manager.list_snapshots("my-repo-global")

        # Sorted ascending by ts.
        assert [ts for _, ts in snaps] == [1700000100, 1700000200]
        paths = [p for p, _ in snaps]
        # CANONICAL shape — includes the ``.versioned`` segment.
        assert paths == [
            f"{_MOUNT}/.versioned/my_repo/v_1700000100",
            f"{_MOUNT}/.versioned/my_repo/v_1700000200",
        ]
        # And explicitly NOT the legacy shape.
        assert f"{_MOUNT}/my_repo/v_1700000100" not in paths

    def test_latest_snapshot_returns_canonical_path_real_backend(self):
        list_json = [
            {"namespace": "r", "name": "v_1700000000"},
            {"namespace": "r", "name": "v_1700099999"},
        ]
        manager, _backend, mock_req = _make_real_cow_manager(list_json)

        with patch.dict(sys.modules, {"requests": mock_req}):
            latest = manager.latest_snapshot("r-global")

        assert latest == f"{_MOUNT}/.versioned/r/v_1700099999"

    def test_discovery_path_matches_what_create_clone_would_write(self):
        """The path discovery emits MUST equal what create_clone stores, so that
        retention's string-equality protection of current/previous works and
        Defect-E's cp source actually exists on disk.

        create_clone builds ``{mount}/.versioned/{sanitized_ns}/{name}`` (see
        CowDaemonBackend.create_clone); discovery must produce the identical
        string for the same (ns, name).
        """
        ns = "my_repo"
        name = "v_1700000200"
        list_json = [{"namespace": ns, "name": name}]
        manager, backend, mock_req = _make_real_cow_manager(list_json)

        # What create_clone would record (compute it the same way the backend does,
        # without any HTTP — this is the canonical dest string create_clone POSTs).
        expected_canonical = (
            f"{backend._mount_point}/.versioned/"
            f"{backend._sanitize_identifier(ns)}/{name}"
        )

        with patch.dict(sys.modules, {"requests": mock_req}):
            discovered = manager.latest_snapshot("my-repo-global")

        assert discovered == expected_canonical

    def test_dotted_alias_namespace_sanitized_and_canonical(self):
        """Dotted aliases are sanitized (dots->underscores) AND placed under the
        canonical ``.versioned`` segment."""
        list_json = [{"namespace": "langfuse_x_y", "name": "v_1749000000"}]
        manager, backend, mock_req = _make_real_cow_manager(list_json)

        with patch.dict(sys.modules, {"requests": mock_req}):
            snaps = manager.list_snapshots("langfuse.x.y-global")

        assert snaps == [(f"{_MOUNT}/.versioned/langfuse_x_y/v_1749000000", 1749000000)]
        # Real backend sanitized the namespace before the daemon GET.
        mock_req.get.assert_called_once()
        assert mock_req.get.call_args.kwargs["params"] == {"namespace": "langfuse_x_y"}
