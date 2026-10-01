"""
_ensure_launch_config must re-validate host/port/workers before rewriting
the live service definition.

launch.json (and, for DEPLOY mode, applied_launch.json / the live unit
file) are sources this process does not fully control the contents of, so
_ensure_launch_config must not trust them blind. When the resolved host is
not a valid IPv4/IPv6 address or RFC 1123 hostname, or port/workers fall
outside their valid ranges, the rewrite must be refused and the on-disk
unit file must be left byte-for-byte unchanged -- matching the function's
existing "return None, do not touch the unit" contract for every other
failure case (missing service file, corrupt source, etc).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

_UNIT_CONTENT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1

[Install]
WantedBy=multi-user.target
"""

_MULTILINE_HOST = "example.com\nExecStartPre=+/bin/true"


def _make_executor(service_name: str = "cidx-server") -> DeploymentExecutor:
    return DeploymentExecutor(repo_path=Path("/tmp"), service_name=service_name)


def _run_apply_with_launch_values(tmp_path: Path, host, port=8000, workers=1):
    """Run _ensure_launch_config("APPLY") with a given launch.json host value.

    Returns (result, unit_file_content_after, tee_was_called).
    """
    executor = _make_executor()
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir(exist_ok=True)
    unit_path = unit_dir / "cidx-server.service"
    unit_path.write_text(_UNIT_CONTENT)

    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({"host": host, "port": port, "workers": workers}))

    tee_calls = []

    def fake_subprocess_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "tee" in cmd:
            tee_calls.append(kwargs.get("input", ""))
            unit_path.write_text(kwargs.get("input", ""))
        return result

    with patch(
        "code_indexer.server.auto_update.deployment_executor.subprocess.run",
        side_effect=fake_subprocess_run,
    ):
        with patch(
            "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
            launch,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                unit_dir,
            ):
                result = executor._ensure_launch_config("APPLY")

    return result, unit_path.read_text(), bool(tee_calls)


class TestEnsureLaunchConfigRejectsInvalidHost:
    def test_multiline_host_refuses_rewrite_and_preserves_unit(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host=_MULTILINE_HOST
        )

        assert not tee_called, (
            "_ensure_launch_config must not write the unit file when the "
            "resolved host fails validation"
        )
        assert unit_after == _UNIT_CONTENT, (
            "unit file content must be byte-for-byte unchanged when the "
            "rewrite is refused"
        )
        assert result is None, (
            "_ensure_launch_config(APPLY) must return None on a refused "
            f"rewrite; got {result!r}"
        )

    def test_percent_zone_id_host_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="fe80::1%eth0"
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None


class TestEnsureLaunchConfigAcceptsLegitimateValues:
    def test_legitimate_new_host_rewrites_execstart(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="192.0.2.10"
        )
        assert tee_called, "expected a legitimate host change to trigger a rewrite"
        assert "--host 192.0.2.10" in unit_after
        assert result is not None
        assert result["host"] == "192.0.2.10"

    def test_localhost_is_accepted(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="localhost"
        )
        assert tee_called
        assert "--host localhost" in unit_after
        assert result is not None

    def test_ipv6_all_zero_is_accepted(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="::"
        )
        assert tee_called
        assert "--host ::" in unit_after
        assert result is not None


class TestEnsureLaunchConfigRejectsOutOfRangePortWorkers:
    def test_port_zero_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="192.0.2.10", port=0
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None

    def test_port_above_max_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="192.0.2.10", port=70000
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None

    def test_workers_above_max_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="192.0.2.10", workers=65
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None


class TestEnsureLaunchConfigSharesTheHostValidator:
    """The deployer re-validation accepts exactly the set the config write
    paths accept: trailing-dot FQDNs and "_" in labels pass, metacharacters
    do not."""

    def test_trailing_dot_fqdn_is_accepted(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="host.example.com."
        )
        assert tee_called
        assert "--host host.example.com. " in unit_after
        assert result is not None

    def test_underscore_hostname_is_accepted(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="node_1.example.internal"
        )
        assert tee_called
        assert "--host node_1.example.internal " in unit_after
        assert result is not None

    @pytest.mark.parametrize("host", ["127.0.0.1.", "10.0.0.1.", "0x7f.0.0.1", "127.1"])
    def test_non_canonical_numeric_host_refuses_rewrite(self, tmp_path, host):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host=host
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None

    def test_backtick_host_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="node`id`.example.internal"
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None

    def test_dollar_host_refuses_rewrite(self, tmp_path):
        result, unit_after, tee_called = _run_apply_with_launch_values(
            tmp_path, host="node$HOME.example.internal"
        )
        assert not tee_called
        assert unit_after == _UNIT_CONTENT
        assert result is None
