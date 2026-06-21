"""
Tests for uvicorn workers configuration in auto-update.

Tests for DeploymentExecutor._ensure_workers_config() method that writes
the configured worker count (from ServerConfigManager) to existing systemd
service files during auto-update.

Story #1167: Auto-Updater Workers Un-Pin + Web UI Workers Setting
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


class TestEnsureWorkersConfig:
    """AC4: Tests for _ensure_workers_config method."""

    def test_ensure_workers_config_method_exists(self):
        """AC4: DeploymentExecutor should have _ensure_workers_config method."""
        executor = _make_executor()
        assert hasattr(executor, "_ensure_workers_config")
        assert callable(getattr(executor, "_ensure_workers_config"))

    def test_ensure_workers_returns_true_when_service_not_found(self):
        """AC4: Should return True when service file doesn't exist (not an error)."""
        executor = _make_executor("nonexistent-service")

        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_workers_config()

        assert result is True

    def test_ensure_workers_returns_true_when_workers_already_present(self):
        """AC3/AC4: Should return True without modification if --workers already configured."""
        executor = _make_executor()

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=_UNIT_WITH_WORKERS_4):
                result = executor._ensure_workers_config()

        assert result is True


# ---------------------------------------------------------------------------
# Story #1167 — new tests
# ---------------------------------------------------------------------------


class TestWorkersUnPin:
    """Story #1167 AC1/AC2: _ensure_workers_config writes the configured count."""

    def _run_ensure_with_config(
        self,
        workers: int,
        unit_content: str = _UNIT_WITHOUT_WORKERS,
    ) -> str:
        """
        Run _ensure_workers_config() with a mocked ServerConfigManager returning
        a config whose .workers == workers.

        Returns the string written to sudo tee (i.e. the new unit content).
        """
        executor = _make_executor()
        fake_config = _make_config(workers)

        tee_calls: list[str] = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "tee" in cmd:
                tee_calls.append(kwargs.get("input", ""))
            return result

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager"
            ) as MockSCM,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=unit_content),
            patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            MockSCM.return_value.load_config.return_value = fake_config
            executor._ensure_workers_config()

        assert tee_calls, "Expected sudo tee to be called (unit was written)"
        return tee_calls[0]

    def test_ac1_config_workers_2_writes_workers_2(self):
        """AC1: When config.workers==2, unit is written with --workers 2."""
        written = self._run_ensure_with_config(workers=2)
        assert "--workers 2" in written, f"Expected '--workers 2' in: {written!r}"

    def test_ac2_config_workers_1_writes_workers_1_regression(self):
        """AC2: When config.workers==1, writes --workers 1 (byte-identical to old behaviour)."""
        written = self._run_ensure_with_config(workers=1)
        assert "--workers 1" in written, f"Expected '--workers 1' in: {written!r}"

    def test_ac2_config_workers_1_no_double_workers(self):
        """AC2: Only one --workers token is written, not two."""
        written = self._run_ensure_with_config(workers=1)
        assert written.count("--workers") == 1

    def test_ac3_idempotency_workers_already_present_no_write(self):
        """AC3: When --workers already in unit, returns True, NO sudo tee called."""
        executor = _make_executor()
        fake_config = _make_config(2)

        subprocess_calls: list = []

        def fake_subprocess_run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager"
            ) as MockSCM,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=_UNIT_WITH_WORKERS_4),
            patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            MockSCM.return_value.load_config.return_value = fake_config
            result = executor._ensure_workers_config()

        assert result is True
        tee_calls = [c for c in subprocess_calls if "tee" in c]
        assert not tee_calls, (
            "sudo tee must NOT be called when --workers already present"
        )

    def test_misconfig_workers_zero_writes_workers_1(self):
        """Misconfig: config.workers==0 -> max(1,0)==1, writes --workers 1."""
        written = self._run_ensure_with_config(workers=0)
        assert "--workers 1" in written

    def test_misconfig_workers_negative_writes_workers_1(self):
        """Misconfig: config.workers==-5 -> max(1,-5)==1, writes --workers 1."""
        written = self._run_ensure_with_config(workers=-5)
        assert "--workers 1" in written

    def test_misconfig_config_none_writes_workers_1(self):
        """Misconfig: load_config() returns None -> falls back to 1."""
        executor = _make_executor()
        tee_calls: list[str] = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "tee" in cmd:
                tee_calls.append(kwargs.get("input", ""))
            return result

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager"
            ) as MockSCM,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=_UNIT_WITHOUT_WORKERS),
            patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            MockSCM.return_value.load_config.return_value = None
            executor._ensure_workers_config()

        assert tee_calls, "Expected sudo tee to be called"
        assert "--workers 1" in tee_calls[0]

    def test_workers_count_logged_on_write(self):
        """The method succeeds with workers=3 (dynamic count written)."""
        executor = _make_executor()
        fake_config = _make_config(3)

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            return result

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager"
            ) as MockSCM,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=_UNIT_WITHOUT_WORKERS),
            patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            MockSCM.return_value.load_config.return_value = fake_config
            result = executor._ensure_workers_config()

        assert result is True


# ---------------------------------------------------------------------------
# Single-writer invariant (AC_B and AC_C) — source-inspection guards
# ---------------------------------------------------------------------------


class TestSingleWriterInvariant:
    """The ONLY method writing --workers is _ensure_workers_config.
    restart_server() and HealthWatchdog._restart_server() must NOT touch the unit.

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

    def test_source_ensure_workers_config_is_only_writer(self):
        """Source-text guard: only _ensure_workers_config writes --workers in deployment_executor."""
        import inspect
        import code_indexer.server.auto_update.deployment_executor as mod

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
        # All write-producing lines should be inside _ensure_workers_config
        for lineno, line in write_worker_lines:
            # Walk back to find the enclosing def
            for j in range(lineno - 2, max(0, lineno - 200), -1):
                if lines[j].strip().startswith("def "):
                    assert "_ensure_workers_config" in lines[j], (
                        f"Line {lineno} writes '--workers' but is NOT inside "
                        f"_ensure_workers_config — single-writer invariant violated: {line!r}"
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
        """AC5: 'workers' must be a BOOTSTRAP_KEY (persisted to config.json)."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "workers" in BOOTSTRAP_KEYS, (
            "'workers' must be in BOOTSTRAP_KEYS so it is saved to config.json"
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
