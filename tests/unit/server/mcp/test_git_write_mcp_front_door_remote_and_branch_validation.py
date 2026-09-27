"""MCP `git_push` handler argv-safety validation, front-door coverage.

`GitOperationsService.git_push_with_pat` validates `remote`/`branch`
before any subprocess (see git_operations_service.py and
test_git_operations_service_remote_and_branch_validation.py::TestPushWithPatArgumentValidation).
That closes the path through the upstream-tracking
`git branch --set-upstream-to=...` call, which takes `branch` as a bare
raw argv entry. This file covers two further requirements:

1. `mcp/handlers/git_write.py`'s `git_push` handler must ALSO validate
   `remote`/`branch` itself, before calling `_get_pat_credential_for_remote()`
   -- a second, independent argv-building call site
   (`git remote get-url <remote>`) that must never see an unvalidated value,
   even though (verified empirically) `git remote get-url` does not itself
   support `--receive-pack=`/`--upload-pack=`.
   `TestGitPushHandlerValidatesRemoteBeforeCredentialLookup` proves this is
   wired by asserting the rejection is the clean `GitArgumentValidationError`
   message from `validate_remote_name`, produced BEFORE
   `_get_pat_credential_for_remote` ever runs -- not the wrapped "Failed to
   get remote URL for ..." message that function produces on its own
   subprocess failure. That distinction is the discriminating signal.

2. Per this repo's "Definition of Done" (CLAUDE.md): a fix proven only at
   the service-method level is not proven REACHABLE from a real front
   door. `TestGitMcpFrontDoorRejectsInjection` drives option-shaped
   values for git_push, git_pull, git_fetch, and git_diff through the
   REAL MCP protocol dispatcher (`protocol.handle_tools_call` ->
   `_invoke_handler` -> the actual registered handler function), proving
   the fix is wired end-to-end through the front door a real MCP client
   uses, not merely reachable via a direct unit-level call to the service
   method.

No git subprocess is mocked anywhere in this file (Anti-Mock rule): all
tests use real throwaway git repositories with a real local-path `golden`
remote, matching how every activated repo is configured. Only the
alias-to-filesystem-path resolution layer (`_legacy._resolve_git_repo_path`,
`GitOperationsService.activated_repo_manager`) and the surrounding MCP
plumbing not under test (repository access filtering, langfuse tracing,
API metrics) are patched.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_SESSION_ID = "mcp-session-git-argv-validation-front-door"


def _git(args: list, cwd: Path) -> None:
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo_with_golden_remote(tmp_path: Path) -> Path:
    """Real repo with a commit, on branch 'main', with a local-path 'golden'
    remote -- matching how every activated repo is configured."""
    remote = tmp_path / "golden.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], cwd=remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    _git(["checkout", "-q", "-b", "main"], cwd=repo)
    (repo / "f.txt").write_text("hello\n")
    _git(["add", "f.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    _git(["remote", "add", "golden", str(remote)], cwd=repo)
    _git(["push", "-q", "--set-upstream", "golden", "main"], cwd=repo)

    return repo


def _rev_parse(repo_path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# git_write.py's git_push handler must validate before
# _get_pat_credential_for_remote(), not just inside git_push_with_pat().
#
# The guard used here is the subprocess-free validate_remote_syntax()
# (a leading '-' other than exactly '-', and a control character -- not
# the full validate_remote_name() membership check): repo_path at
# this point in the handler has not been proven to be a real, initialized
# git repository, and several pre-existing MCP handler tests
# (test_git_mcp_handlers_remote.py, test_git_mcp_handlers_migration_
# triggers.py) legitimately mock repo_path to a non-repo path while
# mocking git_push_with_pat itself -- a membership check here would shell
# out against that fake path for real and break them. The membership
# check still runs, unconditionally, inside git_push_with_pat before any
# of its own subprocesses (proven by
# TestPushWithPatArgumentValidation::test_git_push_with_pat_rejects_remote_not_configured
# in test_git_operations_service_remote_and_branch_validation.py).
# ---------------------------------------------------------------------------


class TestGitPushHandlerValidatesRemoteBeforeCredentialLookup:
    def _call_git_push(self, repo_path: Path, remote: str) -> Dict[str, Any]:
        import code_indexer.server.mcp.handlers.git_write as git_write

        user = MagicMock()
        user.username = "user_a"

        with patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=(str(repo_path), None),
        ):
            response = git_write.git_push(
                {
                    "repository_alias": "example-repo-global",
                    "remote": remote,
                    "branch": "main",
                },
                user,
            )
        content = response["content"]
        return json.loads(content[0]["text"])  # type: ignore[no-any-return]

    def test_rejects_dash_prefixed_remote_before_any_credential_lookup(
        self, tmp_path: Path
    ):
        repo = self._make_repo(tmp_path)
        marker = tmp_path / "pushed_marker"

        parsed = self._call_git_push(repo, f"--receive-pack=touch {marker}")

        assert parsed.get("success") is not True
        error = str(parsed.get("error", ""))
        assert "must not start with '-'" in error, (
            "Expected the clean GitArgumentValidationError message from "
            "validate_remote_syntax, produced before _get_pat_credential_for_remote "
            f"runs at all. Got: {error!r}"
        )
        assert "Failed to get remote URL" not in error, (
            "This is the wrapped message from "
            "_get_pat_credential_for_remote's own 'git remote get-url' "
            "subprocess failure -- its presence means validation did NOT "
            "run before that call."
        )
        assert not marker.exists()

    @pytest.mark.parametrize("remote", ["golden\n", "gol\x01den"])
    def test_rejects_control_character_remote_before_any_credential_lookup(
        self, tmp_path: Path, remote: str
    ):
        repo = self._make_repo(tmp_path)

        parsed = self._call_git_push(repo, remote)

        assert parsed.get("success") is not True
        error = str(parsed.get("error", ""))
        assert "control character" in error, error
        assert "Failed to get remote URL" not in error, (
            "The control-character check must run before "
            "_get_pat_credential_for_remote's 'git remote get-url' subprocess."
        )

    def test_unconfigured_remote_reaches_credential_lookup_gracefully(
        self, tmp_path: Path
    ):
        """A well-formed but unconfigured remote is not an option-shaped
        value, so validate_remote_syntax() lets it through to
        _get_pat_credential_for_remote() -- which already handles this case
        gracefully via its own 'git remote get-url' failure (no crash, no
        unhandled exception): this documents that intentional design, not
        a gap."""
        repo = self._make_repo(tmp_path)

        parsed = self._call_git_push(repo, "not-a-real-remote")

        assert parsed.get("success") is not True
        error = str(parsed.get("error", ""))
        assert "Failed to get remote URL" in error, (
            f"Expected _get_pat_credential_for_remote's own graceful "
            f"get-url failure message. Got: {error!r}"
        )

    def _make_repo(self, tmp_path: Path) -> Path:
        return _make_repo_with_golden_remote(tmp_path)


# ---------------------------------------------------------------------------
# Real MCP front-door dispatch: protocol.handle_tools_call for git_push,
# git_pull, git_fetch, git_diff, with option-shaped values. Each must be
# rejected and must never create a marker/output file.
# ---------------------------------------------------------------------------


def _parse_mcp_response(response: Dict[str, Any]) -> Dict[str, Any]:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _mock_user() -> MagicMock:
    user = MagicMock()
    user.username = "user_a"
    user.has_permission.return_value = True
    return user


@asynccontextmanager
async def _real_dispatch_context(
    tool_name: str, handler_fn: Any, required_permission: str
):
    """Patch everything handle_tools_call needs EXCEPT the actual handler and
    the argv-safety validation inside it, which run for real -- that is the
    entire point of these tests. session_state is passed directly to
    handle_tools_call (see callers below) rather than going through
    session_registry, since impersonation is not under test here."""
    from code_indexer.server.mcp.protocol import handle_tools_call

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {tool_name: handler_fn},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                tool_name: {
                    "required_permission": required_permission,
                    "name": tool_name,
                }
            },
            create=True,
        ),
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        # These git tools all carry a repository_alias argument, so
        # handle_tools_call's fail-closed AttributeError branch (Story #331
        # AC9) would DENY access before _check_repository_access above is
        # ever reached, unless access_filtering_service resolves cleanly.
        patch(
            "code_indexer.server.app.app.state.access_filtering_service",
            MagicMock(),
            create=True,
        ),
    ):
        yield _mock_user(), handle_tools_call


def _no_impersonation_session_state() -> MagicMock:
    session_state = MagicMock()
    session_state.is_impersonating = False
    return session_state


@pytest.mark.asyncio
class TestGitMcpFrontDoorRejectsInjection:
    async def test_git_push_rejects_receive_pack_remote_through_real_dispatch(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "front_door_push_marker"

        async with _real_dispatch_context(
            "git_push", git_write.git_push, "repository:write"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_push",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "remote": f"--receive-pack=touch {marker}",
                            "branch": "main",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        error = str(parsed.get("error", ""))
        assert "must not start with '-'" in error, (
            "This assertion is the discriminating signal -- without it, "
            "this test also passes on unfixed code (a "
            "malformed/unconfigured remote already fails for an unrelated "
            "reason via _get_pat_credential_for_remote's own 'git remote "
            "get-url' failure). Expected the clean "
            "GitArgumentValidationError message from validate_remote_syntax, "
            f"produced before any credential lookup. Got: {error!r}"
        )
        assert not marker.exists(), (
            "git_push through the real MCP dispatch path must never "
            "execute an option value passed as `remote`"
        )

    async def test_git_push_accepts_plus_main_through_real_dispatch(
        self, tmp_path: Path
    ):
        """`branch="+main"` -- git's own force-push ref marker syntax,
        accepted by `git check-ref-format --allow-onelevel` -- must reach
        `git push` unchanged through the real MCP front door.

        `git_push_with_pat` (the method the MCP `git_push` handler always
        calls) builds its own explicit refspec `HEAD:refs/heads/{branch}`,
        so a leading '+' here becomes part of the destination ref NAME
        rather than a force flag (verified empirically: `git push <url>
        "HEAD:refs/heads/+main"` creates a NEW ref literally named
        `refs/heads/+main`, leaving `main` untouched) -- the value is not
        rejected before it reaches git. The PAT credential lookup is
        mocked (plumbing not under test; a local filesystem remote has no
        forge host to resolve a real credential against).
        """
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo_with_golden_remote(tmp_path)
        remote = repo.parent / "golden.git"
        head_before = _rev_parse(remote, "main")

        fake_credential_manager = MagicMock()
        fake_credential_manager.get_credential_for_host.return_value = {
            "token": "unused-fixture-token",
            "git_user_name": "Test User",
            "git_user_email": "test@example.com",
        }

        async with _real_dispatch_context(
            "git_push", git_write.git_push, "repository:write"
        ) as (user, handle_tools_call):
            with (
                patch(
                    "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                    return_value=(str(repo), None),
                ),
                patch.object(
                    git_write,
                    "_get_credential_manager",
                    return_value=fake_credential_manager,
                ),
                patch(
                    "code_indexer.server.services.git_credential_helper."
                    "GitCredentialHelper.extract_host_from_remote_url",
                    return_value="example.com",
                ),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_push",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "remote": "golden",
                            "branch": "+main",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is True, (
            f"branch='+main' must not be rejected by validate_branch_name "
            f"through the real MCP dispatch path. Got: {parsed!r}"
        )
        assert _rev_parse(remote, "+main") == _rev_parse(repo, "HEAD"), (
            "git_push through the real MCP dispatch path must let "
            "branch='+main' reach git, creating refs/heads/+main"
        )
        assert _rev_parse(remote, "main") == head_before, (
            "the existing remote main ref must be untouched by this call"
        )

    async def test_git_pull_rejects_upload_pack_remote_through_real_dispatch(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_write as git_write
        from code_indexer.server.services.git_operations_service import (
            GitOperationsService,
        )

        repo = _make_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "front_door_pull_marker"

        mock_arm = MagicMock()
        mock_arm.get_activated_repo_path.return_value = str(repo)

        async with _real_dispatch_context(
            "git_pull", git_write.git_pull, "repository:write"
        ) as (user, handle_tools_call):
            with patch.object(GitOperationsService, "activated_repo_manager", mock_arm):
                result = await handle_tools_call(
                    params={
                        "name": "git_pull",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "remote": f"--upload-pack=touch {marker}",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert not marker.exists(), (
            "git_pull through the real MCP dispatch path must never "
            "execute an option value passed as `remote`"
        )

    async def test_git_fetch_rejects_upload_pack_remote_through_real_dispatch(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_read as git_read
        from code_indexer.server.services.git_operations_service import (
            GitOperationsService,
        )

        repo = _make_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "front_door_fetch_marker"

        mock_arm = MagicMock()
        mock_arm.get_activated_repo_path.return_value = str(repo)

        async with _real_dispatch_context(
            "git_fetch", git_read.git_fetch, "repository:write"
        ) as (user, handle_tools_call):
            with patch.object(GitOperationsService, "activated_repo_manager", mock_arm):
                result = await handle_tools_call(
                    params={
                        "name": "git_fetch",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "remote": f"--upload-pack=touch {marker}",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert not marker.exists(), (
            "git_fetch through the real MCP dispatch path must never "
            "execute an option value passed as `remote`"
        )

    async def test_git_diff_rejects_no_index_output_option_through_real_dispatch(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_read as git_read

        repo = _make_repo_with_golden_remote(tmp_path)
        decoy = tmp_path / "decoy_secret.txt"
        decoy.write_text("decoy test content\n")
        output_marker = repo / "output_marker.txt"

        async with _real_dispatch_context(
            "git_diff", git_read.git_diff, "query_repos"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_diff",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "from_revision": "--no-index",
                            "file_paths": [
                                "--output=output_marker.txt",
                                "--text",
                                str(decoy),
                                "/dev/null",
                            ],
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert not output_marker.exists(), (
            "git_diff through the real MCP dispatch path must never write "
            "a file from an --output= value passed as a file_paths entry"
        )
