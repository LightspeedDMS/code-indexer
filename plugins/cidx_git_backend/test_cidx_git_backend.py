"""
Tests for the git-mcp-backed GitBackend (EVO-64616 Phase 3).

The GitBackend contract is structural (cidx's Protocol) — so these pin the two
things that can silently drift from git-mcp: (1) the JSON-RPC envelope and
cow_path addressing every method sends, and (2) the reconstruction of the handful
of methods cidx expects as RAW git output (status_porcelain, remote_list -v,
diff_name_only, list_branches, show_ref→CompletedProcess). Transport/tool errors
must surface as GitMcpBackendError, never a silent empty result — a delegated
backend that quietly "succeeded" would corrupt cidx's git state view.

The live end-to-end pass (against the deployed git-mcp on a real repo) proved the
happy path; this locks the contract with no cluster needed.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import cidx_git_backend as plugin
from cidx_git_backend import GitMcpBackend, GitMcpBackendError

REPO = "/data/data/golden-repos/foo/master"


class _FakeResp:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rpc(result: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})


def _content(payload) -> dict:
    """git-mcp wraps a tool's JSON return as content[0].text."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


@pytest.fixture()
def backend(monkeypatch):
    monkeypatch.delenv("CIDX_GIT_MCP_URL", raising=False)
    monkeypatch.delenv("CIDX_GIT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("CIDX_GIT_MCP_TIMEOUT", raising=False)
    return GitMcpBackend(base_url="http://git-mcp:3000")


def _install(monkeypatch, responder):
    """Patch urlopen; `responder(parsed_body) -> raw_json_str`. Records calls."""
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append({"req": req, "body": body, "timeout": timeout})
        return _FakeResp(responder(body))

    monkeypatch.setattr(plugin.urllib.request, "urlopen", fake_urlopen)
    return calls


class TestTransportEnvelope:
    def test_jsonrpc_shape_tool_and_cow_path(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({"ref": "master"})))
        backend.current_branch(REPO)
        body = calls[-1]["body"]
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "git_symbolic_ref"
        # cow_path addressing: cidx passes its own repo path, no workspace_id.
        assert body["params"]["arguments"]["cow_path"] == REPO
        assert "workspace_id" not in body["params"]["arguments"]

    def test_bearer_header_sent_when_token_set(self, monkeypatch):
        b = GitMcpBackend(base_url="http://git-mcp:3000", token="tok123")
        calls = _install(monkeypatch, lambda body: _rpc(_content({"ref": "m"})))
        b.current_branch(REPO)
        assert calls[-1]["req"].headers["Authorization"] == "Bearer tok123"

    def test_no_auth_header_without_token(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda body: _rpc(_content({"ref": "m"})))
        backend.current_branch(REPO)
        assert "Authorization" not in calls[-1]["req"].headers

    def test_per_call_timeout_forwarded(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda body: _rpc(_content({"reachable": True})))
        backend.ls_remote("http://x/y.git", timeout=7)
        assert calls[-1]["timeout"] == 7


class TestStructuredReads:
    def test_ls_remote_bool(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"reachable": True})))
        assert backend.ls_remote("http://x/y.git") is True
        _install(monkeypatch, lambda b: _rpc(_content({"reachable": False})))
        assert backend.ls_remote("http://x/y.git") is False

    def test_rev_parse_strips(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"output": "abc123\n"})))
        assert backend.rev_parse(REPO, "HEAD") == "abc123"

    def test_rev_list_count_int(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"count": 5})))
        assert backend.rev_list_count(REPO, "a..b") == 5

    def test_ls_files_list(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"files": ["a.txt", "b/c.py"]})))
        assert backend.ls_files(REPO) == ["a.txt", "b/c.py"]


class TestReconstruction:
    def test_status_porcelain_untracked_and_tracked(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content(
                    {
                        "unstaged": [{"path": "a.txt", "status": "modified"}],
                        "untracked": [{"path": "c.txt"}],
                    }
                )
            ),
        )
        out = backend.status_porcelain(REPO)
        lines = out.splitlines()
        # cidx filters `??`; a tracked line must survive as non-`??`.
        assert " M a.txt" in lines
        assert "?? c.txt" in lines

    def test_status_porcelain_deleted_and_conflict(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content(
                    {
                        "unstaged": [{"path": "d.txt", "status": "deleted"}],
                        "conflicts": [{"path": "e.txt"}],
                        "staged": [{"path": "s.txt"}],
                    }
                )
            ),
        )
        lines = backend.status_porcelain(REPO).splitlines()
        assert " D d.txt" in lines
        assert "UU e.txt" in lines
        assert "M  s.txt" in lines

    def test_remote_list_verbose_format(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content(
                    {
                        "remotes": [
                            {
                                "name": "origin",
                                "fetch": "http://x/y.git",
                                "push": "http://x/y.git",
                            }
                        ]
                    }
                )
            ),
        )
        out = backend.remote_list(REPO, verbose=True)
        assert out == ("origin\thttp://x/y.git (fetch)\norigin\thttp://x/y.git (push)")

    def test_remote_list_names_only(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content({"remotes": [{"name": "origin"}, {"name": "upstream"}]})
            ),
        )
        assert backend.remote_list(REPO) == "origin\nupstream"

    def test_list_branches_filters_by_type(self, backend, monkeypatch):
        payload = {
            "branches": [
                {"name": "master", "type": "local"},
                {"name": "origin/master", "type": "remote"},
            ]
        }
        _install(monkeypatch, lambda b: _rpc(_content(payload)))
        assert backend.list_branches(REPO) == ["master"]
        _install(monkeypatch, lambda b: _rpc(_content(payload)))
        assert backend.list_branches(REPO, remote=True) == ["origin/master"]
        _install(monkeypatch, lambda b: _rpc(_content(payload)))
        assert backend.list_branches(REPO, all_refs=True) == [
            "master",
            "origin/master",
        ]

    def test_diff_name_only_parses_headers(self, backend, monkeypatch):
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/dir/y.txt b/dir/y.txt\n"
        )
        _install(monkeypatch, lambda b: _rpc(_content({"diff": diff})))
        assert backend.diff_name_only(REPO, "HEAD~1") == ["x.py", "dir/y.txt"]

    def test_show_ref_completed_process(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content({"output": "abc refs/heads/master\n", "found": True})
            ),
        )
        cp = backend.show_ref(REPO, "--heads")
        assert cp.returncode == 0
        assert "refs/heads/master" in cp.stdout

    def test_show_ref_not_found_returncode_1(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"output": "", "found": False})))
        cp = backend.show_ref(REPO, "refs/heads/nope")
        assert cp.returncode == 1


class TestMutationArguments:
    def test_checkout_create_and_force(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.checkout(REPO, "feature", create=True, force=True)
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["cow_path"] == REPO
        assert args["target"] == "feature"
        assert args["create"] is True
        assert args["force"] is True

    def test_pull_rebase_mode(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.pull(REPO, mode="rebase")
        assert calls[-1]["body"]["params"]["arguments"]["rebase"] is True

    def test_reset_hard(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.reset(REPO, "hard", "origin/master")
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["mode"] == "hard" and args["target"] == "origin/master"

    def test_config_set(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.config_set(REPO, "user.name", "x")
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["key"] == "user.name" and args["value"] == "x"


class TestErrorsFailLoud:
    def test_tool_error_raises(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "boom"}],
                    },
                }
            ),
        )
        with pytest.raises(GitMcpBackendError, match="tool error"):
            backend.current_branch(REPO)

    def test_jsonrpc_error_raises(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "x"}}
            ),
        )
        with pytest.raises(GitMcpBackendError, match="JSON-RPC error"):
            backend.current_branch(REPO)

    def test_transport_error_raises(self, backend, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(plugin.urllib.request, "urlopen", boom)
        with pytest.raises(GitMcpBackendError, match="request to .* failed"):
            backend.current_branch(REPO)


class TestFactory:
    def test_make_git_backend_reads_env(self, monkeypatch):
        monkeypatch.setenv("CIDX_GIT_MCP_URL", "http://custom:9/")
        monkeypatch.setenv("CIDX_GIT_MCP_TIMEOUT", "42")
        b = plugin.make_git_backend()
        assert b._url == "http://custom:9/v1/git/mcp"
        assert b._timeout == 42.0
