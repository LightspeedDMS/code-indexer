"""
TDD tests for Story #1199 AC2: DEPLOY mode reads applied_launch.json (NEVER launch.json),
corrupt or missing applied_launch.json preserves live ExecStart, DEPLOY always returns None.

Behavioral: real temp files, patched subprocess only.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_MAIN_PY_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 0.0.0.0 --port 8000

[Install]
WantedBy=multi-user.target
"""


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


def _fake_run_ok(cmd: list, **kwargs: Any) -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stderr = ""
    r.stdout = ""
    return r


def run_deploy(
    executor: Any,
    unit_dir: Path,
    applied_path: Path,
    launch_path: Path,
) -> tuple:
    """Run DEPLOY mode; patch subprocess + both config paths. Returns (result, written_or_None)."""
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
            applied_path,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                launch_path,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    result = executor._ensure_launch_config("DEPLOY")

    return result, (tee_written[0] if tee_written else None)


class TestAC2DeployMode:
    """AC2: DEPLOY mode source, return value, and corrupt-applied behaviour."""

    def test_deploy_returns_none_on_success(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2: DEPLOY always returns None even when ExecStart is rewritten."""
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 4})
        )
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "1.2.3.4", "port": 7777, "workers": 99}))

        result, _ = run_deploy(executor, unit_dir, applied, launch)

        assert result is None, f"DEPLOY must always return None; got: {result!r}"

    def test_deploy_reads_applied_not_launch(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2 BEHAVIORAL: DEPLOY uses applied_launch.json values, not launch.json values."""
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9999, "workers": 5})
        )
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "1.2.3.4", "port": 1111, "workers": 99}))

        _, written = run_deploy(executor, unit_dir, applied, launch)

        assert written is not None, "DEPLOY must rewrite ExecStart from applied values"
        assert "--port 9999" in written, (
            f"DEPLOY must use applied_launch.json port (9999), not launch.json (1111). "
            f"Got: {written!r}"
        )
        assert "--port 1111" not in written, (
            f"DEPLOY must NOT use launch.json port (1111). Got: {written!r}"
        )

    def test_deploy_corrupt_applied_returns_none(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2 BEHAVIORAL: Corrupt applied_launch.json → return None, do not touch ExecStart."""
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text("not valid json {{{")
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "1.2.3.4", "port": 1111, "workers": 99}))

        result, written = run_deploy(executor, unit_dir, applied, launch)

        assert result is None, (
            f"Corrupt applied_launch.json must return None; got: {result!r}"
        )
        assert written is None, (
            f"Corrupt applied_launch.json must NOT rewrite ExecStart. Got: {written!r}"
        )

    def test_deploy_missing_applied_falls_back_to_defaults(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2/AC4 BEHAVIORAL: Missing applied_launch.json → first-boot fallback to config.json
        then ServerConfig defaults. ExecStart IS rewritten from defaults (port 8000).
        DEPLOY must NOT return None for the missing case — that would make first-boot a no-op.

        AC2 (first-boot gherkin): 'applied_launch.json does NOT exist yet (first boot)
        → DEPLOY mode falls back to config.json -> ServerConfig defaults (NOT launch.json/TARGET)'

        The CORRUPT case (applied exists but is malformed) is what triggers preserve/None.
        MISSING triggers the defaults fallback path.
        """
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"  # deliberately NOT created
        launch = tmp_path / "launch.json"
        # launch.json has different values — DEPLOY must NOT read these
        launch.write_text(json.dumps({"host": "1.2.3.4", "port": 1111, "workers": 99}))

        result, written = run_deploy(executor, unit_dir, applied, launch)

        # DEPLOY returns None (it never returns a snapshot), but it MUST have rewritten
        # ExecStart from config.json→defaults (port 8000 default, not 1111 from launch.json)
        assert result is None, f"DEPLOY must always return None; got: {result!r}"
        assert written is not None, (
            "Missing applied_launch.json (first boot) must still rewrite ExecStart "
            "from config.json→defaults. DEPLOY must NOT be a no-op on first boot."
        )
        assert "--port 1111" not in written, (
            f"DEPLOY must NOT use launch.json port (1111) even when applied is missing. "
            f"Got: {written!r}"
        )

    def test_deploy_corrupt_distinct_from_missing_preserves_live(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2 BEHAVIORAL: CORRUPT applied_launch.json → preserve live ExecStart (no rewrite).
        MISSING and CORRUPT are TWO DISTINCT cases — not the same 'preserve' path.

        CORRUPT: applied file EXISTS but cannot be parsed → preserve live ExecStart.
        MISSING: applied file does NOT exist → fall through to defaults.
        """
        # Unit has distinct existing ExecStart — corrupt should leave it untouched
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text("not valid json {{{")  # corrupt
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "1.2.3.4", "port": 1111, "workers": 99}))

        result, written = run_deploy(executor, unit_dir, applied, launch)

        assert result is None, f"DEPLOY must always return None; got: {result!r}"
        assert written is None, (
            "Corrupt applied_launch.json must preserve live ExecStart (no rewrite). "
            f"Got written: {written!r}"
        )

    def test_deploy_rewrites_workers_from_applied(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2: DEPLOY rewrites --workers from applied_launch.json."""
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(json.dumps({"host": "0.0.0.0", "port": 8000, "workers": 8}))
        launch = tmp_path / "launch.json"

        _, written = run_deploy(executor, unit_dir, applied, launch)

        assert written is not None, (
            "DEPLOY must rewrite ExecStart when applied values differ"
        )
        assert "--workers 8" in written, f"Expected --workers 8; got: {written!r}"

    def test_invalid_mode_returns_none(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2 / mode validation: invalid mode string returns None without touching files."""
        write_unit(unit_dir)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 2})
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
            unit_dir,
        ):
            result = executor._ensure_launch_config("BOGUS")

        assert result is None, f"Invalid mode must return None; got: {result!r}"

    def test_deploy_idempotent_no_op_returns_none(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC2: DEPLOY returns None even when ExecStart already matches (idempotent no-op)."""
        # Write unit that already has the exact values from applied_launch.json
        unit_content = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 127.0.0.1 --port 9001 --workers 3

[Install]
WantedBy=multi-user.target
"""
        write_unit(unit_dir, unit_content)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 3})
        )
        launch = tmp_path / "launch.json"

        result, written = run_deploy(executor, unit_dir, applied, launch)

        assert result is None, f"DEPLOY no-op must return None; got: {result!r}"
        assert written is None, f"DEPLOY no-op must not call tee; got: {written!r}"


class TestExecuteStep3DeployMode:
    """Defect A: execute() Step 3 must call _ensure_launch_config('DEPLOY'), not _ensure_workers_config.

    Before Story #1199 Step 3 called _ensure_workers_config() which has a uvicorn-only
    gate — a no-op on all real installer-shape units. After the fix, Step 3 must call
    _ensure_launch_config("DEPLOY") so the broadened predicate and applied-source logic apply.
    """

    def test_execute_step3_calls_ensure_launch_config_deploy(
        self, executor: Any, tmp_path: Path
    ) -> None:
        """Defect A: execute() Step 3 calls _ensure_launch_config('DEPLOY'), not _ensure_workers_config.

        We mock all other steps so only _ensure_launch_config is observable.
        The test fails if Step 3 still calls the old _ensure_workers_config.
        """
        from contextlib import ExitStack
        from unittest.mock import patch as _patch

        launch_config_calls: list = []

        def _fake_launch_config(mode: str) -> None:
            launch_config_calls.append(mode)

        # Patch all steps so execute() runs to completion.
        # _ensure_workers_config is NOT patched — it no longer exists (orphan removed
        # in Story #1199). If execute() were to call it, AttributeError would surface here.
        step_patches = [
            _patch.object(executor, "git_pull", return_value=True),
            _patch.object(executor, "git_submodule_update", return_value=True),
            _patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            _patch.object(executor, "pip_install", return_value=True),
            _patch.object(
                executor, "_ensure_launch_config", side_effect=_fake_launch_config
            ),
            _patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
            _patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            _patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            _patch.object(executor, "_ensure_data_dir_env_var", return_value=True),
            _patch.object(executor, "_ensure_malloc_arena_max", return_value=True),
            _patch.object(executor, "ensure_ripgrep", return_value=True),
            _patch.object(executor, "_ensure_sudoers_restart", return_value=True),
            _patch.object(executor, "_ensure_memory_overcommit", return_value=True),
            _patch.object(executor, "_ensure_swap_file", return_value=True),
            _patch.object(executor, "_ensure_pace_maker_installed", return_value=True),
            _patch.object(executor, "_ensure_rust_toolchain", return_value=True),
        ]

        with ExitStack() as stack:
            for p in step_patches:
                stack.enter_context(p)
            executor.execute()

        assert "DEPLOY" in launch_config_calls, (
            f"execute() Step 3 must call _ensure_launch_config('DEPLOY'); "
            f"got launch_config_calls={launch_config_calls}. "
            f"The old uvicorn-only gate was replaced by _ensure_launch_config which covers "
            f"both installer-shape (code_indexer.server.main) and uvicorn-shape units."
        )
