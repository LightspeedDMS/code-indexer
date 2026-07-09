"""Tests for DeploymentExecutor._ensure_daemon_storage_path().

Bug #1320 Part B: idempotently populate cow_daemon.daemon_storage_path in
config.json so CowDaemonBackend can translate CIDX paths (mount_point view)
to the daemon's local filesystem paths (storage_path view). Part A (already
committed) made CowDaemonBackend._translate_to_daemon_path raise a clear
ValueError instead of silently sending an untranslatable path when this
field is empty; Part B is what actually populates it, on both the installer
and the auto-updater ("add it in both, most robust").

Value resolution order (never hardcoded, never guessed):
  1. CIDX_COW_DAEMON_STORAGE_PATH env var (explicit operator override)
  2. Co-located CoW daemon config `base_path` field (true only on the
     daemon-HOST node, read from COW_DAEMON_HOST_CONFIG_PATH)
  3. Neither resolves -> leave unset, log WARNING, non-fatal

AC-a: writes when missing (value from env var)
AC-b: writes from co-located daemon config when env absent
AC-c: no-op when already set correctly (byte-identical config.json)
AC-d: does NOT clobber a different valid existing value
AC-e: leaves unset + logs WARNING when no source resolves
Plus: clone_backend != cow-daemon -> no-op; cow_daemon config missing ->
no-op + WARNING; malformed co-located daemon config -> non-fatal, treated
as unavailable; config.json absent entirely (fresh install) -> no-op.

Real filesystem (tmp_path) used for config.json reads/writes -- no mocking
of open()/json (Anti-Mock rule). Only external dependency mocked:
ServerConfigManager (decision-logic source), matching the pattern in
test_activated_repos_symlink_setup_bug1052.py.
"""

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    clone_backend: str = "cow-daemon",
    daemon_storage_path: Optional[str] = None,
    cow_daemon_none: bool = False,
) -> MagicMock:
    """Return a mock server config for CoW-daemon scenarios."""
    config = MagicMock()
    config.clone_backend = clone_backend
    if cow_daemon_none:
        config.cow_daemon = None
    else:
        config.cow_daemon = MagicMock()
        config.cow_daemon.daemon_storage_path = daemon_storage_path
    return config


def _write_config_json(data_dir: Path, cow_daemon_extra: Optional[dict] = None) -> Path:
    """Write a real cluster-mode cow-daemon config.json to disk."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = data_dir / "config.json"
    body: dict = {
        "storage_mode": "postgres",
        "clone_backend": "cow-daemon",
        "cow_daemon": {
            "daemon_url": "http://cow-host:8081",
            "api_key": "not-a-real-key-placeholder",
            "mount_point": "/mnt/cow-storage",
        },
    }
    if cow_daemon_extra:
        body["cow_daemon"].update(cow_daemon_extra)
    cfg_path.write_text(json.dumps(body, indent=2) + "\n")
    return cfg_path


def _run_step(
    executor: DeploymentExecutor,
    data_dir: Path,
    config: Optional[MagicMock],
    env: Optional[dict] = None,
    daemon_host_config_path: Optional[Path] = None,
) -> bool:
    """Run _ensure_daemon_storage_path with patched config source, data dir,
    env vars, and (optionally) the co-located daemon config path."""
    with contextlib.ExitStack() as stack:
        mock_cm = stack.enter_context(
            patch("code_indexer.server.utils.config_manager.ServerConfigManager")
        )
        mock_cm.return_value.load_config.return_value = config
        stack.enter_context(
            patch(
                "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
                data_dir,
            )
        )
        stack.enter_context(patch.dict(os.environ, env or {}, clear=False))
        if daemon_host_config_path is not None:
            stack.enter_context(
                patch(
                    "code_indexer.server.auto_update.deployment_executor.COW_DAEMON_HOST_CONFIG_PATH",
                    daemon_host_config_path,
                )
            )
        return bool(executor._ensure_daemon_storage_path())


@pytest.fixture()
def executor() -> DeploymentExecutor:
    """Minimal DeploymentExecutor for unit testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# AC-a: writes when missing (value from env)
# ---------------------------------------------------------------------------


class TestWritesFromEnvWhenMissing:
    def test_writes_from_env_var_when_missing(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)

        config = _make_config(daemon_storage_path=None)
        result = _run_step(
            executor,
            data_dir,
            config,
            env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/cow-storage-root"},
        )

        written = json.loads(cfg_path.read_text())
        assert result is True
        assert written["cow_daemon"]["daemon_storage_path"] == "/data/cow-storage-root"
        # sibling fields must be untouched
        assert written["cow_daemon"]["daemon_url"] == "http://cow-host:8081"
        assert written["cow_daemon"]["mount_point"] == "/mnt/cow-storage"


# ---------------------------------------------------------------------------
# AC-b: writes from co-located daemon config when env absent
# ---------------------------------------------------------------------------


class TestWritesFromCoLocatedDaemonConfig:
    def test_writes_from_co_located_daemon_config_when_env_absent(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)

        daemon_host_cfg = tmp_path / "cow-storage-daemon-config.json"
        daemon_host_cfg.write_text(json.dumps({"base_path": "/srv/cow-xfs"}))

        config = _make_config(daemon_storage_path=None)
        result = _run_step(
            executor,
            data_dir,
            config,
            daemon_host_config_path=daemon_host_cfg,
        )

        written = json.loads(cfg_path.read_text())
        assert result is True
        assert written["cow_daemon"]["daemon_storage_path"] == "/srv/cow-xfs"


# ---------------------------------------------------------------------------
# AC-c: no-op when already set correctly
# ---------------------------------------------------------------------------


class TestNoopWhenAlreadySetCorrectly:
    def test_noop_when_already_set(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(
            data_dir, cow_daemon_extra={"daemon_storage_path": "/srv/cow-xfs"}
        )
        before = cfg_path.read_text()

        config = _make_config(daemon_storage_path="/srv/cow-xfs")
        result = _run_step(
            executor,
            data_dir,
            config,
            env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/cow-storage-root"},
        )

        after = cfg_path.read_text()
        assert result is True
        assert before == after, "config.json must be byte-identical (true no-op)"


# ---------------------------------------------------------------------------
# AC-d: does NOT clobber a different valid existing value
# ---------------------------------------------------------------------------


class TestDoesNotClobberDifferentExistingValue:
    def test_does_not_clobber_different_value(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(
            data_dir, cow_daemon_extra={"daemon_storage_path": "/srv/original-value"}
        )

        config = _make_config(daemon_storage_path="/srv/original-value")
        result = _run_step(
            executor,
            data_dir,
            config,
            env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/some-other-path"},
        )

        written = json.loads(cfg_path.read_text())
        assert result is True
        assert written["cow_daemon"]["daemon_storage_path"] == "/srv/original-value", (
            "must never overwrite an existing non-empty value even if a "
            "different value was resolved from env/co-located config"
        )


# ---------------------------------------------------------------------------
# AC-e: leaves unset + logs when no source resolves
# ---------------------------------------------------------------------------


class TestLeavesUnsetWhenNoSourceResolves:
    def test_leaves_unset_and_logs_when_no_source(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)
        before = cfg_path.read_text()

        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"

        config = _make_config(daemon_storage_path=None)
        with caplog.at_level(logging.WARNING):
            result = _run_step(
                executor,
                data_dir,
                config,
                daemon_host_config_path=nonexistent_daemon_cfg,
            )

        after = cfg_path.read_text()
        assert result is True, "must return True (non-fatal) even when unresolved"
        assert before == after, "must not write a null/empty value"
        assert "daemon_storage_path" not in json.loads(after)["cow_daemon"]
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a WARNING must be logged when no source resolves"
        )


# ---------------------------------------------------------------------------
# Plus: clone_backend != cow-daemon -> no-op
# ---------------------------------------------------------------------------


class TestNoopWhenCloneBackendNotCowDaemon:
    def test_noop_when_clone_backend_is_local(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)
        before = cfg_path.read_text()

        config = _make_config(clone_backend="local")
        result = _run_step(
            executor,
            data_dir,
            config,
            env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/cow-storage-root"},
        )

        after = cfg_path.read_text()
        assert result is True
        assert before == after


# ---------------------------------------------------------------------------
# Plus: cow_daemon config missing -> no-op + WARNING
# ---------------------------------------------------------------------------


class TestNoopWhenCowDaemonConfigMissing:
    def test_noop_and_warns_when_cow_daemon_config_is_none(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)
        before = cfg_path.read_text()

        config = _make_config(cow_daemon_none=True)
        with caplog.at_level(logging.WARNING):
            result = _run_step(
                executor,
                data_dir,
                config,
                env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/cow-storage-root"},
            )

        after = cfg_path.read_text()
        assert result is True
        assert before == after
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Plus: malformed co-located daemon config -> non-fatal, treated as
# unavailable (never crashes, never invents a value)
# ---------------------------------------------------------------------------


class TestMalformedCoLocatedDaemonConfigNonFatal:
    def test_malformed_json_treated_as_unavailable(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        cfg_path = _write_config_json(data_dir)
        before = cfg_path.read_text()

        daemon_host_cfg = tmp_path / "cow-storage-daemon-config.json"
        daemon_host_cfg.write_text("{ not valid json")

        config = _make_config(daemon_storage_path=None)
        with caplog.at_level(logging.WARNING):
            result = _run_step(
                executor,
                data_dir,
                config,
                daemon_host_config_path=daemon_host_cfg,
            )

        after = cfg_path.read_text()
        assert result is True
        assert before == after
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Plus: config.json absent entirely (fresh install) -> no-op, no crash
# ---------------------------------------------------------------------------


class TestNoopWhenConfigFileAbsent:
    def test_noop_when_config_json_absent(
        self, executor: DeploymentExecutor, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_COW_DAEMON_STORAGE_PATH", raising=False)
        data_dir = tmp_path / ".cidx-server"
        # No config.json written at all.

        result = _run_step(
            executor,
            data_dir,
            config=None,
            env={"CIDX_COW_DAEMON_STORAGE_PATH": "/data/cow-storage-root"},
        )

        assert result is True
        assert not (data_dir / "config.json").exists()
