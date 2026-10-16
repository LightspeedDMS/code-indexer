# ruff: noqa: F811
# (the shared fixtures imported below are pytest fixtures used as test
# parameters, which ruff reports as redefinitions)
"""The user wiki and the activation listings judge each of the caller's
activations by its source golden repositories -- the rule the MCP
dispatcher and the activated-repo REST routes apply.

- /wiki/u/{owner}/{alias}/...: the owner's activation must be granted;
  another user's wiki is readable by an admin (admins group) only.
- GET /api/repos/sync-status (bulk): a non-granted activation is omitted.
- GET /api/repos and GET /api/repos/status: a non-granted activation stays
  listed (so its owner can find and deactivate it) as its alias plus
  ``access_revoked`` only, never its metadata.

Driven through the real front door of a real app over the real services
(see activated_repo_access_env). Aliases and usernames are neutral
placeholders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from pydantic import BaseModel, Field

from code_indexer.server.wiki.routes import _reset_wiki_cache
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    GRANTED_REPO,
    GRANTED_REPO_2,
    OWN_ACTIVATION,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
)
from tests.unit.server.routers.activated_repo_access_env import (  # noqa: F401
    PEER,
    SWEEPER,
    Probe,
    assert_refused_as_unknown,
    call,
    client,
    env,
    server_db_template,
)

# A second activation of USER's, of a repository that stays granted.
SECOND_ACTIVATION = "my-second-repo"
ASSET = "logo.png"

# The shared module-scoped real app (activated_repo_access_env, ~5.4 s alone)
# is paid by whichever test runs first, slower under parallel gate load.
pytestmark = pytest.mark.timeout(45)

WIKI: Dict[str, Probe] = {
    "page": ("GET", f"/wiki/u/{USER}/{{a}}/", {}, 200),
    "article": ("GET", f"/wiki/u/{USER}/{{a}}/README", {}, 200),
    "asset": ("GET", f"/wiki/u/{USER}/{{a}}/_assets/{ASSET}", {}, 200),
    "search": ("GET", f"/wiki/u/{USER}/{{a}}/_search", {"params": {"q": "x"}}, 200),
}
BULK_SYNC_STATUS: Probe = ("GET", "/api/repos/sync-status", {}, 200)
LISTING: Probe = ("GET", "/api/repos", {}, 200)
STATUS_SUMMARY: Probe = ("GET", "/api/repos/status", {}, 200)


@pytest.fixture(autouse=True)
def fresh_wiki_cache() -> Iterator[None]:
    """The wiki cache is a module singleton bound to the first app's DB."""
    _reset_wiki_cache()
    yield
    _reset_wiki_cache()


def _user_wiki(env: QueryAccessEnv, golden: str = GRANTED_REPO) -> None:
    """USER activates *golden* as OWN_ACTIVATION with its wiki enabled."""
    env.activate_for(USER, golden, OWN_ACTIVATION)
    manager = env.activated_repo_manager
    manager.set_wiki_enabled(USER, OWN_ACTIVATION, True)
    clone = Path(manager.get_activated_repo_path(USER, OWN_ACTIVATION))
    (clone / ASSET).write_bytes(b"\x89PNG\r\n\x1a\n")


class TestUserWiki:
    @pytest.mark.parametrize("route", sorted(WIKI))
    def test_granted_owner_reads_their_wiki(self, client, env, route):
        _user_wiki(env)

        response = call(client, USER, WIKI[route], OWN_ACTIVATION)

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("route", sorted(WIKI))
    def test_revoked_owner_is_refused_as_unknown(self, client, env, route):
        _user_wiki(env)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        assert call(client, USER, WIKI[route], OWN_ACTIVATION).status_code == 404
        assert_refused_as_unknown(client, USER, WIKI[route], OWN_ACTIVATION)

    @pytest.mark.parametrize("route", sorted(WIKI))
    def test_activation_of_ungranted_repo_is_refused(self, client, env, route):
        _user_wiki(env, golden=UNGRANTED_REPO)

        assert call(client, USER, WIKI[route], OWN_ACTIVATION).status_code == 404

    @pytest.mark.parametrize("route", sorted(WIKI))
    def test_admins_group_member_reads_another_users_wiki(self, client, env, route):
        _user_wiki(env)

        response = call(client, ADMIN, WIKI[route], OWN_ACTIVATION)

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("viewer", [SWEEPER, PEER])
    @pytest.mark.parametrize("route", sorted(WIKI))
    def test_non_admin_cannot_read_another_users_wiki(self, client, env, route, viewer):
        """SWEEPER holds the ADMIN role but is outside the admins group."""
        _user_wiki(env)

        assert call(client, viewer, WIKI[route], OWN_ACTIVATION).status_code == 404


def _two_activations_one_revoked(env: QueryAccessEnv) -> None:
    env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
    env.activate_for(USER, GRANTED_REPO_2, SECOND_ACTIVATION)
    env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)


def _count_activation_lookups(env: QueryAccessEnv, monkeypatch: Any) -> List[str]:
    real_lookup = env.access_service.caller_activation_sources
    calls: List[str] = []

    def _counting_lookup(user_id: str) -> Any:
        calls.append(user_id)
        return real_lookup(user_id)

    monkeypatch.setattr(
        env.access_service, "caller_activation_sources", _counting_lookup
    )
    return calls


class TestBulkSyncStatus:
    def test_revoked_activation_is_omitted_and_granted_kept(
        self, client, env, monkeypatch
    ):
        _two_activations_one_revoked(env)
        lookups = _count_activation_lookups(env, monkeypatch)

        response = call(client, USER, BULK_SYNC_STATUS, "")

        assert response.status_code == 200, response.text
        assert set(response.json()) == {SECOND_ACTIVATION}
        assert lookups == [USER], "activations are read once per request"

    def test_admin_sees_every_activation(self, client, env):
        env.activate_for(ADMIN, UNGRANTED_REPO, OWN_ACTIVATION)

        response = call(client, ADMIN, BULK_SYNC_STATUS, "")

        assert set(response.json()) == {OWN_ACTIVATION}


def _revoked_entry() -> Dict[str, Any]:
    """The string fields a released CLI client requires are "" (never null)."""
    return {
        "user_alias": OWN_ACTIVATION,
        "access_revoked": True,
        "golden_repo_alias": None,
        "current_branch": "",
        "activated_at": "",
        "last_accessed": "",
        "deactivation_job": None,
        "sync_status": None,
    }


class _ReleasedActivatedRepository(BaseModel):
    """api_clients/repos_client.ActivatedRepository as released before
    access_revoked existed (verbatim)."""

    alias: str = Field(..., description="Repository alias")
    current_branch: str = Field(..., description="Current active branch")
    sync_status: str = Field(..., description="Synchronization status")
    last_sync: str = Field(..., description="Last synchronization timestamp")
    activation_date: str = Field(..., description="Repository activation timestamp")
    conflict_details: Optional[str] = Field(None, description="Conflict details if any")


def _released_client_mapping(
    repositories_data: List[Dict[str, Any]],
) -> List[_ReleasedActivatedRepository]:
    """The released ReposAPIClient.list_activated_repositories mapping
    (verbatim)."""
    mapped_repositories = []
    for repo_data in repositories_data:
        mapped_repo = _ReleasedActivatedRepository(
            alias=repo_data["user_alias"],
            current_branch=repo_data["current_branch"],
            sync_status=repo_data.get("sync_status") or "unknown",
            last_sync=repo_data.get("last_accessed", ""),
            activation_date=repo_data.get("activated_at", ""),
            conflict_details=None,
        )
        mapped_repositories.append(mapped_repo)
    return mapped_repositories


class TestListing:
    def test_listing_with_revoked_entry_parses_with_released_client(self, client, env):
        """A CLI client released before access_revoked existed still lists
        every activation once one of them is revoked."""
        _two_activations_one_revoked(env)

        response = call(
            client, USER, LISTING, "", params={"include_sync_status": "true"}
        )

        assert response.status_code == 200, response.text
        mapped = _released_client_mapping(response.json()["repositories"])
        assert sorted(r.alias for r in mapped) == sorted(
            [OWN_ACTIVATION, SECOND_ACTIVATION]
        )

    @pytest.mark.parametrize("include_sync_status", ["false", "true"])
    def test_revoked_activation_is_listed_as_alias_only(
        self, client, env, monkeypatch, include_sync_status
    ):
        _two_activations_one_revoked(env)
        lookups = _count_activation_lookups(env, monkeypatch)

        response = call(
            client,
            USER,
            LISTING,
            "",
            params={"include_sync_status": include_sync_status},
        )

        assert response.status_code == 200, response.text
        entries = {r["user_alias"]: r for r in response.json()["repositories"]}
        assert entries[OWN_ACTIVATION] == _revoked_entry()
        granted = entries[SECOND_ACTIVATION]
        assert granted["access_revoked"] is False
        assert granted["golden_repo_alias"] == GRANTED_REPO_2
        assert granted["activated_at"]
        assert lookups == [USER], "activations are read once per request"

    def test_admin_listing_is_never_reduced(self, client, env):
        env.activate_for(ADMIN, UNGRANTED_REPO, OWN_ACTIVATION)

        response = call(client, ADMIN, LISTING, "")

        (entry,) = response.json()["repositories"]
        assert entry["access_revoked"] is False
        assert entry["golden_repo_alias"] == UNGRANTED_REPO


class TestStatusSummary:
    def test_revoked_activation_is_listed_as_alias_only(self, client, env, monkeypatch):
        _two_activations_one_revoked(env)
        lookups = _count_activation_lookups(env, monkeypatch)

        response = call(client, USER, STATUS_SUMMARY, "")

        assert response.status_code == 200, response.text
        activated = response.json()["activated_repositories"]
        assert activated["revoked_activations"] == [
            {"user_alias": OWN_ACTIVATION, "access_revoked": True}
        ]
        assert activated["total_count"] == 2
        recent = [a["alias"] for a in activated["recent_activations"]]
        assert recent == [SECOND_ACTIVATION]
        syncs = response.json()["recent_activity"]["recent_syncs"]
        assert OWN_ACTIVATION not in [s["alias"] for s in syncs]
        assert lookups == [USER], "activations are read once per request"
