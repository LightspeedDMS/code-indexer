"""Story #1197 AC5: Applied-worker-count resolver (CRITICAL-C2).

RED-phase tests — all must FAIL before production code is written.

The resolver reads applied_launch.json.workers FIRST (the auto-updater-owned
APPLIED file), falls back to config.json workers, then to ServerConfig default 1.
Both consumers (governor, cache initializer) must be rerouted through the resolver.
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
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 8, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 4, (
            f"Resolver must return APPLIED (4) from applied_launch.json, not TARGET (8). Got {result}"
        )

    def test_resolver_mismatch_target_vs_applied(self, tmp_path: Path) -> None:
        """CRITICAL-C2: Target=8, applied_launch.json says workers=4 → return 4."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 4}))
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 8, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 4, f"CRITICAL-C2: must return APPLIED=4, got {result}"
        assert result != 8, "Resolver must NOT return the saved TARGET (8)"

    def test_resolver_falls_back_to_config_json_when_no_applied_launch(
        self, tmp_path: Path
    ) -> None:
        """No applied_launch.json → falls back to config.json workers."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 3, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 3, f"Fallback to config.json: expected 3, got {result}"

    def test_resolver_returns_1_when_both_absent(self, tmp_path: Path) -> None:
        """Both files absent → returns default 1."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 1, f"Default fallback: expected 1, got {result}"


class TestResolverFailSoftBehavior:
    """Resolver must be fail-soft: bad inputs → default 1."""

    def test_corrupt_applied_launch_json_falls_back(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text("INVALID {{{{")

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 1, f"Corrupt applied_launch.json must return 1, got {result}"

    def test_applied_launch_json_missing_workers_key_falls_back(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"other_key": 5}))
        (tmp_path / "config.json").write_text(
            json.dumps({"workers": 2, "server_dir": str(tmp_path)})
        )

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result == 2, (
            f"Missing workers key → config.json fallback → expected 2, got {result}"
        )

    def test_resolver_enforces_minimum_1_on_zero_value(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": 0}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
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
            data_dir=str(tmp_path), config_dir=str(tmp_path)
        )
        assert result >= 1, f"Resolver must enforce minimum 1 on negative, got {result}"

    def test_resolver_enforces_minimum_1_on_non_int_value(self, tmp_path: Path) -> None:
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        (tmp_path / "applied_launch.json").write_text(json.dumps({"workers": "four"}))

        result = get_applied_worker_count(
            data_dir=str(tmp_path), config_dir=str(tmp_path)
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

        result = get_applied_worker_count()
        assert result == 6, f"Must use CIDX_DATA_DIR env, got {result}"

    def test_resolver_no_args_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling resolver with no args must not raise even when files absent."""
        from code_indexer.server.services.applied_worker_count import (
            get_applied_worker_count,
        )

        # Point to a non-existent dir so we exercise the safe fallback
        monkeypatch.setenv("CIDX_DATA_DIR", "/nonexistent-path-xyz-1197")
        result = get_applied_worker_count()
        assert result == 1, f"No-args safe fallback must return 1, got {result}"


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
