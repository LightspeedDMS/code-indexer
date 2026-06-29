"""
Tests for workers configuration in auto-update.

Previously tested DeploymentExecutor._ensure_workers_config(); Story #1199 replaced
that method with _ensure_launch_config(mode), which handles host/port/workers together.

Tests retargeted from _ensure_workers_config → _ensure_launch_config("DEPLOY")
for Story #1199 orphan-cleanup. Behavioral equivalence is preserved: the same
ExecStart rewriting logic (_rewrite_execstart_lines / _rewrite_flag / _write_and_reload_service)
is exercised; only the source of the workers value changed from ServerConfigManager
to applied_launch.json.

Story #1167: Auto-Updater Workers Un-Pin + Web UI Workers Setting
Story #1199: _ensure_launch_config replaces _ensure_workers_config
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNIT_WITHOUT_WORKERS = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000

[Install]
WantedBy=multi-user.target
"""

_UNIT_WITH_WORKERS_4 = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 4

[Install]
WantedBy=multi-user.target
"""

_UNIT_WITH_WORKERS_1 = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1

[Install]
WantedBy=multi-user.target
"""


def _make_config(workers: int) -> MagicMock:
    """Return a mock ServerConfig with a specific workers value."""
    cfg = MagicMock()
    cfg.workers = workers
    return cfg


def _make_executor(service_name: str = "cidx-server") -> DeploymentExecutor:
    return DeploymentExecutor(repo_path=Path("/tmp"), service_name=service_name)


# ---------------------------------------------------------------------------
# Original / pre-existing tests (kept intact)
# ---------------------------------------------------------------------------


class TestEnsureLaunchConfigExists:
    """AC4 (retargeted): DeploymentExecutor must expose _ensure_launch_config (Story #1199).

    _ensure_workers_config was removed as orphan code after Step 3 of execute() was
    rewired to call _ensure_launch_config("DEPLOY") instead.
    """

    def test_ensure_launch_config_method_exists(self):
        """AC4: DeploymentExecutor must have _ensure_launch_config method."""
        executor = _make_executor()
        assert hasattr(executor, "_ensure_launch_config")
        assert callable(getattr(executor, "_ensure_launch_config"))

    def test_ensure_workers_config_removed(self):
        """Story #1199: _ensure_workers_config must NOT exist (orphan removed)."""
        executor = _make_executor()
        assert not hasattr(executor, "_ensure_workers_config"), (
            "_ensure_workers_config was an orphan after Story #1199 and must be removed. "
            "Use _ensure_launch_config instead."
        )

    def test_deploy_returns_none_when_service_not_found(self, tmp_path):
        """DEPLOY returns None when service file doesn't exist (no-op, unit dir is empty)."""
        import json

        executor = _make_executor("nonexistent-service")
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 1})
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
            applied,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                unit_dir,
            ):
                result = executor._ensure_launch_config("DEPLOY")

        assert result is None, f"DEPLOY must return None; got: {result!r}"


# ---------------------------------------------------------------------------
# Story #1167 — new tests
# ---------------------------------------------------------------------------


class TestWorkersUnPin:
    """Story #1167 AC1/AC2 (retargeted): _ensure_launch_config("DEPLOY") writes the configured workers.

    Previously tested _ensure_workers_config; Story #1199 replaced it with
    _ensure_launch_config("DEPLOY"). Workers are now sourced from applied_launch.json
    instead of ServerConfigManager. Same ExecStart rewriting behavior is verified.
    """

    def _run_ensure_with_config(
        self,
        tmp_path: Path,
        workers: int,
        unit_content: str = _UNIT_WITHOUT_WORKERS,
    ) -> str:
        """Run _ensure_launch_config("DEPLOY") with workers sourced from applied_launch.json.

        Returns the string written to sudo tee (i.e. the new unit content).
        workers <= 0 are clamped to 1 by _ensure_launch_config (max(1,...)).
        """
        import json

        executor = _make_executor()
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir(exist_ok=True)
        (unit_dir / "cidx-server.service").write_text(unit_content)

        # Supply workers via applied_launch.json (DEPLOY source)
        # Values <= 0 get clamped to 1 by _resolve_launch_values max(1,...)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "0.0.0.0", "port": 8000, "workers": workers})
        )

        tee_calls: list[str] = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "tee" in cmd:
                tee_calls.append(kwargs.get("input", ""))
            return result

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_subprocess_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    executor._ensure_launch_config("DEPLOY")

        assert tee_calls, "Expected sudo tee to be called (unit was written)"
        return tee_calls[0]

    def test_ac1_config_workers_2_writes_workers_2(self, tmp_path):
        """AC1: When applied_launch.json has workers=2, unit is written with --workers 2."""
        written = self._run_ensure_with_config(tmp_path, workers=2)
        assert "--workers 2" in written, f"Expected '--workers 2' in: {written!r}"

    def test_ac2_config_workers_1_writes_workers_1_regression(self, tmp_path):
        """AC2: When applied_launch.json has workers=1, writes --workers 1."""
        written = self._run_ensure_with_config(tmp_path, workers=1)
        assert "--workers 1" in written, f"Expected '--workers 1' in: {written!r}"

    def test_ac2_config_workers_1_no_double_workers(self, tmp_path):
        """AC2: Only one --workers token is written, not two."""
        written = self._run_ensure_with_config(tmp_path, workers=1)
        assert written.count("--workers") == 1

    def test_ac3_idempotency_workers_already_present_no_write(self, tmp_path):
        """AC3: When ExecStart already has the exact --workers value, NO sudo tee called.

        The idempotency guard must be value-aware: '--workers 4' in ExecStart + workers=4
        in applied_launch.json is a no-op. A different count must rewrite
        (tested separately in TestWorkersIdempotencyOnValue).
        """
        import json

        executor = _make_executor()
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        (unit_dir / "cidx-server.service").write_text(_UNIT_WITH_WORKERS_4)

        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 4})
        )

        subprocess_calls: list = []

        def fake_subprocess_run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_subprocess_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    result = executor._ensure_launch_config("DEPLOY")

        assert result is None, f"DEPLOY must return None; got: {result!r}"
        tee_calls = [c for c in subprocess_calls if "tee" in c]
        assert not tee_calls, (
            "sudo tee must NOT be called when --workers already present at correct value"
        )

    def test_misconfig_workers_zero_writes_workers_1(self, tmp_path):
        """Misconfig: applied workers=0 -> max(1,0)==1, writes --workers 1."""
        written = self._run_ensure_with_config(tmp_path, workers=0)
        assert "--workers 1" in written

    def test_misconfig_workers_negative_writes_workers_1(self, tmp_path):
        """Misconfig: applied workers=-5 -> max(1,-5)==1, writes --workers 1."""
        written = self._run_ensure_with_config(tmp_path, workers=-5)
        assert "--workers 1" in written

    def test_misconfig_applied_missing_preserves_live(self, tmp_path):
        """CORRECTED: no applied_launch.json -> preserve live ExecStart (no rewrite).

        This test previously encoded a production-safety bug: it asserted that
        DEPLOY+missing falls through to ServerConfig defaults and rewrites ExecStart.
        In a real cluster, the live ExecStart has --host 0.0.0.0 but config.json has
        host=127.0.0.1 (the ServerConfig default). The old behavior would rewrite
        --host 0.0.0.0 to 127.0.0.1, dropping the node off HAProxy (production outage).

        After the fix: DEPLOY+missing returns None from _read_launch_source (same as
        CORRUPT), so _ensure_launch_config returns without touching the live unit.
        No tee call is expected.
        """
        executor = _make_executor()
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        (unit_dir / "cidx-server.service").write_text(_UNIT_WITHOUT_WORKERS)

        applied = tmp_path / "applied_launch.json"  # deliberately NOT created

        tee_calls: list[str] = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "tee" in cmd:
                tee_calls.append(kwargs.get("input", ""))
            return result

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_subprocess_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    executor._ensure_launch_config("DEPLOY")

        # CORRECTED: no rewrite expected when applied_launch.json is missing
        assert not tee_calls, (
            "DEPLOY + missing applied_launch.json must preserve live ExecStart (no tee call). "
            "The old assertion ('tee must be called with defaults') encoded the production-safety "
            "bug where --host 0.0.0.0 would be rewritten to 127.0.0.1 from config.json defaults. "
            f"Got tee_calls: {tee_calls!r}"
        )

    def test_workers_count_written_for_workers_3(self, tmp_path):
        """DEPLOY succeeds with workers=3 (applied_launch.json value written)."""
        written = self._run_ensure_with_config(tmp_path, workers=3)
        assert "--workers 3" in written, f"Expected '--workers 3' in: {written!r}"


# ---------------------------------------------------------------------------
# Bug #1175 / Story #1167 — idempotency on VALUE not mere presence of --workers
# ---------------------------------------------------------------------------


class TestWorkersIdempotencyOnValue:
    """_ensure_launch_config("DEPLOY") must be idempotent on the EXACT worker count value.

    Retargeted from _ensure_workers_config (Story #1199 orphan-cleanup).
    Workers are now sourced from applied_launch.json. Same invariants apply:

    - Unit has '--workers 1', applied has workers=4 -> REWRITE to '--workers 4'
    - Unit has '--workers 4', applied has workers=4 -> NO-OP (true idempotency)
    """

    def _run_with_unit(
        self,
        tmp_path: Path,
        unit_content: str,
        applied_workers: int,
    ) -> tuple[list[str], object]:
        """Run _ensure_launch_config("DEPLOY") and return (tee_written_contents, return_value)."""
        import json

        executor = _make_executor()
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir(exist_ok=True)
        (unit_dir / "cidx-server.service").write_text(unit_content)

        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "workers": applied_workers})
        )

        tee_calls: list[str] = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "tee" in cmd:
                tee_calls.append(kwargs.get("input", ""))
            return result

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=fake_subprocess_run,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    result = executor._ensure_launch_config("DEPLOY")

        return tee_calls, result

    def test_unit_has_workers_1_applied_is_4_rewrites_to_4(self, tmp_path):
        """Bug B: unit has '--workers 1', applied has workers=4 -> must rewrite to '--workers 4'.

        The old guard ('if \"--workers\" in content: return True') fires here and
        returns without updating, leaving the node stuck at 1 worker. The fix must
        detect the VALUE mismatch and rewrite the ExecStart line.
        """
        tee_calls, result = self._run_with_unit(
            tmp_path, _UNIT_WITH_WORKERS_1, applied_workers=4
        )

        assert tee_calls, (
            "sudo tee must be called when existing '--workers 1' != applied workers=4"
        )
        written = tee_calls[0]
        assert "--workers 4" in written, (
            f"Written content must contain '--workers 4', got: {written!r}"
        )
        assert "--workers 1" not in written, (
            f"Written content must NOT contain old '--workers 1', got: {written!r}"
        )
        assert result is None, f"DEPLOY must return None; got: {result!r}"

    def test_unit_has_workers_4_applied_is_4_no_tee_call(self, tmp_path):
        """Bug B: unit has '--workers 4', applied has workers=4 -> NO-OP (true idempotency).

        When the existing value already matches applied_launch.json, no write should occur.
        """
        tee_calls, result = self._run_with_unit(
            tmp_path, _UNIT_WITH_WORKERS_4, applied_workers=4
        )

        assert not tee_calls, (
            f"sudo tee must NOT be called when '--workers 4' already matches applied; "
            f"got tee calls: {tee_calls}"
        )
        assert result is None, f"DEPLOY must return None; got: {result!r}"


# ---------------------------------------------------------------------------
# Single-writer invariant (AC_B and AC_C) — source-inspection guards
# ---------------------------------------------------------------------------


class TestSingleWriterInvariant:
    """The ONLY method writing --workers is _ensure_launch_config (and its helpers).
    restart_server() and HealthWatchdog._restart_server() must NOT touch the unit.
    _ensure_workers_config was removed as orphan code in Story #1199.

    Verified via source inspection (avoids live HTTP/drain calls that would timeout).
    """

    def test_ac_b_restart_server_source_has_no_workers_write(self):
        """AC_B: restart_server() source must not contain '--workers' token writing."""
        import inspect
        import code_indexer.server.auto_update.deployment_executor as mod

        source_lines = inspect.getsource(mod.DeploymentExecutor.restart_server).split(
            "\n"
        )
        # Look for any line that writes --workers (f-string, format, concat)
        write_lines = [
            line
            for line in source_lines
            if "--workers" in line
            and ('f"' in line or "f'" in line or ".format(" in line or "+ " in line)
        ]
        assert not write_lines, (
            f"restart_server() must not write '--workers' — found: {write_lines}"
        )

    def test_ac_b_restart_server_source_has_no_tee_call(self):
        """AC_B: restart_server() must not call 'sudo tee' (no unit file rewrite)."""
        import inspect
        import code_indexer.server.auto_update.deployment_executor as mod

        source = inspect.getsource(mod.DeploymentExecutor.restart_server)
        assert "tee" not in source, (
            "restart_server() must not contain 'tee' — single-writer invariant violated"
        )

    def test_ac_b_restart_server_source_has_no_execstart_write(self):
        """AC_B: restart_server() must not write ExecStart= line."""
        import inspect
        import code_indexer.server.auto_update.deployment_executor as mod

        source = inspect.getsource(mod.DeploymentExecutor.restart_server)
        assert "ExecStart=" not in source, (
            "restart_server() must not write ExecStart= — single-writer invariant violated"
        )

    def test_ac_c_watchdog_restart_does_not_rewrite_unit(self):
        """AC_C: HealthWatchdog._restart_server() calls systemctl restart only, never tee."""
        from code_indexer.server.auto_update.health_watchdog import HealthWatchdog

        state_file = Path("/tmp/test_watchdog_state.json")
        watcher = HealthWatchdog(
            service_name="cidx-server",
            server_url="http://localhost:8000",
            state_file=state_file,
        )

        subprocess_cmds: list = []

        def fake_run(cmd, **kwargs):
            subprocess_cmds.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            return r

        with patch(
            "code_indexer.server.auto_update.health_watchdog.subprocess.run",
            side_effect=fake_run,
        ):
            watcher._restart_server()

        tee_calls = [c for c in subprocess_cmds if "tee" in c]
        assert not tee_calls, (
            f"HealthWatchdog._restart_server() must not call tee; got: {tee_calls}"
        )

        workers_writes = [
            c for c in subprocess_cmds if any("--workers" in str(arg) for arg in c)
        ]
        assert not workers_writes, (
            "HealthWatchdog._restart_server() must not write --workers"
        )

    def test_ac_c_watchdog_source_has_no_workers_write(self):
        """AC_C: HealthWatchdog._restart_server() source must not contain '--workers'."""
        import inspect
        from code_indexer.server.auto_update.health_watchdog import HealthWatchdog

        source = inspect.getsource(HealthWatchdog._restart_server)
        assert "--workers" not in source, (
            "HealthWatchdog._restart_server() must not write '--workers'"
        )

    def test_source_ensure_launch_config_is_only_writer(self):
        """Source-text guard: only the _ensure_launch_config family writes --workers.

        Story #1199 replaced _ensure_workers_config with _ensure_launch_config.
        _ensure_workers_config was then removed as orphan code (no production callers).
        Permitted writers (single-writer family): _ensure_launch_config,
        _rewrite_execstart_lines, _rewrite_flag, _write_and_reload_service.
        Forbidden: _ensure_workers_config (deleted), restart_server,
        HealthWatchdog._restart_server.
        """
        import inspect
        import code_indexer.server.auto_update.deployment_executor as mod

        _PERMITTED_WRITERS = {
            "_ensure_launch_config",
            "_rewrite_execstart_lines",
            "_rewrite_flag",
            "_write_and_reload_service",
        }

        source = inspect.getsource(mod)
        lines = source.split("\n")
        # Find lines that write --workers (f-string or concatenation)
        write_worker_lines = [
            (i + 1, src_line)
            for i, src_line in enumerate(lines)
            if "--workers" in src_line
            and (
                'f"' in src_line
                or "f'" in src_line
                or ".format(" in src_line
                or '+ "' in src_line
                or "+ '" in src_line
            )
        ]
        # All write-producing lines should be inside a permitted writer method
        for lineno, line in write_worker_lines:
            # Walk back to find the enclosing def
            for j in range(lineno - 2, max(0, lineno - 200), -1):
                if lines[j].strip().startswith("def "):
                    enclosing = lines[j]
                    assert any(name in enclosing for name in _PERMITTED_WRITERS), (
                        f"Line {lineno} writes '--workers' but is NOT inside a permitted "
                        f"single-writer method — single-writer invariant violated.\n"
                        f"Enclosing def: {enclosing.strip()!r}\n"
                        f"Line: {line!r}\n"
                        f"Permitted: {_PERMITTED_WRITERS}"
                    )
                    break


# ---------------------------------------------------------------------------
# AC5: Web UI workers field
# ---------------------------------------------------------------------------


class TestAC5RestartRequiredFields:
    """AC5: 'workers' must be in RESTART_REQUIRED_FIELDS."""

    def test_workers_in_restart_required_fields(self):
        """AC5: routes.py RESTART_REQUIRED_FIELDS must include 'workers'."""
        from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS

        assert "workers" in RESTART_REQUIRED_FIELDS, (
            "RESTART_REQUIRED_FIELDS must contain 'workers'; "
            f"current fields: {RESTART_REQUIRED_FIELDS}"
        )


class TestAC5WorkersValidation:
    """AC5: Validation rejects out-of-range or non-integer workers values."""

    def _validate(self, workers_value) -> object:
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("server", {"workers": workers_value})

    def test_workers_1_is_valid(self):
        assert self._validate(1) is None

    def test_workers_64_is_valid(self):
        assert self._validate(64) is None

    def test_workers_2_is_valid(self):
        assert self._validate(2) is None

    def test_workers_0_is_rejected(self):
        result = self._validate(0)
        assert result is not None, "workers=0 must be rejected"

    def test_workers_negative_is_rejected(self):
        result = self._validate(-1)
        assert result is not None, "workers=-1 must be rejected"

    def test_workers_65_is_rejected(self):
        """AC5: workers > 64 must be rejected (max=64 as per spec)."""
        result = self._validate(65)
        assert result is not None, "workers=65 must be rejected (max is 64)"

    def test_workers_100_is_rejected(self):
        result = self._validate(100)
        assert result is not None, "workers=100 must be rejected"

    def test_workers_string_abc_is_rejected(self):
        result = self._validate("abc")
        assert result is not None, "workers='abc' must be rejected"

    def test_workers_float_string_rejected(self):
        # "1.5" -> int("1.5") raises ValueError -> rejected
        result = self._validate("1.5")
        assert result is not None, "workers='1.5' must be rejected"


class TestAC5WorkersSaveFlow:
    """AC5: _update_server_setting maps 'workers' and save_config_dict persists."""

    def test_update_server_setting_maps_workers(self):
        """AC5: _update_server_setting('workers', 4) sets config.workers=4."""
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.services.config_service import ConfigService

        config = ServerConfig(server_dir="/tmp/fake")
        assert config.workers == 1  # default

        svc = ConfigService.__new__(ConfigService)
        svc._update_server_setting(config, "workers", 4)
        assert config.workers == 4

    def test_update_server_setting_workers_string_int(self):
        """AC5: _update_server_setting coerces string '4' to int 4."""
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.services.config_service import ConfigService

        config = ServerConfig(server_dir="/tmp/fake")
        svc = ConfigService.__new__(ConfigService)
        svc._update_server_setting(config, "workers", "4")
        assert config.workers == 4

    def test_workers_in_bootstrap_keys(self):
        """AC5: 'workers' must be persisted to config.json.

        Story #1197 moved 'workers' from BOOTSTRAP_KEYS to TRANSITION_PRESERVE_KEYS
        so that it continues to be written to config.json during the transition
        release while also being available as a runtime key in the DB.
        """
        from code_indexer.server.services.config_service import TRANSITION_PRESERVE_KEYS

        assert "workers" in TRANSITION_PRESERVE_KEYS, (
            "'workers' must be in TRANSITION_PRESERVE_KEYS so it is saved to config.json. "
            "Story #1197 moved it from BOOTSTRAP_KEYS to TRANSITION_PRESERVE_KEYS."
        )


class TestAC5HtmlField:
    """AC5: config_section.html contains workers display row and edit input."""

    def _read_template(self) -> str:
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src/code_indexer/server/web/templates/partials/config_section.html"
        )
        return template_path.read_text()

    def test_display_table_has_workers_row(self):
        """AC5: display table shows a workers value via {{ config.server.workers }}."""
        content = self._read_template()
        assert "config.server.workers" in content, (
            "Display table must render {{ config.server.workers }}"
        )

    def test_edit_form_has_workers_input(self):
        """AC5: edit form has name='workers' input."""
        content = self._read_template()
        assert 'name="workers"' in content, (
            "Edit form must have an input with name='workers'"
        )

    def test_display_table_workers_has_restart_note(self):
        """AC5: workers display row references 'workers' in restart_required_fields."""
        content = self._read_template()
        assert "'workers' in restart_required_fields" in content, (
            "Display table must show restart note for 'workers' setting"
        )
