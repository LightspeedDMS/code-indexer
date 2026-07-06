"""Story #1197 AC5: Applied-worker-count resolver (CRITICAL-C2).
Story #1196 (next-release cleanup): drops the resolver's config.json rung (FIX-1b).

Priority (post Story #1196 cleanup):
  1. Live systemd ExecStart --workers  (Bug #1239 fix -- ground truth of the
     running process).
  2. applied_launch.json["workers"]   (auto-updater-owned APPLIED file, Story #3).
  3. ServerConfig default: 1.

Story #1197 originally introduced the chain applied_launch.json.workers ->
config.json -> ServerConfig default 1.  Story #1196 removes the config.json
launch-key copies themselves (config_service.py AC1) and, in lockstep, this
resolver's config.json rung (AC3): there is no longer a middle rung -- a node
missing applied_launch.json falls straight to the default 1.

Both consumers (governor, cache initializer) must be rerouted through the
resolver (unaffected by this cleanup, still verified below).
"""

import inspect
import json
from pathlib import Path

import pytest


class TestResolverBasicBehavior:
    """Basic resolver behavior: source priority and fallbacks."""

    def test_resolver_module_is_importable(self) -> None:
        """The resolver module must exist and be importable."""
        from code_indexer.server.services import applied_worker_count  # noqa: F401

    def test_resolver_function_exists(self) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        assert callable(get_applied_worker_count)

    def test_resolver_reads_applied_launch_json_first(self, tmp_path: Path) -> None:
        """Resolver returns applied_launch.json workers when file exists."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 4}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 4, (
            f"Resolver must return APPLIED (4) from applied_launch.json. Got {result}"
        )

    def test_resolver_returns_1_when_all_sources_absent(self, tmp_path: Path) -> None:
        """No applied_launch.json, no ExecStart -> returns default 1."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 1, f"Default fallback: expected 1, got {result}"


class TestFix1bResolverDropsConfigJsonRung:
    """Story #1196 FIX-1 proof test (b): resolver drops config.json after cleanup.

    Story #1197 introduced chain applied_launch.json.workers -> config.json ->
    ServerConfig default 1.  Story #1196 removes the config.json launch-key
    copies (config_service.py AC1) and, in lockstep, this resolver's
    config.json rung: the post-cleanup chain is applied_launch.json.workers ->
    ServerConfig default 1 (no config.json rung at all).
    """

    def test_missing_applied_launch_falls_to_default_not_config_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIX-1(b): applied_launch.json ABSENT + a config.json with a stale
        workers value present in the SAME directory -> resolver returns the
        default 1, NOT config.json's stale value.

        Uses CIDX_DATA_DIR (the real production resolution path, matched by
        both data_dir and the removed config_dir) so this genuinely exercises
        -- and, pre-fix, exposes -- the config.json rung being removed.
        """
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 8, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(unit_file=tmp_path / "nonexistent.service")
        assert result == 1, (
            f"FIX-1(b): post-cleanup resolver must fall to the ServerConfig "
            f"default 1, NOT config.json's stale workers=8. Got {result}"
        )

    def test_applied_launch_json_missing_workers_key_falls_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """applied_launch.json exists but lacks 'workers' -> falls to default 1,
        NOT config.json (the config.json rung no longer exists)."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
        (tmp_path / "applied_launch.json").write_text(json.dumps({"other_key": 5}))
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 2, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(unit_file=tmp_path / "nonexistent.service")
        assert result == 1, (
            f"Missing workers key must fall to default 1 (no config.json rung "
            f"left to catch it). Got {result}"
        )

    def test_resolver_no_longer_accepts_config_dir_param(self) -> None:
        """The config_dir parameter (dependency injection for the now-removed
        config.json rung) must be gone from the resolver's signature."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        sig = inspect.signature(get_applied_worker_count)
        assert "config_dir" not in sig.parameters, (
            "Story #1196: config_dir parameter must be removed -- there is no "
            "config.json rung left to inject a directory for."
        )

    def test_resolver_still_returns_applied_when_present_regression(
        self, tmp_path: Path
    ) -> None:
        """Regression: applied_launch.json present -> still returns APPLIED (4)."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 4}))
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 8, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 4, (
            f"Regression: applied_launch.json must still win over any stray "
            f"config.json (never read anymore). Got {result}"
        )
        assert result != 8, "Resolver must NOT return config.json's stale value"


class TestResolverFailSoftBehavior:
    """Resolver must be fail-soft: bad inputs → default 1."""

    def test_corrupt_applied_launch_json_falls_back(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text("INVALID {{{{")

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 1, f"Corrupt applied_launch.json must return 1, got {result}"

    def test_resolver_enforces_minimum_1_on_zero_value(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 0}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result >= 1, f"Resolver must enforce minimum 1, got {result}"

    def test_resolver_enforces_minimum_1_on_negative_value(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": -3}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result >= 1, f"Resolver must enforce minimum 1 on negative, got {result}"

    def test_resolver_enforces_minimum_1_on_non_int_value(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": "four"}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 1, f"Non-int workers must fall back to 1, got {result}"


class TestResolverEnvBasedResolution:
    """Resolver default-arg path uses CIDX_DATA_DIR env or ~/.cidx-server."""

    def test_resolver_uses_cidx_data_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 6}))

        result = get_applied_worker_count(unit_file=tmp_path / "nonexistent.service")
        assert result == 6, f"Must use CIDX_DATA_DIR env, got {result}"

    def test_resolver_no_args_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling resolver with no args must not raise even when files absent."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        # Point both data and unit-file dirs to non-existent paths so all
        # priorities fall through to the default, host-independent of any
        # real /etc/systemd/system/cidx-server.service on the test machine.
        monkeypatch.setenv("CIDX_DATA_DIR", "/nonexistent-path-xyz-1197")
        monkeypatch.setenv("SYSTEMD_UNIT_DIR", "/nonexistent-path-xyz-1197")
        result = get_applied_worker_count()
        assert result == 1, f"No-args safe fallback must return 1, got {result}"


class TestExecStartPriority1239:
    """Bug #1239: live systemd ExecStart is Priority 1 for applied worker count.

    Root cause: on first v11 auto-update deploy from 10.141.0, the unit file has
    no --workers token (uvicorn default = 1 worker), but applied_launch.json was
    absent so an older Priority 2 (config.json) was used, silently
    under-resourcing the single running worker until the next Web-UI restart.

    Post Story #1196 cleanup, the priority chain is:
      - ExecStart present with --workers N  -> return N
      - ExecStart present, no --workers     -> return 1 (uvicorn default = ground truth)
      - ExecStart unreadable/absent         -> fall through to applied_launch.json
      - applied_launch.json absent/invalid  -> fall through to ServerConfig default 1
    """

    # Minimal unit-file content; _is_cidx_execstart requires "uvicorn" in line
    _EXECSTART_WITH_WORKERS = (
        "ExecStart=/usr/bin/python3 -m uvicorn code_indexer.server.app:app "
        "--host 0.0.0.0 --port 8001 --workers {workers}"
    )
    _EXECSTART_NO_WORKERS = (
        "ExecStart=/usr/bin/python3 -m uvicorn code_indexer.server.app:app "
        "--host 0.0.0.0 --port 8001"
    )

    def _write_unit_file(self, path: Path, execstart_line: str) -> None:
        path.write_text(f"[Service]\n{execstart_line}\n")

    def test_execstart_no_workers_returns_1_not_applied_value(
        self, tmp_path: Path
    ) -> None:
        """CORE REGRESSION GUARD (Bug #1239).

        First-deploy case: ExecStart exists but has NO --workers token.
        applied_launch.json says workers=4, but uvicorn was launched with 1
        (default). Resolver MUST return 1, NOT 4.
        """
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        unit_file = tmp_path / "cidx-server.service"
        self._write_unit_file(unit_file, self._EXECSTART_NO_WORKERS)
        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 4}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=unit_file,
        )
        assert result == 1, (
            f"Bug #1239 regression: ExecStart found but no --workers token "
            f"must return 1 (uvicorn default), not applied_launch.json's 4. "
            f"Got {result}"
        )

    def test_execstart_with_workers_4_returns_4(self, tmp_path: Path) -> None:
        """ExecStart has --workers 4 -> returns 4 (ExecStart is Priority 1)."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        unit_file = tmp_path / "cidx-server.service"
        self._write_unit_file(unit_file, self._EXECSTART_WITH_WORKERS.format(workers=4))
        # applied_launch.json would have been Priority 1 before Bug #1239's fix
        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 8}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=unit_file,
        )
        assert result == 4, (
            f"ExecStart --workers 4 must be Priority 1 (beats applied_launch=8). "
            f"Got {result}"
        )

    def test_execstart_absent_falls_through_to_applied_launch(
        self, tmp_path: Path
    ) -> None:
        """ExecStart absent + applied_launch.json workers=3 -> returns 3 (Priority 2 preserved)."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 3}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 3, (
            f"ExecStart absent -> fall through to applied_launch.json workers=3. Got {result}"
        )

    def test_execstart_and_applied_both_absent_returns_default(
        self, tmp_path: Path
    ) -> None:
        """ExecStart absent + no applied_launch.json -> default 1 (no config.json rung)."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=tmp_path / "nonexistent.service",
        )
        assert result == 1, f"No sources available -> default 1. Got {result}"

    def test_fail_soft_unreadable_unit_file_falls_through(self, tmp_path: Path) -> None:
        """Fail-soft: error reading unit file must fall through, never propagate."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        # A directory at the unit_file path -> read_text() raises IsADirectoryError
        unit_file = tmp_path / "cidx-server.service"
        unit_file.mkdir()
        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 3}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path),
            unit_file=unit_file,
        )
        assert result == 3, (
            f"Fail-soft: unreadable unit file must fall through to "
            f"applied_launch.json workers=3. Got {result}"
        )


class TestConsumersReroutedToResolver:
    """Both consumers must use the resolver, not get_config().workers directly."""

    def test_governor_read_config_workers_uses_resolver(self) -> None:
        """ProviderConcurrencyGovernor._read_config_workers must call the resolver."""
        from code_indexer.server.services.provider_concurrency_governor import (
            ProviderConcurrencyGovernor,
        )

        source = inspect.getsource(ProviderConcurrencyGovernor._read_config_workers)
        assert (
            "applied_worker_count" in source or "get_applied_worker_count" in source
        ), (
            "Story #1197 AC5: ProviderConcurrencyGovernor._read_config_workers must "
            "use the applied-worker-count resolver, not get_config().workers directly. "
            f"Source:\n{source}"
        )

    def test_service_init_uses_resolver_not_get_config(self) -> None:
        """service_init.py cache-init block must reference the resolver module."""
        # Derive path from the module itself (no hardcoded absolute paths)
        import code_indexer.server.startup.service_init as _svc_init_mod

        svc_init_path = Path(inspect.getfile(_svc_init_mod))
        source = svc_init_path.read_text()

        assert (
            "applied_worker_count" in source or "get_applied_worker_count" in source
        ), (
            "Story #1197 AC5: service_init.py cache worker-count init must "
            "use the applied-worker-count resolver, not get_config().workers directly"
        )
