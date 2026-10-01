"""Unit tests for Bug #882 auto-updater fixes (v9.21.1), superseded in part by Bug #1782.

Bug #882 had two independent defects:

  Defect 1 — run_once.py ignored the operator-configured host/port and
    relied on DeploymentExecutor's hardcoded "http://localhost:8000" default.
    Any deployment on a non-default port (e.g., 8080) could not issue
    maintenance-mode or drain-status requests against its own server.

  Defect 2 — DeploymentExecutor._wait_for_drain() had no early-exit when
    the server was genuinely unreachable. The drain loop would spin for
    up to drain_timeout seconds (7200s fallback when the timeout endpoint
    also fails), blowing through the 120s systemd TimeoutStartSec budget
    on cidx-auto-update.service and killing the entire upgrade cycle.

The original Fix 1 — run_once.py loaded ServerConfigManager().load_config()
and passed `server_url` explicitly into DeploymentExecutor. This was ITSELF
buggy (Bug #1782): Story #1196 deprecated config.json as a source of
host/port — nothing writes those keys there on an ongoing basis, and
ServerConfig's dataclass defaults silently fill in host="127.0.0.1"/
port=8000 when the keys are absent, so a real server bound to a non-default
host/port (e.g. 0.0.0.0:8080) silently resolved to the wrong URL with no
exception (confirmed live on staging).

  Fix 1 (Bug #1782 correction) — run_once._resolve_server_url() now resolves
    host/port via the SAME authoritative launch-config mechanism
    DeploymentExecutor already uses for its ExecStart-rewrite path (Story
    #1199): applied_launch.json (the confirmed-applied config from the most
    recent successful deploy), falling back to the live systemd ExecStart
    flags when applied_launch.json is missing/corrupt/incomplete. See
    TestResolveServerUrlBug1782LaunchConfigMechanism below. When NEITHER
    source can supply a host/port, run_once still raises RuntimeError so
    systemd records an actionable failure instead of silently pointing at
    the wrong URL.

  Fix 2 — _wait_for_drain() tracks STRICTLY CONSECUTIVE ConnectionErrors.
    After three in a row (~30s at the default 10s poll interval) it
    returns True ("assume drained — nothing to drain if server is down").
    Any non-ConnectionError iteration outcome (HTTP response received,
    auth failure, generic exception) resets the counter so the early-exit
    is never triggered by cumulative mixed failures.
"""

import contextlib
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Shared helpers — keep setup duplication out of individual tests.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched_launch_paths(applied_path, launch_path, unit_dir):
    """Patch the module-level path constants DeploymentExecutor's launch-config
    mechanism reads (Story #1199 / Bug #1782), mirroring the pattern used in
    test_deploy_mode_1199.py's run_deploy() helper.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "code_indexer.server.auto_update.deployment_executor"
                ".APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.server.auto_update.deployment_executor"
                ".LAUNCH_CONFIG_PATH",
                launch_path,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                unit_dir,
            )
        )
        yield


def _drain_status_response(drained):
    """Build a 200 drain-status response. drained=True/False controls the payload."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = (
        {"drained": True}
        if drained
        else {"drained": False, "running_jobs": 1, "queued_jobs": 0}
    )
    return response


@pytest.fixture
def drain_executor():
    """DeploymentExecutor configured for fast drain-loop tests (no real sleeps)."""
    from code_indexer.server.auto_update.deployment_executor import (
        DeploymentExecutor,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        yield DeploymentExecutor(
            repo_path=Path(tmpdir),
            server_url="http://127.0.0.1:8000",
            drain_poll_interval=0,
        )


@contextlib.contextmanager
def _patched_drain(executor, auth="fake-token", drain_timeout=60):
    """Patch the three collaborators the drain loop calls; yield the requests.get mock."""
    # Use explicit keyword args rather than **auth_kwargs dict-unpacking so that
    # mypy can resolve the patch.object overload unambiguously.
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(executor, "_get_drain_timeout", return_value=drain_timeout)
        )
        if isinstance(auth, list):
            stack.enter_context(
                patch.object(executor, "_get_auth_token", side_effect=auth)
            )
        else:
            stack.enter_context(
                patch.object(executor, "_get_auth_token", return_value=auth)
            )
        mock_get = stack.enter_context(patch("requests.get"))
        yield mock_get


# ---------------------------------------------------------------------------
# Fix 1: run_once._resolve_server_url
# ---------------------------------------------------------------------------


_MAIN_PY_UNIT_TEMPLATE = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host {host} --port {port}

[Install]
WantedBy=multi-user.target
"""


@pytest.fixture
def executor(tmp_path):
    """Real DeploymentExecutor rooted at tmp_path (no server_url needed here)."""
    from code_indexer.server.auto_update.deployment_executor import (
        DeploymentExecutor,
    )

    return DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")


@pytest.fixture
def launch_paths(tmp_path):
    """Real temp-file (applied_launch.json, launch.json, systemd unit dir) triple.

    Neither applied_launch.json nor launch.json is created here — individual
    tests write whichever file(s) their scenario requires. unit_dir always
    exists (empty by default) so SYSTEMD_UNIT_DIR patching is always valid.
    """
    applied = tmp_path / "applied_launch.json"
    launch = tmp_path / "launch.json"
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    return applied, launch, unit_dir


class TestResolveServerUrlBug1782LaunchConfigMechanism:
    """Bug #1782 — _resolve_server_url() must resolve host/port via the SAME
    authoritative launch-config mechanism DeploymentExecutor already uses for
    its ExecStart-rewrite path (_read_launch_source/_fill_from_live_execstart),
    never via config.json/ServerConfigManager.

    Story #1196 deprecated config.json as a source of host/port: nothing
    writes those keys there on an ongoing basis, and ServerConfig's dataclass
    defaults silently fill in host="127.0.0.1"/port=8000 when absent — a real
    server bound to a non-default host/port (e.g. 0.0.0.0:8080) resolved to
    the wrong URL with no exception (confirmed live on staging).
    """

    def test_returns_url_from_applied_launch_json_real_host_port(
        self, executor, launch_paths
    ):
        """AC1: a real, non-default host/port in applied_launch.json flows through."""
        from code_indexer.server.auto_update import run_once

        applied, launch, unit_dir = launch_paths
        applied.write_text(json.dumps({"host": "0.0.0.0", "port": 8080, "workers": 4}))

        with _patched_launch_paths(applied, launch, unit_dir):
            url = run_once._resolve_server_url(executor)

        assert url == "http://0.0.0.0:8080"

    def test_old_bug_scenario_config_json_missing_host_port_launch_config_wins(
        self, executor, launch_paths, tmp_path
    ):
        """AC2: reproduces the real staging bug — config.json present but missing
        host/port keys (ServerConfig dataclass would default to 127.0.0.1:8000),
        applied_launch.json present with the correct real values. The function
        must return the applied_launch.json-derived URL, never the config.json/
        dataclass-default URL.
        """
        from code_indexer.server.auto_update import run_once

        applied, launch, unit_dir = launch_paths
        # config.json present, but WITHOUT host/port keys — mirrors the real
        # staging file that triggered Bug #1782. It must never be consulted.
        (tmp_path / "config.json").write_text(json.dumps({"server_dir": str(tmp_path)}))
        applied.write_text(json.dumps({"host": "0.0.0.0", "port": 8080, "workers": 2}))

        with _patched_launch_paths(applied, launch, unit_dir):
            url = run_once._resolve_server_url(executor)

        assert url == "http://0.0.0.0:8080", (
            f"Expected the applied_launch.json-derived URL, got {url!r} — this is "
            "the exact Bug #1782 regression (silently falling back to the "
            "ServerConfig/config.json default of 127.0.0.1:8000)."
        )
        assert url != "http://127.0.0.1:8000"

    def test_falls_back_to_live_execstart_when_applied_launch_json_missing(
        self, executor, launch_paths
    ):
        """AC1/mechanism: applied_launch.json missing → live systemd ExecStart
        (the confirmed running state) supplies host/port, matching the same
        fallback DeploymentExecutor._ensure_launch_config('DEPLOY') relies on.
        """
        from code_indexer.server.auto_update import run_once

        applied, launch, unit_dir = launch_paths  # applied deliberately unwritten
        (unit_dir / "cidx-server.service").write_text(
            _MAIN_PY_UNIT_TEMPLATE.format(host="10.0.0.42", port=9090)
        )

        with _patched_launch_paths(applied, launch, unit_dir):
            url = run_once._resolve_server_url(executor)

        assert url == "http://10.0.0.42:9090"


class TestResolveServerUrlBug1782FailLoud:
    """Bug #1782 — genuine total-unresolvability must still fail loud."""

    def test_raises_runtime_error_when_neither_source_available(
        self, executor, launch_paths
    ):
        """AC3: Messi #2 Anti-Fallback — fail loud with an actionable message when
        NEITHER applied_launch.json NOR a live systemd ExecStart can supply a
        host/port. This is the genuine-total-unresolvability case; no hardcoded
        default URL may be silently returned.
        """
        from code_indexer.server.auto_update import run_once

        applied, launch, unit_dir = launch_paths
        # applied is missing and unit_dir has no cidx-server.service unit file.

        with _patched_launch_paths(applied, launch, unit_dir):
            with pytest.raises(RuntimeError) as exc_info:
                run_once._resolve_server_url(executor)

        message = str(exc_info.value)
        assert "1782" in message
        # Anti-regression guard: no hardcoded fallback URL may leak into the
        # error message. If someone later re-introduces a default literal,
        # this assertion fails.
        assert "http://127.0.0.1:8000" not in message
        assert "http://localhost:8000" not in message


# ---------------------------------------------------------------------------
# Fix 2: DeploymentExecutor._wait_for_drain early-exit behavior
# ---------------------------------------------------------------------------


# Parametrized table: each scenario exercises a different non-ConnectionError
# iteration outcome that MUST reset the consecutive counter. If any of these
# paths stops resetting, cumulative-but-not-consecutive failures will trigger
# the early-exit prematurely.
_RESET_COUNTER_SCENARIOS = [
    pytest.param(
        "fake-token",
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            _drain_status_response(drained=False),  # 200 response resets counter
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        6,
        id="200_response_resets",
    ),
    pytest.param(
        ["fake-token", "fake-token", None, "fake-token", "fake-token", "fake-token"],
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            # poll 3: no requests.get call — auth=None hits `continue`
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        5,
        id="auth_none_resets",
    ),
    pytest.param(
        "fake-token",
        [
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            ValueError("synthetic non-connection failure"),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ],
        6,
        id="generic_exception_resets",
    ),
]


class TestWaitForDrainEarlyExit:
    """Bug #882 defect #2 — early-exit on persistent ConnectionError."""

    def test_early_exits_after_three_consecutive_connection_errors(
        self, drain_executor
    ):
        """Three strictly-consecutive ConnectionErrors return True after exactly 3 polls."""
        with _patched_drain(drain_executor) as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError()
            result = drain_executor._wait_for_drain()

        assert result is True
        # Exactly 3 polls — proves the early-exit fired on the 3rd consecutive
        # ConnectionError and the loop did not iterate further.
        assert mock_get.call_count == 3

    @pytest.mark.parametrize(
        "auth,get_side_effect,expected_get_calls", _RESET_COUNTER_SCENARIOS
    )
    def test_non_connection_failure_resets_consecutive_counter(
        self, drain_executor, auth, get_side_effect, expected_get_calls
    ):
        """200-response / auth-None / generic-exception each reset the counter."""
        with _patched_drain(drain_executor, auth=auth) as mock_get:
            mock_get.side_effect = get_side_effect
            result = drain_executor._wait_for_drain()

        assert result is True
        assert mock_get.call_count == expected_get_calls

    def test_intermittent_failures_let_normal_drained_path_win(self, drain_executor):
        """ConnErr/200 alternation never fires early-exit — drained=True resolves."""
        with _patched_drain(drain_executor) as mock_get:
            mock_get.side_effect = [
                requests.exceptions.ConnectionError(),
                requests.exceptions.ConnectionError(),
                _drain_status_response(drained=False),  # resets counter → 0
                requests.exceptions.ConnectionError(),
                requests.exceptions.ConnectionError(),
                _drain_status_response(drained=True),  # normal exit: True
            ]
            result = drain_executor._wait_for_drain()

        assert result is True
        assert mock_get.call_count == 6
