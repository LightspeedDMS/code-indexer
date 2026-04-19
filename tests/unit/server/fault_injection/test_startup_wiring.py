"""
Tests for fault injection startup wiring scenarios.

Story #746 — Phase E startup guards.

Covers Scenarios 1, 2, 3, 4 from the story spec:
  1 — harness disabled: app.state.fault_injection_service=None, endpoints return 404
  2 — enabled but ack missing: sys.exit(1)
  3 — enabled on production: sys.exit(1), critical log says "production"
  4 — enabled + ack + non-production: harness live, WARNING logged, service on app.state
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.fault_injection.startup import wire_fault_injection
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _make_config(
    enabled: bool = False,
    ack: bool = False,
    deployment_env: str = "development",
) -> SimpleNamespace:
    """Build a minimal ServerConfig-compatible object for wiring tests."""
    telemetry = SimpleNamespace(deployment_environment=deployment_env)
    return SimpleNamespace(
        fault_injection_enabled=enabled,
        fault_injection_nonprod_ack=ack,
        telemetry_config=telemetry,
    )


def _make_app() -> FastAPI:
    """Create a bare FastAPI app without auth middleware for wiring tests."""
    return FastAPI()


# ---------------------------------------------------------------------------
# Scenario 1: harness disabled
# ---------------------------------------------------------------------------


class TestScenario1HarnessDisabled:
    """When fault_injection_enabled=false, harness is inactive."""

    def test_returns_none(self):
        app = _make_app()
        result = wire_fault_injection(app, _make_config(enabled=False))
        assert result is None

    def test_fault_injection_service_is_none_on_app_state(self):
        app = _make_app()
        wire_fault_injection(app, _make_config(enabled=False))
        assert app.state.fault_injection_service is None

    def test_http_client_factory_set_on_app_state(self):
        app = _make_app()
        wire_fault_injection(app, _make_config(enabled=False))
        assert isinstance(app.state.http_client_factory, HttpClientFactory)

    def test_status_endpoint_returns_404_when_disabled(self):
        """Scenario 1: /admin/fault-injection/status returns 404 when harness inactive."""
        app = _make_app()
        wire_fault_injection(app, _make_config(enabled=False))
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/admin/fault-injection/status")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 2: enabled but ack missing
# ---------------------------------------------------------------------------


class TestScenario2AckMissing:
    """fault_injection_enabled=true + ack=false → sys.exit(1)."""

    def test_exits_with_code_1(self):
        app = _make_app()
        cfg = _make_config(enabled=True, ack=False, deployment_env="development")
        with pytest.raises(SystemExit) as exc_info:
            wire_fault_injection(app, cfg)
        assert exc_info.value.code == 1

    def test_logs_critical_message_about_ack(self, caplog):
        app = _make_app()
        cfg = _make_config(enabled=True, ack=False)
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                wire_fault_injection(app, cfg)
        assert any("nonprod_ack" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Scenario 3: enabled on production
# ---------------------------------------------------------------------------


class TestScenario3Production:
    """fault_injection_enabled=true + production env → sys.exit(1)."""

    def test_exits_with_code_1(self):
        app = _make_app()
        cfg = _make_config(enabled=True, ack=True, deployment_env="production")
        with pytest.raises(SystemExit) as exc_info:
            wire_fault_injection(app, cfg)
        assert exc_info.value.code == 1

    def test_logs_critical_message_mentioning_production(self, caplog):
        app = _make_app()
        cfg = _make_config(enabled=True, ack=True, deployment_env="production")
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                wire_fault_injection(app, cfg)
        critical_messages = [
            r.message for r in caplog.records if r.levelno == logging.CRITICAL
        ]
        assert any("production" in m.lower() for m in critical_messages)
        # Production guard fires — ack-missing message must NOT appear
        assert not any("nonprod_ack" in m for m in critical_messages)


# ---------------------------------------------------------------------------
# Scenario 4: harness live (enabled + ack + non-production)
# ---------------------------------------------------------------------------


class TestScenario4HarnessLive:
    """fault_injection_enabled=true + ack=true + non-production → harness active."""

    def _wire(self) -> tuple:
        app = _make_app()
        cfg = _make_config(enabled=True, ack=True, deployment_env="staging")
        svc = wire_fault_injection(app, cfg)
        return app, svc

    def test_returns_fault_injection_service(self):
        _, svc = self._wire()
        assert isinstance(svc, FaultInjectionService)

    def test_service_is_enabled(self):
        _, svc = self._wire()
        assert svc.enabled is True

    def test_service_stored_on_app_state(self):
        app, svc = self._wire()
        assert app.state.fault_injection_service is svc

    def test_http_client_factory_stored_on_app_state(self):
        app, _ = self._wire()
        assert isinstance(app.state.http_client_factory, HttpClientFactory)

    def test_warning_logged(self, caplog):
        app = _make_app()
        cfg = _make_config(enabled=True, ack=True, deployment_env="development")
        with caplog.at_level(logging.WARNING):
            wire_fault_injection(app, cfg)
        assert any(
            "FAULT INJECTION HARNESS ACTIVE" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Coverage gap: startup.py branch 92->94 (telemetry_config is None)
# ---------------------------------------------------------------------------


class TestTelemetryConfigNone:
    """Cover startup.py branch where config.telemetry_config is None."""

    def test_production_guard_skipped_when_telemetry_config_is_none(self):
        """When telemetry_config is None, deployment_env defaults to '' (non-prod).
        The harness must activate without error (branch 92->94 taken)."""
        app = _make_app()
        cfg = SimpleNamespace(
            fault_injection_enabled=True,
            fault_injection_nonprod_ack=True,
            telemetry_config=None,
        )
        wire_fault_injection(app, cfg)
        assert isinstance(app.state.fault_injection_service, FaultInjectionService)
