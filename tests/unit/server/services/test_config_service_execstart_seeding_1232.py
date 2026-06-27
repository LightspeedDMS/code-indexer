"""
TDD tests for Bug #1232: materialize_launch_config seeds host/port/workers
from the live ExecStart, not from the ServerConfig default (127.0.0.1).

Root cause:
  materialize_launch_config() calls self.get_config() and uses config.host.
  If config.json has no explicit 'host', config.host == '127.0.0.1' (the
  ServerConfig dataclass default). On a cluster node the installer wrote
  --host 0.0.0.0 directly into the systemd ExecStart (so HAProxy can reach
  the node), but never updated config.json. So the first materialise after
  centralization seeds launch.json with 127.0.0.1 — the auto-updater's next
  APPLY phase rewrites ExecStart to --host 127.0.0.1, dropping the node off
  HAProxy (production outage identical to the defect that prompted Story #1199).

Fix:
  Priority order for host/port/workers in materialize_launch_config:
  1. Live ExecStart value (always authoritative when parseable).
  2. Explicit value in config.json (operator-set; only when ExecStart absent).
  3. ServerConfig default as last resort + WARNING.

  If an explicit config.json value CONTRADICTS the live ExecStart, prefer the
  live ExecStart and log a structured WARNING.

Tests (real files, patched filesystem paths, no mocks of core logic):
1. config.json WITHOUT 'host' + ExecStart --host 0.0.0.0  -> launch host == 0.0.0.0
2. config.json WITH 'host: 0.0.0.0' (explicit match)       -> launch host == 0.0.0.0
3. No ExecStart + no explicit host                          -> default 127.0.0.1 + WARNING
4. Contradiction: config 127.0.0.1 / ExecStart 0.0.0.0     -> 0.0.0.0 + WARNING
5. port and workers from ExecStart (at least one test)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_CLUSTER_EXECSTART = (
    "ExecStart=/opt/code-indexer/venv/bin/python3 -m code_indexer.server.main"
    " --host 0.0.0.0 --port 8000 --workers 4"
)

_CLUSTER_UNIT_TMPL = """\
[Unit]
Description=CIDX Multi-User Server
After=network.target

[Service]
Type=simple
User=code-indexer
WorkingDirectory=/opt/code-indexer
{execstart}
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _write_service(unit_dir: Path, execstart: str = _CLUSTER_EXECSTART) -> None:
    """Write a realistic cidx-server.service to unit_dir."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "cidx-server.service").write_text(
        _CLUSTER_UNIT_TMPL.format(execstart=execstart)
    )


def _make_svc(server_dir: Path, config_json: dict):  # type: ignore[no-untyped-def]
    """Create a minimal ConfigService backed by the given config.json dict."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    server_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"server_dir": str(server_dir)}
    cfg.update(config_json)
    (server_dir / "config.json").write_text(json.dumps(cfg))
    mgr = ServerConfigManager(server_dir_path=str(server_dir))
    svc = ConfigService(config_manager=mgr)
    svc.load_config()
    return svc


def _run_materialize(
    svc,  # type: ignore[no-untyped-def]
    launch_path: Path,
    unit_dir: Path,
) -> dict:
    """Call materialize_launch_config with filesystem paths patched; return launch.json."""
    import code_indexer.server.services.config_service as cs_mod
    import code_indexer.server.auto_update.deployment_executor as de_mod

    with patch.object(cs_mod, "LAUNCH_CONFIG_PATH", launch_path):
        with patch.object(de_mod, "SYSTEMD_UNIT_DIR", unit_dir):
            result = svc.materialize_launch_config()

    assert result is True, (
        f"materialize_launch_config() returned {result!r} (expected True)"
    )
    assert launch_path.exists(), "launch.json was not created"
    return cast(dict, json.loads(launch_path.read_text()))


# ---------------------------------------------------------------------------
# Test 1: config.json WITHOUT 'host'; ExecStart has --host 0.0.0.0
# ---------------------------------------------------------------------------


class TestExecStartHostWithoutConfigJsonHost:
    """When config.json has no explicit 'host', the live ExecStart value wins."""

    def test_launch_host_from_execstart_when_absent_from_config_json(
        self, tmp_path: Path
    ) -> None:
        """Bug #1232 core case: no 'host' in config.json + ExecStart 0.0.0.0 -> 0.0.0.0.

        Before the fix: config.host == '127.0.0.1' (the ServerConfig default)
        -> launch.json seeded with 127.0.0.1 -> APPLY rewrites ExecStart to
        127.0.0.1 -> node drops off HAProxy.

        After the fix: host is read from the live ExecStart (0.0.0.0).
        """
        unit_dir = tmp_path / "systemd"
        _write_service(unit_dir)

        # config.json has NO 'host' key — only port and workers explicit
        svc = _make_svc(tmp_path / "server", {"port": 8000, "workers": 4})

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "0.0.0.0", (
            f"Bug #1232: host must come from live ExecStart (0.0.0.0) when absent "
            f"from config.json. Got: {data['host']!r}"
        )

    def test_workers_from_execstart_when_absent_from_config_json(
        self, tmp_path: Path
    ) -> None:
        """workers=4 from ExecStart when config.json has no 'workers' key."""
        unit_dir = tmp_path / "systemd"
        _write_service(
            unit_dir,
            execstart=(
                "ExecStart=/opt/venv/bin/python3 -m code_indexer.server.main"
                " --host 0.0.0.0 --port 8000 --workers 8"
            ),
        )

        svc = _make_svc(tmp_path / "server", {"port": 8000})  # no 'workers' key

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["workers"] == 8, (
            f"Bug #1232: workers must come from live ExecStart (8) when absent "
            f"from config.json. Got: {data['workers']!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: config.json WITH explicit 'host: 0.0.0.0' (ExecStart also 0.0.0.0)
# ---------------------------------------------------------------------------


class TestExplicitConfigJsonHostPreserved:
    """When config.json has an explicit host, it is used (ExecStart also matches)."""

    def test_explicit_host_in_config_json_used(self, tmp_path: Path) -> None:
        """config.json WITH host=0.0.0.0 (operator-set) -> launch host == 0.0.0.0."""
        unit_dir = tmp_path / "systemd"
        _write_service(unit_dir)  # ExecStart also has 0.0.0.0

        # config.json has explicit 'host' == 0.0.0.0
        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 4},
        )

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "0.0.0.0", (
            f"Explicit config.json host=0.0.0.0 must be preserved. Got: {data['host']!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: No ExecStart available + no explicit host -> default + WARNING
# ---------------------------------------------------------------------------


class TestFallbackToDefaultWithWarning:
    """No ExecStart + no explicit host in config.json -> default 127.0.0.1 + WARNING."""

    def test_fallback_to_default_host_with_warning_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No service file + no explicit host -> host=127.0.0.1 (default) + WARNING."""
        unit_dir = tmp_path / "systemd"
        # Deliberately do NOT write any service file

        svc = _make_svc(tmp_path / "server", {"port": 8000, "workers": 1})
        # No 'host' in config.json, no ExecStart available

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.services.config_service"
        ):
            data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "127.0.0.1", (
            f"Fallback: default host (127.0.0.1) expected when ExecStart unavailable "
            f"and host absent from config.json. Got: {data['host']!r}"
        )

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "host" in m.lower() or "default" in m.lower() or "execstart" in m.lower()
            for m in warning_messages
        ), (
            f"A WARNING must be logged when falling back to the default host. "
            f"Logged warnings: {warning_messages!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: Contradiction — config.json/default says 127.0.0.1, ExecStart says 0.0.0.0
# ---------------------------------------------------------------------------


class TestContradictionExecStartWins:
    """ExecStart wins when it contradicts config.json; structured WARNING logged."""

    def test_execstart_wins_over_config_json_contradiction(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """config.json host=127.0.0.1 but ExecStart --host 0.0.0.0 -> 0.0.0.0 + WARNING.

        This is the cluster-upgrade scenario: the operator may have an old
        config.json with the loopback default, but the actual running bind is
        0.0.0.0 (set by the installer in ExecStart). The ExecStart is truth.
        """
        unit_dir = tmp_path / "systemd"
        _write_service(unit_dir)  # ExecStart: --host 0.0.0.0

        # config.json explicitly says 127.0.0.1 (the old default)
        svc = _make_svc(
            tmp_path / "server",
            {"host": "127.0.0.1", "port": 8000, "workers": 4},
        )

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.services.config_service"
        ):
            data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "0.0.0.0", (
            f"Bug #1232 contradiction: ExecStart (0.0.0.0) must win over "
            f"config.json (127.0.0.1). Got: {data['host']!r}"
        )

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            (
                "contradict" in m.lower()
                or "conflict" in m.lower()
                or "execstart" in m.lower()
                or "prefer" in m.lower()
                or "0.0.0.0" in m
                or "127.0.0.1" in m
            )
            for m in warning_messages
        ), (
            f"A WARNING describing the contradiction must be logged. "
            f"Logged warnings: {warning_messages!r}"
        )

    def test_execstart_wins_when_config_json_has_default_host(
        self, tmp_path: Path
    ) -> None:
        """No explicit 'host' in config.json (so default 127.0.0.1 loaded), ExecStart 0.0.0.0.

        This is the MOST COMMON production case (installer never wrote host to
        config.json).  The live ExecStart 0.0.0.0 must win.
        """
        unit_dir = tmp_path / "systemd"
        _write_service(unit_dir)  # ExecStart: --host 0.0.0.0

        # config.json has NO 'host' key -> get_config() returns host='127.0.0.1'
        svc = _make_svc(tmp_path / "server", {"port": 8000, "workers": 4})

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "0.0.0.0", (
            f"Bug #1232 core: ExecStart 0.0.0.0 must win over default 127.0.0.1 "
            f"(absent from config.json). Got: {data['host']!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: port and workers parsed from ExecStart analogously
# ---------------------------------------------------------------------------


class TestPortAndWorkersFromExecStart:
    """Port and workers are also resolved from ExecStart when absent from config.json."""

    def test_port_from_execstart_when_absent_from_config_json(
        self, tmp_path: Path
    ) -> None:
        """ExecStart --port 9999 -> launch.json port == 9999 (not config default 8000)."""
        unit_dir = tmp_path / "systemd"
        _write_service(
            unit_dir,
            execstart=(
                "ExecStart=/opt/venv/bin/python3 -m code_indexer.server.main"
                " --host 0.0.0.0 --port 9999 --workers 2"
            ),
        )

        # config.json has NO 'port' key
        svc = _make_svc(tmp_path / "server", {"workers": 2})

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["port"] == 9999, (
            f"Bug #1232: port from ExecStart (9999) must be used when absent from "
            f"config.json. Got: {data['port']!r}"
        )

    def test_all_three_from_execstart_when_config_json_has_none(
        self, tmp_path: Path
    ) -> None:
        """config.json has ONLY server_dir; all three come from ExecStart."""
        unit_dir = tmp_path / "systemd"
        _write_service(
            unit_dir,
            execstart=(
                "ExecStart=/opt/venv/bin/python3 -m code_indexer.server.main"
                " --host 192.168.1.50 --port 7000 --workers 6"
            ),
        )

        # Minimal config.json — no host, port, or workers
        svc = _make_svc(tmp_path / "server", {})

        data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["host"] == "192.168.1.50", f"Got host={data['host']!r}"
        assert data["port"] == 7000, f"Got port={data['port']!r}"
        assert data["workers"] == 6, f"Got workers={data['workers']!r}"

    def test_workers_contradiction_execstart_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ExecStart --workers 8 vs config.json workers=1 -> 8 used + WARNING."""
        unit_dir = tmp_path / "systemd"
        _write_service(
            unit_dir,
            execstart=(
                "ExecStart=/opt/venv/bin/python3 -m code_indexer.server.main"
                " --host 0.0.0.0 --port 8000 --workers 8"
            ),
        )

        # config.json has explicit workers=1
        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 1},
        )

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.services.config_service"
        ):
            data = _run_materialize(svc, tmp_path / "launch.json", unit_dir)

        assert data["workers"] == 8, (
            f"ExecStart workers=8 must win over config.json workers=1. "
            f"Got: {data['workers']!r}"
        )
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "worker" in m.lower()
            or "8" in m
            or "1" in m
            or "execstart" in m.lower()
            or "contradict" in m.lower()
            or "conflict" in m.lower()
            or "prefer" in m.lower()
            for m in warning_messages
        ), f"WARNING expected for workers contradiction. Got: {warning_messages!r}"
