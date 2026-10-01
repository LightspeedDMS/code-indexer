"""Front-door parity for configuration and server-secret audit rows.

Every configuration write path records exactly one row per request naming
the authenticated caller and the door.  Rows name the changed keys; values
are recorded only for allowlisted non-secret scalar keys, so no secret
(provider key, client secret, CI token, PAT, URL credentials) ever reaches
a row.  A rejected change publishes nothing and records one failure row.
See ``_audit_front_doors`` for the harness.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import CAPTURE_LOGGER, capture_errors
from _audit_front_doors import DoorsEnv, assert_attributed, front_door_env

_SENTINEL = "SENTINEL-7f3a-config"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    from code_indexer.server.auth.oidc import routes as oidc_routes

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(oidc_routes, "oidc_manager", oidc_routes.oidc_manager)
    monkeypatch.setattr(oidc_routes, "state_manager", oidc_routes.state_manager)
    yield from front_door_env(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


def _config_row(env: DoorsEnv, source: str, target: str):
    row = env.only_row("config_changed")
    assert_attributed(row, source=source)
    assert row.target_id == target
    return row


def _no_secret(env: DoorsEnv) -> None:
    assert _SENTINEL not in env.store.all_raw_text()


class TestWebConfigSections:
    def test_section_records_changed_keys_and_allowlisted_values(self, env) -> None:
        env.web(
            "POST",
            "/admin/config/data_retention",
            data={"audit_logs_retention_hours": "100"},
        )
        row = _config_row(env, "web", "data_retention")
        assert row.details["change_kind"] == "update"
        assert row.details["changed_keys"] == [
            "data_retention_config.audit_logs_retention_hours"
        ]
        assert row.details["values"] == {
            "data_retention_config.audit_logs_retention_hours": [2160, 100]
        }
        live = env.config_service.get_config()
        assert live.data_retention_config.audit_logs_retention_hours == 100

    def test_totp_elevation_section(self, env) -> None:
        env.web(
            "POST",
            "/admin/config/totp_elevation",
            data={
                "elevation_enforcement_enabled": "true",
                "elevation_idle_timeout_seconds": "600",
                "elevation_max_age_seconds": "1800",
            },
        )
        row = _config_row(env, "web", "totp_elevation")
        assert row.details["values"]["elevation_enforcement_enabled"] == [False, True]
        assert env.config_service.get_config().elevation_idle_timeout_seconds == 600

    def test_oidc_section_names_the_secret_key_but_never_its_value(self, env) -> None:
        env.web(
            "POST",
            "/admin/config/oidc",
            data={"use_pkce": "false", "client_secret": _SENTINEL},
        )
        row = _config_row(env, "web", "oidc")
        assert sorted(row.details["changed_keys"]) == [
            "oidc_provider_config.client_secret",
            "oidc_provider_config.use_pkce",
        ]
        assert row.details["values"] == {"oidc_provider_config.use_pkce": [True, False]}
        _no_secret(env)
        assert env.config_service.get_config().oidc_provider_config.client_secret == (
            _SENTINEL
        )

    def test_rejected_oidc_change_publishes_nothing_and_records_a_failure(
        self, env, monkeypatch
    ) -> None:
        from code_indexer.server.web import routes as web_routes

        seen = []

        def _failing_reload(candidate) -> None:
            seen.append(candidate.oidc_provider_config.use_pkce)
            raise RuntimeError("provider rejected the configuration")

        monkeypatch.setattr(web_routes, "_prepare_oidc_managers", _failing_reload)
        resp = env.web("POST", "/admin/config/oidc", data={"use_pkce": "false"})
        assert resp.status_code == 400
        assert "Changes not saved" in resp.text
        assert seen == [False]  # the reload ran against the CANDIDATE
        assert env.config_service.get_config().oidc_provider_config.use_pkce is True
        reloaded = type(env.config_service)(server_dir_path=str(env.server_dir))
        assert reloaded.load_config().oidc_provider_config.use_pkce is True
        row = env.only_row("config_changed")
        assert_attributed(row, source="web", outcome="failure")
        assert row.details == {
            "change_kind": "update",
            "attempted_keys": ["oidc_provider_config.use_pkce"],
        }

    def test_oidc_managers_stay_live_when_the_save_fails(
        self, env, monkeypatch
    ) -> None:
        from code_indexer.server.auth.oidc import routes as oidc_routes

        live_manager, live_state = object(), object()
        monkeypatch.setattr(oidc_routes, "oidc_manager", live_manager)
        monkeypatch.setattr(oidc_routes, "state_manager", live_state)

        def _failing_save(_config) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(env.config_service, "save_config", _failing_save)
        resp = env.web("POST", "/admin/config/oidc", data={"use_pkce": "false"})
        assert resp.status_code == 500
        assert oidc_routes.oidc_manager is live_manager
        assert oidc_routes.state_manager is live_state
        assert env.config_service.get_config().oidc_provider_config.use_pkce is True
        row = env.only_row("config_changed")
        assert_attributed(row, source="web", outcome="failure")

    def test_oidc_managers_are_replaced_after_a_successful_save(
        self, env, monkeypatch
    ) -> None:
        from code_indexer.server.auth.oidc import routes as oidc_routes

        monkeypatch.setattr(oidc_routes, "oidc_manager", object())
        monkeypatch.setattr(oidc_routes, "state_manager", object())
        resp = env.web("POST", "/admin/config/oidc", data={"use_pkce": "false"})
        assert resp.status_code == 200, resp.text
        # OIDC stays disabled, so the published config clears the managers.
        assert oidc_routes.oidc_manager is None
        assert oidc_routes.state_manager is None
        assert env.only_row("config_changed").outcome == "success"

    def test_indexing_section_is_one_audited_change(self, env) -> None:
        env.web(
            "POST",
            "/admin/config/indexing",
            data={"voyage_ai_parallel_requests": "12"},
        )
        row = _config_row(env, "web", "indexing")
        assert row.details["changed_keys"] == [
            "indexing_config.voyage_ai_parallel_requests"
        ]
        assert row.details["values"] == {
            "indexing_config.voyage_ai_parallel_requests": [8, 12]
        }
        reloaded = type(env.config_service)(server_dir_path=str(env.server_dir))
        assert reloaded.load_config().indexing_config.voyage_ai_parallel_requests == 12

    def test_reset_to_defaults(self, env) -> None:
        env.config_service.update_setting(
            "data_retention", "audit_logs_retention_hours", 100
        )
        env.web("POST", "/admin/config/reset", data={})
        row = _config_row(env, "web", "*")
        assert row.details["change_kind"] == "reset_to_defaults"
        changed = row.details["changed_keys"]
        assert "data_retention_config.audit_logs_retention_hours" in changed
        live = env.config_service.get_config()
        assert live.data_retention_config.audit_logs_retention_hours == 2160

    def test_langfuse_pull_is_one_change_without_secrets(self, env) -> None:
        projects = [{"public_key": "pk-example", "secret_key": _SENTINEL}]
        env.web(
            "POST",
            "/admin/config/langfuse_pull",
            data={
                "pull_enabled": "true",
                "pull_sync_interval_seconds": "600",
                "pull_projects": json.dumps(projects),
            },
        )
        row = _config_row(env, "web", "langfuse")
        assert "langfuse_config.pull_projects" in row.details["changed_keys"]
        assert row.details["values"]["langfuse_config.pull_enabled"] == [False, True]
        _no_secret(env)

    def test_cidx_meta_backup_is_one_change(self, env) -> None:
        env.web(
            "POST",
            "/admin/config/cidx_meta_backup",
            data={"enabled": "true", "remote_url": ""},
        )
        row = _config_row(env, "web", "cidx_meta_backup")
        assert row.details["changed_keys"] == ["cidx_meta_backup_config.enabled"]

    def test_self_monitoring(self, env) -> None:
        env.web(
            "POST",
            "/admin/self-monitoring",
            data={"enabled": "on", "cadence_minutes": "30", "model": "sonnet"},
        )
        row = _config_row(env, "web", "self_monitoring")
        assert row.details["values"]["self_monitoring_config.cadence_minutes"] == [
            60,
            30,
        ]
        assert env.config_service.get_config().self_monitoring_config.enabled is True


class TestGlobalConfig:
    def test_rest(self, env) -> None:
        resp = env.rest("PUT", "/global/config", json={"refresh_interval": 120})
        assert resp.status_code == 200, resp.text
        row = _config_row(env, "rest", "golden_repos")
        assert row.details["values"] == {
            "golden_repos_config.refresh_interval_seconds": [3600, 120]
        }

    def test_mcp(self, env) -> None:
        result = env.mcp("set_global_config", {"refresh_interval": 120})
        assert result["success"] is True, result
        _config_row(env, "mcp", "golden_repos")

    def test_rejected_interval_records_a_failure_and_publishes_nothing(
        self, env
    ) -> None:
        result = env.mcp("set_global_config", {"refresh_interval": 5})
        assert result["success"] is False
        row = env.only_row("config_changed")
        assert_attributed(row, source="mcp", outcome="failure")
        live = env.config_service.get_config()
        assert live.golden_repos_config.refresh_interval_seconds == 3600


class TestServerSecrets:
    def test_llm_creds_save_config(self, env) -> None:
        resp = env.rest(
            "POST",
            "/api/llm-creds/save-config",
            json={
                "claude_auth_mode": "api_key",
                "llm_creds_provider_url": "",
                "llm_creds_provider_api_key": _SENTINEL,
                "llm_creds_provider_consumer_id": "",
            },
        )
        assert resp.status_code == 200, resp.text
        row = _config_row(env, "rest", "claude_integration")
        changed = row.details["changed_keys"]
        assert "claude_integration_config.llm_creds_provider_api_key" in changed
        _no_secret(env)

    def test_provider_key_set_and_cleared(self, env) -> None:
        key = "pa-" + _SENTINEL + "-0123456789"
        resp = env.rest("POST", "/api/api-keys/voyageai", json={"api_key": key})
        assert resp.status_code == 200, resp.text
        resp = env.rest("DELETE", "/api/api-keys/voyageai")
        assert resp.status_code == 200, resp.text
        for action_type in ("provider_api_key_set", "provider_api_key_cleared"):
            row = env.only_row(action_type)
            assert_attributed(row, source="rest")
            assert (row.target_type, row.target_id) == ("config", "voyageai")
            assert row.details == {"provider": "voyageai"}
        assert env.rows("config_changed") == []
        _no_secret(env)

    def test_startup_key_seeding_writes_no_row(self, env) -> None:
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        env.rest(
            "POST",
            "/api/api-keys/voyageai",
            json={"api_key": "pa-example-0123456789-abcdef"},
        )
        before = len(env.store.rows(""))
        seed_api_keys_on_startup(env.config_service)
        assert len(env.store.rows("")) == before

    def test_git_settings_records_key_names_only(self, env) -> None:
        resp = env.rest(
            "PUT",
            "/api/settings/git",
            json={"default_committer_email": "person@example.com"},
        )
        assert resp.status_code == 200, resp.text
        row = env.only_row("git_settings_changed")
        assert_attributed(row, source="rest")
        assert row.target_id == "git_service"
        assert row.details == {"changed_keys": ["git_service.default_committer_email"]}
        assert "person@example.com" not in env.store.all_raw_text()

    def test_ci_token_set_and_deleted(self, env) -> None:
        token = "ghp_" + "A" * 36
        env.web(
            "POST",
            "/admin/config/api-keys/github",
            data={"token": token, "api_url": ""},
        )
        env.web(
            "DELETE", "/admin/config/api-keys/github", headers={"X-CSRF-Token": "x"}
        )
        for action_type in ("ci_token_set", "ci_token_deleted"):
            row = env.only_row(action_type)
            assert_attributed(row, source="web")
            assert (row.target_id, row.details) == ("github", {"platform": "github"})
        assert token not in env.store.all_raw_text()
