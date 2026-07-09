"""
TDD tests for Story #1199 AC1 gap: APPLY snapshot must include applied_restart_generation.

AC1 requires: APPLY mode writes applied_launch.json containing applied_restart_generation
= target_restart_generation from launch.json, ONLY after a successful restart AND ensure.

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

_ALREADY_MATCHING_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 127.0.0.1 --port 9001 --workers 6

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


def write_unit(unit_dir: Path, content: str = _MAIN_PY_UNIT) -> None:
    (unit_dir / "cidx-server.service").write_text(content)


def write_launch(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 9001,
    workers: int = 6,
    target_restart_generation: int = 7,
) -> Path:
    """Write launch.json with target_restart_generation."""
    p = tmp_path / "launch.json"
    p.write_text(
        json.dumps(
            {
                "host": host,
                "port": port,
                "workers": workers,
                "target_restart_generation": target_restart_generation,
            }
        )
    )
    return p


def write_launch_no_generation(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 9001,
    workers: int = 6,
) -> Path:
    """Write launch.json WITHOUT target_restart_generation field."""
    p = tmp_path / "launch.json"
    p.write_text(json.dumps({"host": host, "port": port, "workers": workers}))
    return p


def run_apply(
    executor: Any, unit_dir: Path, launch_path: Path, tee_ok: bool = True
) -> tuple:
    """Run APPLY mode; patch subprocess + path. Returns (result, written_or_None)."""
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
# Tests: applied_restart_generation in APPLY snapshot (modified-ExecStart path)
# ---------------------------------------------------------------------------


class TestApplySnapshotIncludesRestartGeneration:
    """AC1 gap: APPLY snapshot must carry applied_restart_generation from launch.json."""

    def test_apply_modified_path_includes_applied_restart_generation(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """APPLY (modified ExecStart path) snapshot must include applied_restart_generation=7."""
        write_unit(unit_dir, _MAIN_PY_UNIT)  # initial ExecStart will be rewritten
        launch = write_launch(tmp_path, target_restart_generation=7)

        result, _ = run_apply(executor, unit_dir, launch)

        assert result is not None, "APPLY must return a snapshot"
        assert "applied_restart_generation" in result, (
            f"Snapshot must include applied_restart_generation; got: {result}"
        )
        assert result["applied_restart_generation"] == 7, (
            f"applied_restart_generation must equal target (7); got: {result}"
        )

    def test_apply_modified_path_generation_matches_target(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """APPLY snapshot applied_restart_generation must match target_restart_generation."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, target_restart_generation=42)

        result, _ = run_apply(executor, unit_dir, launch)

        assert result is not None
        assert result.get("applied_restart_generation") == 42, (
            f"applied_restart_generation must be 42; got: {result}"
        )


class TestApplyNoOpPathIncludesRestartGeneration:
    """AC1 convergence-critical: no-op path (ExecStart already matches) must also carry generation.

    Without this, a brand-new node whose ExecStart is already correct never records the
    generation in applied_launch.json, so #1200's check_pending_launch_restart() would
    perpetually see applied=0 < target and re-signal a restart on every poll.
    """

    def test_apply_noop_path_includes_applied_restart_generation(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """No-op APPLY (ExecStart already matches) must still return snapshot with generation."""
        # ExecStart already has the correct host/port/workers from launch.json
        write_unit(unit_dir, _ALREADY_MATCHING_UNIT)
        launch = write_launch(
            tmp_path,
            host="127.0.0.1",
            port=9001,
            workers=6,
            target_restart_generation=7,
        )

        result, written = run_apply(executor, unit_dir, launch)

        # No-op: ExecStart not modified, so no sudo tee call
        assert written is None, "No-op path must not rewrite unit file"
        assert result is not None, "No-op APPLY must still return a snapshot"
        assert "applied_restart_generation" in result, (
            f"No-op snapshot must include applied_restart_generation; got: {result}"
        )
        assert result["applied_restart_generation"] == 7, (
            f"No-op snapshot applied_restart_generation must be 7; got: {result}"
        )

    def test_apply_noop_generation_varies_with_target(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """No-op APPLY snapshot applied_restart_generation must reflect the target value."""
        write_unit(unit_dir, _ALREADY_MATCHING_UNIT)
        launch = write_launch(
            tmp_path,
            host="127.0.0.1",
            port=9001,
            workers=6,
            target_restart_generation=99,
        )

        result, _ = run_apply(executor, unit_dir, launch)

        assert result is not None
        assert result.get("applied_restart_generation") == 99, (
            f"No-op applied_restart_generation must be 99; got: {result}"
        )


class TestApplyMissingOrAbsentGenerationDefaultsToZero:
    """AC1: When launch.json has no target_restart_generation, default to 0 (COALESCE-0)."""

    def test_apply_launch_json_missing_generation_defaults_to_zero(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """launch.json without target_restart_generation → applied_restart_generation=0."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch_no_generation(tmp_path)

        result, _ = run_apply(executor, unit_dir, launch)

        assert result is not None, (
            "APPLY must succeed even without target_restart_generation"
        )
        assert "applied_restart_generation" in result, (
            f"Snapshot must always include applied_restart_generation; got: {result}"
        )
        assert result["applied_restart_generation"] == 0, (
            f"Missing target_restart_generation must default to 0; got: {result}"
        )

    def test_apply_launch_json_absent_defaults_to_zero(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """Absent launch.json → APPLY falls back to config.json → defaults, generation=0."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        # No launch.json created — path does not exist
        absent_launch = tmp_path / "launch.json"

        result, _ = run_apply(executor, unit_dir, absent_launch)

        assert result is not None, "APPLY must succeed even when launch.json is absent"
        assert result.get("applied_restart_generation") == 0, (
            f"Absent launch.json → applied_restart_generation must be 0; got: {result}"
        )


# ---------------------------------------------------------------------------
# Tests: DEPLOY mode unaffected — no generation leak
# ---------------------------------------------------------------------------


class TestDeployModeUnaffectedByGeneration:
    """DEPLOY mode must NOT be affected: still returns None, no generation in ExecStart."""

    def test_deploy_still_returns_none(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """DEPLOY always returns None regardless of target_restart_generation in launch.json."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 4})
        )
        launch = tmp_path / "launch.json"
        launch.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 9001,
                    "workers": 4,
                    "target_restart_generation": 7,
                }
            )
        )

        tee_written: list = []

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if "tee" in cmd:
                tee_written.append(kwargs.get("input", ""))
            return r

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                    launch,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                        unit_dir,
                    ):
                        result = executor._ensure_launch_config("DEPLOY")

        assert result is None, f"DEPLOY must always return None; got: {result!r}"

    def test_deploy_execstart_does_not_contain_applied_restart_generation(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """DEPLOY ExecStart rewrite must NOT include applied_restart_generation token."""
        write_unit(unit_dir, _MAIN_PY_UNIT)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 4})
        )
        launch = tmp_path / "launch.json"
        launch.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 9001,
                    "workers": 4,
                    "target_restart_generation": 7,
                }
            )
        )

        tee_written: list = []

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if "tee" in cmd:
                tee_written.append(kwargs.get("input", ""))
            return r

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                    launch,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                        unit_dir,
                    ):
                        executor._ensure_launch_config("DEPLOY")

        if tee_written:
            written = tee_written[0]
            assert "applied_restart_generation" not in written, (
                f"DEPLOY ExecStart must NOT contain applied_restart_generation; got: {written!r}"
            )


# ---------------------------------------------------------------------------
# Tests: end-to-end disk write via service.py path
# ---------------------------------------------------------------------------


class TestAppliedLaunchJsonOnDiskContainsGeneration:
    """End-to-end: after APPLY returns snapshot, applied_launch.json on disk has the generation."""

    def test_applied_launch_json_written_with_generation(self, tmp_path: Path) -> None:
        """service.py writes applied_launch.json = json.dumps(snapshot); verify generation present.

        This test simulates the service.py single-writer path:
          APPLIED_LAUNCH_CONFIG_PATH.write_text(json.dumps(launch_snapshot))
        by calling _ensure_launch_config("APPLY") and then doing the write itself,
        asserting that the resulting JSON contains applied_restart_generation.
        """
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        write_unit(unit_dir, _MAIN_PY_UNIT)
        launch = write_launch(tmp_path, target_restart_generation=7)
        applied_path = tmp_path / "applied_launch.json"

        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        executor = DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")

        tee_written: list = []

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            r = MagicMock()
            if "tee" in cmd:
                r.returncode = 0
                r.stderr = ""
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
                launch,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    snapshot = executor._ensure_launch_config("APPLY")

        assert snapshot is not None, "APPLY must return a snapshot"

        # Simulate exactly what service.py does after successful ensure+restart
        applied_path.write_text(json.dumps(snapshot))

        on_disk = json.loads(applied_path.read_text())
        assert "applied_restart_generation" in on_disk, (
            f"applied_launch.json on disk must contain applied_restart_generation; "
            f"got keys: {list(on_disk.keys())}"
        )
        assert on_disk["applied_restart_generation"] == 7, (
            f"applied_launch.json applied_restart_generation must be 7; got: {on_disk}"
        )
