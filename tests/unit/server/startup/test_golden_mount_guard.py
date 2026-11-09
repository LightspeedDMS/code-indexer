"""Startup check: all-delegated cidx needs nothing on the golden-repos volume.

Inert under any other configuration (the local default is unchanged); under
all-delegated backends a cidx state path under golden-repos REFUSES startup,
and a golden-repos directory that is still a network mount, or a SCIP backend
that builds on this pod, is a warning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from code_indexer.server.startup.golden_mount_guard import (
    MOUNT_FREE_BACKEND_FIELDS,
    GoldenMountDependencyError,
    check_golden_mount_independence,
    golden_mount_free,
    golden_volume_monitoring_skip_reason,
    legacy_lease_sweep_skip_reason,
    network_mount_of,
    payload_cache_db_path,
    pod_state_dir,
    snapshot_lease_directory,
    versioned_snapshot_sweep_skip_reason,
)


def _config(**overrides: Any) -> SimpleNamespace:
    values: Dict[str, Any] = {f: "delegated" for f in MOUNT_FREE_BACKEND_FIELDS}
    values.update(overrides)
    return SimpleNamespace(**values)


def _part(mountpoint: str, fstype: str) -> SimpleNamespace:
    return SimpleNamespace(mountpoint=mountpoint, fstype=fstype)


SERVER = Path("/srv/cidx")
GOLDEN = SERVER / "data" / "golden-repos"


def test_all_delegated_is_mount_free() -> None:
    assert golden_mount_free(_config()) is True


@pytest.mark.parametrize("field", MOUNT_FREE_BACKEND_FIELDS)
def test_one_local_backend_is_not_mount_free(field: str) -> None:
    assert golden_mount_free(_config(**{field: "local"})) is False


def test_an_unset_config_is_not_mount_free() -> None:
    assert golden_mount_free(SimpleNamespace()) is False


def test_pod_state_is_beside_golden_repos_never_inside() -> None:
    state = pod_state_dir(SERVER)
    assert state.parent == GOLDEN.parent
    assert GOLDEN not in state.parents and state != GOLDEN


def test_the_payload_cache_moves_only_when_mount_free() -> None:
    assert payload_cache_db_path(_config(), GOLDEN, SERVER) == (
        pod_state_dir(SERVER) / "payload_cache.db"
    )
    assert payload_cache_db_path(_config(content_backend="local"), GOLDEN, SERVER) == (
        GOLDEN / ".cache" / "payload_cache.db"
    )


def test_leases_go_to_the_pod_state_dir() -> None:
    assert snapshot_lease_directory(SERVER).parent == pod_state_dir(SERVER)


def _state(config: Any) -> Dict[str, Path]:
    return {
        "payload_cache_db": payload_cache_db_path(config, GOLDEN, SERVER),
        "snapshot_reader_leases": snapshot_lease_directory(SERVER),
    }


def test_a_clean_mount_free_pod_passes_without_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config()
    assert (
        check_golden_mount_independence(
            config, GOLDEN, _state(config), partitions=[_part("/", "ext4")]
        )
        == []
    )
    assert "holds this pod's index state only" in caplog.text


@pytest.mark.parametrize(
    "path",
    [GOLDEN / ".cache" / "payload_cache.db", GOLDEN / ".scratch" / "leases", GOLDEN],
)
def test_state_under_golden_repos_refuses_startup(path: Path) -> None:
    with pytest.raises(GoldenMountDependencyError, match="payload_cache_db"):
        check_golden_mount_independence(
            _config(), GOLDEN, {"payload_cache_db": path}, partitions=[]
        )


def test_a_sibling_with_the_same_prefix_is_not_under_golden_repos() -> None:
    sibling = SERVER / "data" / "golden-repos-backup" / "x.db"
    assert (
        check_golden_mount_independence(
            _config(), GOLDEN, {"x": sibling}, partitions=[]
        )
        == []
    )


@pytest.mark.parametrize("fstype", ["nfs", "nfs4"])
def test_a_golden_repos_network_mount_is_a_warning(fstype: str) -> None:
    config = _config()
    warnings = check_golden_mount_independence(
        config,
        GOLDEN,
        _state(config),
        partitions=[_part("/", "ext4"), _part(str(GOLDEN), fstype)],
    )
    assert len(warnings) == 1 and "still a network mount" in warnings[0]
    assert fstype in warnings[0]


def test_the_most_specific_network_mount_is_named() -> None:
    parts = [_part("/srv", "nfs"), _part(str(GOLDEN), "nfs4"), _part("/x", "nfs")]
    assert network_mount_of(GOLDEN / "repo", parts) == (str(GOLDEN), "nfs4")
    assert network_mount_of(Path("/elsewhere"), parts) is None


def _with_scip(scip_backend: str) -> SimpleNamespace:
    """All repository seams delegated, the scip feature enabled with
    *scip_backend* (its bootstrap setting since modular phase 4d)."""
    return _config(
        enabled_features=["scip"],
        feature_bootstrap_settings={"scip": {"scip_backend": scip_backend}},
    )


def test_a_delegated_plugin_scip_backend_is_mount_free() -> None:
    """SCIP's delegated spelling is ``delegated-plugin``: it builds in the
    plugin's sandbox, so it reads nothing on this pod."""
    assert golden_mount_free(_with_scip("delegated-plugin")) is True


def test_a_scip_backend_that_builds_here_is_not_mount_free() -> None:
    """SCIP under ``local`` builds from the repository on this pod, so the
    golden-repos volume is still needed: the check is inert."""
    config = _with_scip("local")
    assert golden_mount_free(config) is False
    assert (
        check_golden_mount_independence(
            config, GOLDEN, _state(config), partitions=[_part(str(GOLDEN), "nfs4")]
        )
        == []
    )


def test_an_unreadable_mount_table_is_a_warning_not_a_guess() -> None:
    class Broken:
        def __iter__(self) -> Any:
            raise OSError("no /proc/mounts")

    config = _config()
    warnings = check_golden_mount_independence(
        config, GOLDEN, _state(config), partitions=Broken()
    )
    assert len(warnings) == 1 and "could not tell" in warnings[0]


def test_inert_when_not_mount_free() -> None:
    config = _config(index_store_backend="local")
    assert (
        check_golden_mount_independence(
            config,
            GOLDEN,
            {"payload_cache_db": GOLDEN / ".cache" / "payload_cache.db"},
            partitions=[_part(str(GOLDEN), "nfs4")],
        )
        == []
    )


@pytest.mark.parametrize(
    "reason", [golden_volume_monitoring_skip_reason, legacy_lease_sweep_skip_reason]
)
def test_volume_bound_startup_steps_are_skipped_only_when_mount_free(
    reason: Any,
) -> None:
    assert reason(_config()) is not None
    assert reason(_config(git_backend="local")) is None


def test_the_versioned_snapshot_sweep_is_skipped_under_a_delegated_store() -> None:
    assert versioned_snapshot_sweep_skip_reason(SimpleNamespace(is_local=False)) == (
        "index_store_backend='delegated' -- this pod's snapshots are IndexStore "
        "checkouts, pruned by the checkout sweep and the served-index cache"
    )
    assert versioned_snapshot_sweep_skip_reason(SimpleNamespace(is_local=True)) is None
