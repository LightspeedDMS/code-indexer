"""Phase 3 -- Story #1135 (Epic #1121): Git-Write Round-Trip + Global-Alias
Bare-Name Promotion (#1039).

Both acceptance criteria are exercised entirely through the REAL MCP / REST
front door against an in-process CIDX server (FastAPI TestClient).  No mocks:
real golden-repo registration, real VoyageAI indexing, real git subprocesses,
real file I/O.

AC1 -- git-write round-trip on an ACTIVATED repo
------------------------------------------------
As the admin (who has markupsafe activated via ``seeded_indexed_client``), drive
the full write cycle through MCP tools and verify the written content survives a
commit + branch + merge:

    create_file -> git_stage -> git_commit -> git_branch_create -> git_merge
                -> git_file_at_revision (exact content verification)

Ordinary activated repos are writable WITHOUT ``enter_write_mode`` -- the
activated-repos path segment alone grants write (file_crud_service.py:133-173,
_is_writable_repo in files.py).  No enter_write_mode call is made.

Harness note (test-only, NOT a product change): the MCP git-write resolver
``_resolve_git_repo_path`` constructs a fresh ``ActivatedRepoManager()`` with no
data_dir, which defaults to ``~/.cidx-server/data`` and does NOT honour
``CIDX_SERVER_DATA_DIR``.  In production the server runs against its real data
dir so the resolver and the activated workspace coincide; in the in-process
TestClient harness the workspace lives under the session temp dir.  The
``_arm_data_dir_aligned`` fixture monkeypatches the ARM default to honour
``CIDX_SERVER_DATA_DIR`` so the REAL resolver finds the REAL activated workspace
the REAL registration created.  Every git/file operation still runs for real --
this only aligns a directory default, it does not mock any behaviour.

AC2 -- read-vs-write global-alias promotion asymmetry (#1039)
-------------------------------------------------------------
markupsafe is globally active as ``markupsafe-global`` (golden-repo registration
auto-activates globally -- golden_repo_manager.py:422-446).  A power_user is put
in a group that has markupsafe access but does NOT activate the repo for that
user.  The MCP protocol access guard (protocol.py:283-468) passes because the
group grants access; the user simply lacks an activated workspace.

  * READ handlers PROMOTE the bare alias: ``search_code`` and
    ``get_file_content`` with ``repository_alias="markupsafe"`` return the
    ``markupsafe-global`` repo's results (via try_global_fallback).
  * WRITE handlers stay STRICT: ``git_commit`` (a Section-B write handler) with
    the bare alias is NOT promoted -- it fails with "not found / no .git"
    against the user's (non-existent) activated workspace and never silently
    acts on ``markupsafe-global``.

The write-strict result is the MUTATION assertion; the read-promote result is
the CONTROL.  git_commit is used as the write probe (rather than create_file)
because its strict-failure path returns the error WITHOUT emitting an
ERROR/WARNING log entry, keeping the Phase 3 log-audit gate clean.

Credentials from env: E2E_ADMIN_USER, E2E_ADMIN_PASS (set by e2e-automation.sh).
Requires VOYAGE_API_KEY / E2E_VOYAGE_API_KEY (indexed global repo for the read
promotion).  Module loud-skips when the embedding key is absent.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterator, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import _auth_headers, require_voyage_key
from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_GLOBAL_SUFFIX: str = "-global"
# A file already present in the markupsafe seed repo -- used to prove a
# promoted READ returns the global repo's real content.
_KNOWN_REPO_FILE: str = "README.rst"
# Substring proving git_commit reached the strict resolver and was NOT promoted
# to the global repo (the activated workspace does not exist for this user).
_WRITE_STRICT_MARKERS: tuple[str, ...] = (
    "not found",
    "does not have a .git",
    "not an activated workspace",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_and_error(resp: Any) -> Tuple[dict, Optional[dict]]:
    """Return (parsed-tool-result, jsonrpc-error) for an MCP tools/call response."""
    body = resp.json()
    return parse_mcp_result(body), body.get("error")


def _ok_tool(resp: Any, label: str) -> dict:
    """Assert a successful MCP tool call and return its parsed result dict."""
    parsed, err = _result_and_error(resp)
    assert resp.status_code == 200, (
        f"{label}: HTTP {resp.status_code} -- {resp.text[:300]}"
    )
    assert err is None, f"{label}: unexpected JSON-RPC error: {err}"
    assert parsed.get("success") is True, f"{label}: tool reported failure: {parsed}"
    return parsed


def _login(client: TestClient, username: str, password: str) -> str:
    """POST /auth/login and return the access_token."""
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, (
        f"login {username!r}: {resp.status_code} -- {resp.text[:300]}"
    )
    token = resp.json().get("access_token")
    assert token, f"login {username!r}: missing access_token: {resp.json()}"
    return str(token)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _arm_data_dir_aligned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Align ActivatedRepoManager()'s default data_dir to CIDX_SERVER_DATA_DIR.

    The MCP git-write resolver builds a fresh ``ActivatedRepoManager()`` with no
    data_dir, which defaults to ``~/.cidx-server/data``.  In the in-process
    TestClient harness the activated workspace lives under the session temp dir
    pointed to by ``CIDX_SERVER_DATA_DIR``.  This monkeypatch makes the no-arg
    constructor honour that env var so the REAL resolver finds the REAL
    workspace.  It changes only a directory default -- no behaviour is mocked.
    """
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    original_init = ActivatedRepoManager.__init__

    def aligned_init(
        self: Any, data_dir: Optional[str] = None, *args: Any, **kwargs: Any
    ) -> None:
        if data_dir is None:
            env_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
            if env_dir:
                data_dir = os.path.join(env_dir, "data")
        original_init(self, data_dir, *args, **kwargs)

    monkeypatch.setattr(ActivatedRepoManager, "__init__", aligned_init)


@pytest.fixture
def group_member_no_activation(
    seeded_indexed_client: tuple[TestClient, str],
    auth_headers: dict,
) -> Iterator[Tuple[str, dict]]:
    """Create a power_user in a group that has markupsafe access but is NOT activated.

    Yields ``(username, member_auth_headers)``.  The member can pass the MCP
    access guard (group grants markupsafe) and has repository:write permission
    (power_user role) -- yet has no activated workspace, so write handlers fail
    strict while read handlers promote to the global form.

    Teardown removes the member from the group, deletes the user, and deletes
    the group (front-door, in dependency order so delete_group does not error
    on an active member).
    """
    client, alias = seeded_indexed_client

    group_name = f"grp_1135_{uuid.uuid4().hex[:8]}"
    username = f"member_1135_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex + "Aa1!"

    group_id: Optional[str] = None
    try:
        grp = _ok_tool(
            call_mcp_tool(client, "create_group", {"name": group_name}, auth_headers),
            "create_group",
        )
        group_id = str(grp["group_id"])

        _ok_tool(
            call_mcp_tool(
                client,
                "manage_group_repos",
                {"action": "add", "group_id": group_id, "repos": [alias]},
                auth_headers,
            ),
            "manage_group_repos add",
        )
        _ok_tool(
            call_mcp_tool(
                client,
                "create_user",
                {"username": username, "password": password, "role": "power_user"},
                auth_headers,
            ),
            "create_user",
        )
        _ok_tool(
            call_mcp_tool(
                client,
                "manage_group_members",
                {"action": "add", "group_id": group_id, "user_id": username},
                auth_headers,
            ),
            "manage_group_members add",
        )

        member_token = _login(client, username, password)
        yield username, _auth_headers(member_token)
    finally:
        if group_id is not None:
            call_mcp_tool(
                client,
                "manage_group_members",
                {"action": "remove", "group_id": group_id, "user_id": username},
                auth_headers,
            )
        client.delete(f"/api/admin/users/{username}", headers=auth_headers)
        if group_id is not None:
            call_mcp_tool(client, "delete_group", {"group_id": group_id}, auth_headers)


# ---------------------------------------------------------------------------
# AC1 -- git-write round-trip on an activated repo
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_arm_data_dir_aligned")
class TestAC1GitWriteRoundTrip:
    """AC1: create_file -> commit -> branch -> merge -> verify content via MCP."""

    def test_round_trip_preserves_written_content(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """A file written + committed + branched + merged reads back byte-exact.

        Drives the full git-write cycle through MCP tools against the admin's
        activated markupsafe workspace, then asserts git_file_at_revision at
        HEAD returns the exact bytes that create_file wrote.
        """
        require_voyage_key()
        client, alias = seeded_indexed_client

        # Deterministic, collision-free new path + content + branch.
        run_id = uuid.uuid4().hex[:12]
        file_path = f"e2e_1135_{run_id}.txt"
        content = f"story-1135-roundtrip-{run_id}\n"
        branch_name = f"e2e-1135-{run_id}"

        # 1. create_file (writes to the activated workspace; no enter_write_mode).
        _ok_tool(
            call_mcp_tool(
                client,
                "create_file",
                {"repository_alias": alias, "file_path": file_path, "content": content},
                auth_headers,
            ),
            "create_file",
        )

        # 2. git_stage the new file (create_file does not auto-stage).
        _ok_tool(
            call_mcp_tool(
                client,
                "git_stage",
                {"repository_alias": alias, "file_paths": [file_path]},
                auth_headers,
            ),
            "git_stage",
        )

        # 3. git_commit (co_author_email is mandatory).
        commit = _ok_tool(
            call_mcp_tool(
                client,
                "git_commit",
                {
                    "repository_alias": alias,
                    "message": f"Story #1135 round-trip {run_id}",
                    "co_author_email": "e2e-1135@example.com",
                },
                auth_headers,
            ),
            "git_commit",
        )
        commit_hash = commit.get("commit_hash")
        assert commit_hash, f"git_commit returned no commit_hash: {commit}"

        # 4. git_branch_create from the committed state.
        _ok_tool(
            call_mcp_tool(
                client,
                "git_branch_create",
                {"repository_alias": alias, "branch_name": branch_name},
                auth_headers,
            ),
            "git_branch_create",
        )

        # 5. git_merge the new branch back into the current branch (already an
        #    ancestor -> fast-forward / up-to-date; exercises the merge handler
        #    on the activated workspace).
        _ok_tool(
            call_mcp_tool(
                client,
                "git_merge",
                {"repository_alias": alias, "source_branch": branch_name},
                auth_headers,
            ),
            "git_merge",
        )

        # 6. git_file_at_revision verifies the EXACT written content at HEAD
        #    (git_read.py:592-601 returns result.content).
        revision = commit_hash
        at_rev = _ok_tool(
            call_mcp_tool(
                client,
                "git_file_at_revision",
                {"repository_alias": alias, "path": file_path, "revision": revision},
                auth_headers,
            ),
            "git_file_at_revision",
        )
        assert at_rev.get("content") == content, (
            "AC1 FAILED: git_file_at_revision did not return the written content. "
            f"wrote {content!r}, read back {at_rev.get('content')!r} "
            f"at revision {revision!r}."
        )


# ---------------------------------------------------------------------------
# AC2 -- read-vs-write global-alias promotion asymmetry
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded_indexed_client")
class TestAC2GlobalAliasPromotionAsymmetry:
    """AC2: bare alias promotes for READ handlers, stays strict for WRITE handlers."""

    def test_read_handlers_promote_bare_alias_to_global(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        group_member_no_activation: Tuple[str, dict],
    ) -> None:
        """CONTROL: search_code + get_file_content with the bare alias promote.

        A non-activating group member querying ``repository_alias="markupsafe"``
        receives results from the globally-activated ``markupsafe-global`` repo.
        """
        require_voyage_key()
        client, alias = seeded_indexed_client
        _username, member_headers = group_member_no_activation
        global_alias = f"{alias}{_GLOBAL_SUFFIX}"

        # search_code -> results carry the -global source repo (promotion fired).
        search = _ok_tool(
            call_mcp_tool(
                client,
                "search_code",
                {"query_text": "template", "repository_alias": alias, "limit": 3},
                member_headers,
            ),
            "search_code bare alias (read-promote)",
        )
        results_block = search.get("results", {})
        rows = (
            results_block.get("results", [])
            if isinstance(results_block, dict)
            else results_block
        )
        assert rows, f"search_code returned no rows: {search}"
        sources = {
            row.get("repository_alias") or row.get("source_repo") for row in rows
        }
        assert sources == {global_alias}, (
            "AC2 READ FAILED: bare-alias search_code did not promote to the "
            f"global repo. Expected all rows from {global_alias!r}, got {sources!r}."
        )

        # get_file_content -> returns the global repo's real file (promotion fired).
        file_result = _ok_tool(
            call_mcp_tool(
                client,
                "get_file_content",
                {"repository_alias": alias, "file_path": _KNOWN_REPO_FILE},
                member_headers,
            ),
            "get_file_content bare alias (read-promote)",
        )
        file_content_block = file_result.get("file_content")
        assert file_content_block, (
            "AC2 READ FAILED: bare-alias get_file_content returned no content; "
            f"promotion to {global_alias!r} did not occur: {file_result}"
        )

    def test_write_handler_does_not_promote_bare_alias(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        group_member_no_activation: Tuple[str, dict],
    ) -> None:
        """MUTATION: git_commit with the bare alias is NOT promoted to -global.

        The same non-activating group member that READS promote to
        markupsafe-global is REFUSED on a WRITE: git_commit against the bare
        alias resolves to the user's own (non-existent) activated workspace and
        fails strict -- it never silently commits to markupsafe-global.
        """
        require_voyage_key()
        client, alias = seeded_indexed_client
        _username, member_headers = group_member_no_activation
        global_alias = f"{alias}{_GLOBAL_SUFFIX}"

        parsed, err = _result_and_error(
            call_mcp_tool(
                client,
                "git_commit",
                {
                    "repository_alias": alias,
                    "message": "Story #1135 must-not-promote",
                    "co_author_email": "e2e-1135-strict@example.com",
                },
                member_headers,
            )
        )

        # The write must NOT succeed and must NOT have been promoted to -global.
        assert err is None or "global" not in str(err).lower(), (
            f"AC2 WRITE FAILED: git_commit error mentions the global repo "
            f"(possible promotion): {err}"
        )
        assert parsed.get("success") is not True, (
            "AC2 WRITE FAILED: git_commit on the bare alias SUCCEEDED for a "
            "non-activating user -- a write handler must never promote to "
            f"{global_alias!r}: {parsed}"
        )
        error_text = str(parsed.get("error", "")).lower()
        assert global_alias not in error_text, (
            "AC2 WRITE FAILED: git_commit acted on the global repo "
            f"{global_alias!r} instead of staying strict: {parsed}"
        )
        assert any(marker in error_text for marker in _WRITE_STRICT_MARKERS), (
            "AC2 WRITE FAILED: git_commit did not fail with the expected "
            f"strict (non-activated-workspace) error. markers={_WRITE_STRICT_MARKERS}, "
            f"got: {parsed}"
        )
