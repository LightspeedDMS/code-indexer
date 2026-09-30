"""Front-door parity for golden-repository and provider-index audit rows.

Every capability writes exactly one row per request on every door where it
exists (REST, MCP, Web), naming the authenticated caller -- an admin whose
name is NOT ``admin`` -- and the door as the source.  Job-based rows carry
the submitted job id.  See ``_audit_front_doors`` for the harness.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import CAPTURE_LOGGER, capture_errors
from _audit_front_doors import (
    ACTING_ADMIN,
    DoorsEnv,
    assert_attributed,
    front_door_env,
)
from _audit_repos_support import EXAMPLE_ALIAS

_NEW_ALIAS = "example-new"
_PROVIDER = "voyage-ai"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    yield from front_door_env(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    """No door may emit on the event loop, drop a row or build a bad event."""
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


def _job_of(env: DoorsEnv, action_type: str) -> str:
    row = env.only_row(action_type)
    job_id = row.details["job_id"]
    assert job_id in {s["job_id"] for s in env.jobs.submissions}
    return str(job_id)


class TestRemovalActor:
    """Removal names the real caller on every door (never ``admin``)."""

    def test_rest(self, env) -> None:
        resp = env.rest("DELETE", f"/api/admin/golden-repos/{EXAMPLE_ALIAS}")
        assert resp.status_code == 204, resp.text
        row = env.only_row("golden_repo_removed")
        assert_attributed(row, source="rest")
        assert row.target_id == EXAMPLE_ALIAS
        assert env.jobs.submissions[0]["submitter_username"] == ACTING_ADMIN
        _job_of(env, "golden_repo_removed")

    def test_mcp(self, env) -> None:
        result = env.mcp("remove_golden_repo", {"alias": EXAMPLE_ALIAS})
        assert result["success"] is True, result
        assert_attributed(env.only_row("golden_repo_removed"), source="mcp")

    def test_web(self, env) -> None:
        env.web("POST", f"/admin/golden-repos/{EXAMPLE_ALIAS}/delete", data={})
        assert_attributed(env.only_row("golden_repo_removed"), source="web")


class TestAdd:
    def test_rest(self, env) -> None:
        body = {"repo_url": env.git_url, "alias": _NEW_ALIAS}
        resp = env.rest("POST", "/api/admin/golden-repos", json=body)
        assert resp.status_code == 202, resp.text
        row = env.only_row("golden_repo_added")
        assert_attributed(row, source="rest")
        assert row.target_id == _NEW_ALIAS

    def test_mcp(self, env) -> None:
        result = env.mcp("add_golden_repo", {"url": env.git_url, "alias": _NEW_ALIAS})
        assert result["success"] is True, result
        assert_attributed(env.only_row("golden_repo_added"), source="mcp")

    def test_web_single(self, env) -> None:
        env.web(
            "POST",
            "/admin/golden-repos/add",
            data={"alias": _NEW_ALIAS, "repo_url": env.git_url},
        )
        assert_attributed(env.only_row("golden_repo_added"), source="web")

    def test_web_batch_records_one_row_per_repo(self, env) -> None:
        repos = [
            {"clone_url": "https://git.example.com/org/a.git", "alias": "example-a"},
            {"clone_url": "https://git.example.com/org/b.git", "alias": "example-b"},
        ]
        resp = env.web(
            "POST",
            "/admin/golden-repos/batch-create",
            data={"repos": json.dumps(repos)},
        )
        assert resp.status_code == 200, resp.text
        rows = env.rows("golden_repo_added")
        assert sorted(r.target_id for r in rows) == ["example-a", "example-b"]
        for row in rows:
            assert_attributed(row, source="web")
            assert row.details["repo_host"] == "git.example.com"


class TestRefresh:
    def test_rest(self, env) -> None:
        resp = env.rest("POST", f"/api/admin/golden-repos/{EXAMPLE_ALIAS}/refresh")
        assert resp.status_code == 202, resp.text
        row = env.only_row("golden_repo_refreshed")
        assert_attributed(row, source="rest")
        assert row.details["force_reset"] is False
        _job_of(env, "golden_repo_refreshed")

    def test_mcp(self, env) -> None:
        result = env.mcp("refresh_golden_repo", {"alias": EXAMPLE_ALIAS})
        assert result["success"] is True, result
        assert_attributed(env.only_row("golden_repo_refreshed"), source="mcp")

    def test_web_refresh(self, env) -> None:
        env.web("POST", f"/admin/golden-repos/{EXAMPLE_ALIAS}/refresh", data={})
        row = env.only_row("golden_repo_refreshed")
        assert_attributed(row, source="web")
        assert row.details["force_reset"] is False

    def test_web_force_resync(self, env) -> None:
        env.web("POST", f"/admin/golden-repos/{EXAMPLE_ALIAS}/force-resync", data={})
        row = env.only_row("golden_repo_refreshed")
        assert_attributed(row, source="web")
        assert row.details["force_reset"] is True


class TestIndexAndBranch:
    def test_rest_add_index(self, env) -> None:
        resp = env.rest(
            "POST",
            f"/api/admin/golden-repos/{EXAMPLE_ALIAS}/indexes",
            json={"index_type": "fts"},
        )
        assert resp.status_code == 202, resp.text
        row = env.only_row("golden_repo_index_added")
        assert_attributed(row, source="rest")
        assert row.details["index_types"] == ["fts"]

    def test_rest_add_provider_scoped_semantic_index(self, env) -> None:
        resp = env.rest(
            "POST",
            f"/api/admin/golden-repos/{EXAMPLE_ALIAS}/indexes",
            json={"index_types": ["semantic"], "providers": [_PROVIDER]},
        )
        assert resp.status_code == 202, resp.text
        row = env.only_row("golden_repo_index_added")
        assert_attributed(row, source="rest")
        assert row.details["index_types"] == ["semantic"]

    def test_mcp_add_index(self, env) -> None:
        result = env.mcp(
            "add_golden_repo_index", {"alias": EXAMPLE_ALIAS, "index_type": "fts"}
        )
        assert result["success"] is True, result
        assert_attributed(env.only_row("golden_repo_index_added"), source="mcp")

    def test_mcp_change_branch(self, env) -> None:
        result = env.mcp(
            "change_golden_repo_branch", {"alias": EXAMPLE_ALIAS, "branch": "dev"}
        )
        assert result["success"] is True, result
        row = env.only_row("golden_repo_branch_changed")
        assert_attributed(row, source="mcp")
        assert row.details["new_branch"] == "dev"

    def test_web_change_branch_runs_off_the_event_loop(self, env) -> None:
        resp = env.web(
            "POST",
            f"/admin/golden-repos/{EXAMPLE_ALIAS}/change-branch",
            json={"branch": "dev"},
        )
        assert resp.status_code == 202, resp.text
        assert_attributed(env.only_row("golden_repo_branch_changed"), source="web")


class TestProviderIndexes:
    def test_rest_add_recreate_remove(self, env) -> None:
        body = {"provider": _PROVIDER, "alias": EXAMPLE_ALIAS}
        for path, action_type in (
            ("add", "provider_index_added"),
            ("recreate", "provider_index_recreated"),
            ("remove", "provider_index_removed"),
        ):
            resp = env.rest("POST", f"/api/admin/provider-indexes/{path}", json=body)
            assert resp.status_code in (200, 202), resp.text
            row = env.only_row(action_type)
            assert row.source == "rest" and row.actor == ACTING_ADMIN
            assert row.target_id == EXAMPLE_ALIAS
            assert row.details["provider"] == _PROVIDER
        assert env.only_row("provider_index_added").outcome == "success"

    def test_mcp_add_recreate_remove(self, env) -> None:
        for action, action_type in (
            ("add", "provider_index_added"),
            ("recreate", "provider_index_recreated"),
            ("remove", "provider_index_removed"),
        ):
            env.mcp(
                "manage_provider_indexes",
                {
                    "action": action,
                    "provider": _PROVIDER,
                    "repository_alias": EXAMPLE_ALIAS,
                },
            )
            row = env.only_row(action_type)
            assert row.source == "mcp" and row.actor == ACTING_ADMIN
            assert row.target_id == EXAMPLE_ALIAS
        assert env.only_row("provider_index_added").outcome == "success"

    def test_rest_bulk_add_writes_one_row_per_request(self, env) -> None:
        resp = env.rest(
            "POST", "/api/admin/provider-indexes/bulk-add", json={"provider": _PROVIDER}
        )
        assert resp.status_code == 202, resp.text
        row = env.only_row("provider_index_bulk_added")
        assert_attributed(row, source="rest")
        assert row.target_id == _PROVIDER
        assert row.details["aliases"] == [f"{EXAMPLE_ALIAS}-global"]
        assert len(row.details["job_ids"]) == 1

    def test_mcp_bulk_add_writes_one_row_per_request(self, env) -> None:
        result = env.mcp("bulk_add_provider_index", {"provider": _PROVIDER})
        assert result["success"] is True, result
        row = env.only_row("provider_index_bulk_added")
        assert_attributed(row, source="mcp")
        assert row.details["aliases"] == [f"{EXAMPLE_ALIAS}-global"]

    def test_unconfigured_provider_is_one_failure_row_on_each_door(self, env) -> None:
        body = {"provider": "cohere", "alias": EXAMPLE_ALIAS}
        assert (
            env.rest("POST", "/api/admin/provider-indexes/add", json=body).status_code
            == 400
        )
        env.mcp(
            "manage_provider_indexes",
            {"action": "add", "provider": "cohere", "repository_alias": EXAMPLE_ALIAS},
        )
        rows = env.rows("provider_index_added")
        assert [(r.source, r.outcome, r.target_id) for r in rows] == [
            ("rest", "failure", "unresolved"),
            ("mcp", "failure", "unresolved"),
        ]
