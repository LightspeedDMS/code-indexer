"""
TDD tests for Story #1199 AC1 + AC3: APPLY mode rewrites ExecStart from launch.json,
covering both the main.py (installer) shape and the uvicorn shape.

Behavioral: real temp unit files, real launch.json, patched subprocess only.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Unit-file templates
# ---------------------------------------------------------------------------

_MAIN_PY_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 0.0.0.0 --port 8000

[Install]
WantedBy=multi-user.target
"""

_UVICORN_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000

[Install]
WantedBy=multi-user.target
"""

_UNRELATED_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/some-other-command --host 0.0.0.0

[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def unit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "systemd"
    d.mkdir()
    return d


@pytest.fixture
def executor(tmp_path: Path) -> Any:
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")


def write_unit(unit_dir: Path, content: str) -> None:
    (unit_dir / "cidx-server.service").write_text(content)


def write_launch(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 9001,
    workers: int = 6,
    gen: int = 4,
) -> Path:
    p = tmp_path / "launch.json"
    p.write_text(
        json.dumps(
            {
                "host": host,
                "port": port,
                "workers": workers,
                "log_level": "INFO",
                "target_restart_generation": gen,
            }
        )
    )
    return p


def run_apply(
    executor: Any, unit_dir: Path, launch_path: Path, tee_ok: bool = True
) -> tuple:
    """Run APPLY mode; patch subprocess boundaries only. Returns (result, written_or_None)."""
    tee_written: list = []

    def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
        r = MagicMock()
        if "tee" in cmd:
            r.returncode = 0 if tee_ok else 1
            r.stderr = "" if tee_ok else "tee failed"
            tee_written.append(kwargs.get("input", ""))
        else:
            r.returncode = 0
            r.stderr = ""
        r.stdout = ""
        return r

    with patch(
        "code_indexer.server.auto_update.deployment_executor.subprocess.run",
        side_effect=fake_run,
    ):
        with patch(
            "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
            launch_path,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                unit_dir,
            ):
                result = executor._ensure_launch_config("APPLY")

    return result, (tee_written[0] if tee_written else None)


# ---------------------------------------------------------------------------
# AC1: APPLY rewrites ExecStart from launch.json
# ---------------------------------------------------------------------------


class TestAC1ApplyRewritesFromLaunchJson:
    """AC1: APPLY mode token-bounded in-place rewrite of --host/--port/--workers."""

    def test_rewrites_host_port_workers_on_main_py_shape(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC1 BEHAVIORAL: APPLY rewrites all three flags on installer (main.py) shape."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, host="127.0.0.1", port=9001, workers=6)

        result, written = run_apply(executor, unit_dir, launch)

        assert written is not None, "APPLY must write the unit file"
        assert "--host 127.0.0.1" in written, f"Expected --host in: {written!r}"
        assert "--port 9001" in written, f"Expected --port 9001 in: {written!r}"
        assert "--workers 6" in written, f"Expected --workers 6 in: {written!r}"

    def test_does_not_write_log_level(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC1/CRITICAL-A: --log-level must NOT appear in the rewritten ExecStart."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path)
        result, written = run_apply(executor, unit_dir, launch)

        assert written is not None, "APPLY must write unit"
        assert "--log-level" not in written, (
            f"--log-level must NOT appear in ExecStart (CRITICAL-A): {written!r}"
        )

    def test_returns_snapshot_dict_on_success(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC1 BEHAVIORAL: APPLY returns a truthy dict snapshot on success."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, port=9001, workers=6)
        result, _ = run_apply(executor, unit_dir, launch)

        assert result, f"APPLY must return truthy snapshot; got: {result!r}"
        assert isinstance(result, dict), f"Snapshot must be a dict; got: {type(result)}"
        assert "host" in result and "port" in result and "workers" in result, (
            f"Snapshot must have host/port/workers; got: {result}"
        )

    def test_returns_falsy_on_tee_failure(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC1 BEHAVIORAL: APPLY returns falsy/None when sudo tee fails."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path)
        result, _ = run_apply(executor, unit_dir, launch, tee_ok=False)

        assert not result, f"APPLY must return falsy on tee failure; got: {result!r}"

    def test_snapshot_contains_correct_values(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC1 BEHAVIORAL: snapshot dict reflects the launch.json values."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, host="10.0.0.1", port=7777, workers=3)
        result, _ = run_apply(executor, unit_dir, launch)

        assert result is not None
        assert result.get("host") == "10.0.0.1"
        assert result.get("port") == 7777
        assert result.get("workers") == 3


# ---------------------------------------------------------------------------
# AC3: Both ExecStart shapes handled; detection predicate broadened
# ---------------------------------------------------------------------------


class TestAC3BothShapes:
    """AC3: Detection predicate covers code_indexer.server.main AND uvicorn shapes."""

    def test_main_py_shape_detected_and_rewritten(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC3 BEHAVIORAL REGRESSION: installer main.py ExecStart must be detected.

        Old guard only checked for 'uvicorn' — installer units were silently skipped.
        """
        write_unit(unit_dir, _MAIN_PY_UNIT)  # contains NO 'uvicorn' substring
        launch = write_launch(tmp_path, workers=6, port=9001)
        _, written = run_apply(executor, unit_dir, launch)

        assert written is not None, (
            "CRITICAL REGRESSION: main.py ExecStart (no 'uvicorn' substring) was silently "
            "skipped. Detection predicate must cover code_indexer.server.main."
        )
        assert "--workers 6" in written, f"workers must be rewritten: {written!r}"
        assert "--port 9001" in written, f"port must be rewritten: {written!r}"

    def test_uvicorn_shape_detected_and_rewritten(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC3 BEHAVIORAL: uvicorn ExecStart shape is also detected and rewritten."""
        write_unit(unit_dir, _UVICORN_UNIT)
        launch = write_launch(tmp_path, workers=6, port=9001)
        _, written = run_apply(executor, unit_dir, launch)

        assert written is not None, "uvicorn ExecStart shape must be detected"
        assert "--workers 6" in written, (
            f"uvicorn shape: workers must be rewritten: {written!r}"
        )
        assert "--port 9001" in written, (
            f"uvicorn shape: port must be rewritten: {written!r}"
        )

    def test_unrelated_unit_not_modified(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC3: A unit with neither main.py nor uvicorn keyword must not be modified."""
        write_unit(unit_dir, _UNRELATED_UNIT)
        launch = write_launch(tmp_path)
        _, written = run_apply(executor, unit_dir, launch)

        assert written is None, (
            f"Must not rewrite ExecStart lacking main.py/uvicorn. Got: {written!r}"
        )

    def test_apply_targets_live_systemd_unit_dir(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC3/CRITICAL-D: APPLY writes to SYSTEMD_UNIT_DIR (live unit), not installer copy."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, port=9001, workers=4)
        tee_targets: list = []

        def capture_tee(cmd: list, **kwargs: Any) -> MagicMock:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if "tee" in cmd:
                tee_targets.append(cmd[-1] if len(cmd) > 1 else "")
            return r

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=capture_tee,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                launch,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    executor._ensure_launch_config("APPLY")

        assert tee_targets, "sudo tee must be called"
        assert (
            str(unit_dir) in tee_targets[0] or "cidx-server.service" in tee_targets[0]
        ), (
            f"APPLY must target the live unit dir ({unit_dir}). Got tee target: {tee_targets[0]!r}"
        )
