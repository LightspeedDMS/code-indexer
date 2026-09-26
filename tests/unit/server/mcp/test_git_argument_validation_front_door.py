"""Front-door proof that the git argv-safety fixes are actually WIRED into
the real MCP dispatch path, not just reachable via a direct unit-level call
to a service method (per this repo's Definition of Done: "wire it or don't
write it").

Each test drives an option-shaped value through the REAL MCP protocol
dispatcher (`protocol.handle_tools_call` -> `_invoke_handler` -> the actual
registered handler function) against a REAL throwaway git repository (no
mocking of git itself, per this repo's Anti-Mock rule). Only the
alias-to-filesystem-path resolution layer and the surrounding MCP plumbing
not under test (repository access filtering, langfuse tracing, API
metrics) are patched.

Covers:
  - git_blame         (global_repos.GitOperationsService.get_blame)
  - git_log           (omni/multi-repo array path -> global_repos
                        .GitOperationsService.get_log, a code path distinct
                        from the single-alias git_log handler)
  - git_branch_switch (server GitOperationsService.git_branch_switch)
  - git_branch_create (server GitOperationsService.git_branch_create)
  - git_branch_delete (server GitOperationsService.git_branch_delete)
  - git_stage         (server GitOperationsService.git_stage)
  - git_reset         (server GitOperationsService.git_reset)
  - git_merge         (server GitOperationsService.merge_branch)

Each rejection test asserts on the validator's own rejection message (the
"must not start with '-'" text), never on a bare `success is not True`.
That distinction matters here: git_merge's `-Xtheirs` is real git option
syntax, so git's own argument parser also rejects it with a non-zero exit
when the validator is bypassed -- a bare success check would pass either
way and would not prove the validator ran.

git_stage is the exception: its pathspecs always follow a `--`
separator, so an option-shaped entry such as `--chmod=+x` is a literal
path that git itself reports as unmatched (asserted on git's own
"did not match" text, which only appears when the separator is present),
and a real filename containing LF stages normally.

`git_branch_switch` uses a non-`-global` repository alias: a `-global`
alias is rejected by an unrelated golden-repo guard before the handler
ever reaches the service, which would make that test pass regardless of
the fix under test.

`git_reset` and `git_branch_delete` route every `ValueError` (their
confirmation-token machinery raises one on an invalid token) through a
shared helper that has its own pre-existing, unrelated defect (filed, not
fixed here). A validation rejection is a `ValueError` subclass, so each
handler now catches it explicitly, ahead of the generic `ValueError`
clause, and returns the same clean structured error the other git
handlers return.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_SESSION_ID = "mcp-session-git-argument-validation-front-door"


def _git(args: list, cwd: Path) -> None:
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["checkout", "-q", "-b", "main"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "first"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    _git(["checkout", "-q", "-b", "feature/nested"], repo)
    (repo / "f.txt").write_text("one\ntwo\nthree\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "feature commit"], repo)
    _git(["checkout", "-q", "main"], repo)
    return repo


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
    """Patch everything handle_tools_call needs EXCEPT the actual handler
    and the argv-safety validation inside it, which run for real."""
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
class TestGitBlameFrontDoor:
    async def test_rejects_contents_option_through_real_dispatch(self, tmp_path: Path):
        import code_indexer.server.mcp.handlers.git_read as git_read

        repo = _make_repo(tmp_path)
        outside_file = tmp_path / "outside_marker.txt"
        outside_file.write_text("marker-content\n")

        async with _real_dispatch_context(
            "git_blame", git_read.handle_git_blame, "query_repos"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_blame",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "path": "f.txt",
                            "revision": f"--contents={outside_file}",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
        assert "marker-content" not in json.dumps(parsed)


@pytest.mark.asyncio
class TestGitLogOmniFrontDoor:
    async def test_rejects_output_option_as_branch_through_real_dispatch(
        self, tmp_path: Path
    ):
        """repository_alias as an array routes through _omni_git_log ->
        handle_git_log -> global_repos.GitOperationsService.get_log, a
        code path distinct from the single-alias git_log handler (which
        already goes through the server's own GitOperationsService)."""
        import code_indexer.server.mcp.handlers.git_read as git_read

        repo = _make_repo(tmp_path)
        marker = tmp_path / "git_log_omni_output_marker.txt"

        async with _real_dispatch_context(
            "git_log", git_read.handle_git_log, "query_repos"
        ) as (user, handle_tools_call):
            with (
                patch(
                    "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                    return_value=(str(repo), None),
                ),
                patch(
                    "code_indexer.server.app.app.state.golden_repos_dir",
                    str(tmp_path),
                    create=True,
                ),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_log",
                        "arguments": {
                            "repository_alias": ["example-repo-global"],
                            "branch": f"--output={marker}",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert not marker.exists(), (
            "branch must resolve via git rev-parse --verify and never start with '-'"
        )
        errors = parsed.get("errors", {})
        assert "must not start with '-'" in str(
            errors.get("example-repo-global", "")
        ), f"expected a clean rejection message in errors, got: {parsed!r}"


@pytest.mark.asyncio
class TestGitBranchSwitchFrontDoor:
    async def test_rejects_leading_dash_through_real_dispatch(self, tmp_path: Path):
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)
        (repo / "f.txt").write_text("uncommitted local edit\n")

        async with _real_dispatch_context(
            "git_branch_switch", git_write.git_branch_switch, "repository:write"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_branch_switch",
                        "arguments": {
                            # A "-global" alias is rejected by an unrelated
                            # golden-repo guard before the handler reaches
                            # the service, which would make this test pass
                            # regardless of the fix under test.
                            "repository_alias": "example-repo",
                            "branch_name": "-f",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
        assert (repo / "f.txt").read_text() == "uncommitted local edit\n", (
            "branch_name must be a well-formed ref name and must never "
            "start with '-' or '+'"
        )


@pytest.mark.asyncio
class TestGitBranchCreateFrontDoor:
    async def test_rejects_leading_dash_through_real_dispatch(self, tmp_path: Path):
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)

        async with _real_dispatch_context(
            "git_branch_create", git_write.git_branch_create, "repository:write"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_branch_create",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "branch_name": "--track",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
        branches = subprocess.run(
            ["git", "branch"], cwd=str(repo), capture_output=True, text=True
        ).stdout
        assert "--track" not in branches


@pytest.mark.asyncio
class TestGitStageFrontDoor:
    async def _stage(self, tmp_path: Path, repo: Path, file_paths: list) -> dict:
        import code_indexer.server.mcp.handlers.git_write as git_write

        async with _real_dispatch_context(
            "git_stage", git_write.git_stage, "repository:write"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_stage",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "file_paths": file_paths,
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )
        parsed: dict = _parse_mcp_response(result)
        return parsed

    async def test_option_shaped_pathspec_follows_separator_through_real_dispatch(
        self, tmp_path: Path
    ):
        """`git add -- <paths>`: `--chmod=+x` is a literal pathspec, so git
        itself rejects it as unmatched and stages nothing. Without the
        separator git would read it as an option and stage exec_me.txt
        with mode 100755."""
        repo = _make_repo(tmp_path)
        (repo / "exec_me.txt").write_text("plain file\n")

        parsed = await self._stage(tmp_path, repo, ["--chmod=+x", "exec_me.txt"])

        assert parsed.get("success") is not True
        assert "pathspec '--chmod=+x' did not match" in json.dumps(parsed)
        ls_files = subprocess.run(
            ["git", "ls-files", "-s", "exec_me.txt"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout
        assert ls_files == ""

    async def test_stages_real_file_with_lf_in_name_through_real_dispatch(
        self, tmp_path: Path
    ):
        repo = _make_repo(tmp_path)
        name = "line\nbreak.txt"
        (repo / name).write_text("content\n")

        parsed = await self._stage(tmp_path, repo, [name])

        assert parsed.get("success") is True, parsed
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout.split("\0")[:-1]
        assert staged == [name]


@pytest.mark.asyncio
class TestGitMergeFrontDoor:
    async def test_rejects_option_shaped_source_branch_through_real_dispatch(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        async with _real_dispatch_context(
            "git_merge", git_write.git_merge, "repository:write"
        ) as (user, handle_tools_call):
            with (
                patch(
                    "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                    return_value=(str(repo), None),
                ),
                patch(
                    "code_indexer.server.mcp.handlers.git_write._check_writable",
                    return_value=None,
                ),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_merge",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "source_branch": "-Xtheirs",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        # "-Xtheirs" is itself a real `git merge` strategy-option flag, so
        # git's own argument parser also rejects it with a non-zero exit
        # when the validator is bypassed. The validator's own message is
        # the only signal that discriminates.
        assert "must not start with '-'" in str(parsed.get("error", ""))
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        assert head_after == head_before


@pytest.mark.asyncio
class TestGitResetFrontDoor:
    async def test_rejects_option_shaped_commit_hash_with_clean_error(
        self, tmp_path: Path
    ):
        """An invalid commit_hash must return a clean structured
        validation error through the real MCP dispatch path, not crash
        the confirmation-token machinery."""
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        async with _real_dispatch_context(
            "git_reset", git_write.git_reset, "repository:admin"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_reset",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "mode": "mixed",
                            "commit_hash": "-x",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
        assert "confirmation_token_required" not in parsed
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        assert head_after == head_before


@pytest.mark.asyncio
class TestGitBranchDeleteFrontDoor:
    async def test_rejects_leading_dash_with_clean_error(self, tmp_path: Path):
        """An invalid branch_name must return a clean structured
        validation error through the real MCP dispatch path, not crash
        the confirmation-token machinery."""
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)

        async with _real_dispatch_context(
            "git_branch_delete", git_write.git_branch_delete, "repository:admin"
        ) as (user, handle_tools_call):
            with patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=(str(repo), None),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_branch_delete",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "branch_name": "-D",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
        assert "confirmation_token_required" not in parsed
        branches = subprocess.run(
            ["git", "branch"], cwd=str(repo), capture_output=True, text=True
        ).stdout
        assert "feature/nested" in branches
