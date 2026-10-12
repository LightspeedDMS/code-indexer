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
import subprocess
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


def _content_raw(text: str) -> dict:
    """A success result whose content text is NOT valid JSON."""
    return {"content": [{"type": "text", "text": text}]}


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

    def test_diff_name_only_uses_git_mcp_name_only_files(self, backend, monkeypatch):
        # Uses git-mcp's own --name-only list (never a lossy unified-diff parse), so
        # even a quoted / non-ASCII path survives intact.
        calls = _install(
            monkeypatch, lambda b: _rpc(_content({"files": ["x.py", "dir/café.py"]}))
        )
        assert backend.diff_name_only(REPO, "HEAD~1") == ["x.py", "dir/café.py"]
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["name_only"] is True
        assert args["base"] == "HEAD~1"

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
    def test_checkout_create_only(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.checkout(REPO, "feature", create=True)
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["cow_path"] == REPO
        assert args["target"] == "feature"
        assert args["create"] is True
        # create (-b) and force_new (-B) must NOT collapse — force_new absent here.
        assert "force_new" not in args

    def test_checkout_force_new_and_start_point_are_distinct(
        self, backend, monkeypatch
    ):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.checkout(REPO, "feature", force_new=True, start_point="origin/feature")
        args = calls[-1]["body"]["params"]["arguments"]
        assert args["force_new"] is True
        assert args["start_point"] == "origin/feature"
        # force_new is a separate signal from create (-B vs -b).
        assert "create" not in args

    def test_checkout_force(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.checkout(REPO, "HEAD", force=True)
        assert calls[-1]["body"]["params"]["arguments"]["force"] is True

    def test_pull_rebase_mode(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.pull(REPO, mode="rebase")
        assert calls[-1]["body"]["params"]["arguments"]["rebase"] is True

    def test_pull_no_ff_mode(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.pull(REPO, mode="no-ff")
        assert calls[-1]["body"]["params"]["arguments"]["no_ff"] is True

    def test_pull_rejects_unknown_mode(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({})))
        with pytest.raises(GitMcpBackendError, match="unsupported mode"):
            backend.pull(REPO, mode="bogus")

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


class TestExceptionContract:
    """The ~30 seam callers do `except subprocess.CalledProcessError` and read
    `.stderr`; the delegated backend MUST raise that type, with git-mcp's error
    text in `.stderr`, or every recovery path goes dead (a raw RuntimeError would
    escape uncaught)."""

    def test_error_is_called_process_error(self):
        assert issubclass(GitMcpBackendError, subprocess.CalledProcessError)

    def test_tool_error_populates_stderr_for_recovery_matching(
        self, backend, monkeypatch
    ):
        # e.g. remote-already-exists fallback greps e.stderr for "already exists".
        _install(
            monkeypatch,
            lambda b: json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "fatal: remote origin already exists",
                            }
                        ],
                    },
                }
            ),
        )
        try:
            backend.remote_add(REPO, "origin", "http://x/y.git")
            raise AssertionError("expected raise")
        except subprocess.CalledProcessError as e:  # caught as the contract type
            assert "already exists" in (e.stderr or "")


class TestWrongShapeBodyFailsLoud:
    """A valid-transport 200 with an unexpected body must NOT read as clean/empty —
    that would silently corrupt cidx's git-state view."""

    def test_unparseable_nonempty_body_raises(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content_raw("not json at all")))
        with pytest.raises(GitMcpBackendError, match="unparseable body"):
            backend.current_branch(REPO)

    def test_status_missing_buckets_raises_not_clean(self, backend, monkeypatch):
        # `{}` would previously read as a CLEAN tree; now it fails loud.
        _install(monkeypatch, lambda b: _rpc(_content({})))
        with pytest.raises(GitMcpBackendError, match="status buckets"):
            backend.status_porcelain(REPO)

    def test_ls_files_missing_key_raises_not_empty(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({})))
        with pytest.raises(GitMcpBackendError, match="missing 'files'"):
            backend.ls_files(REPO)

    def test_rev_parse_missing_output_raises(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({})))
        with pytest.raises(GitMcpBackendError, match="missing 'output'"):
            backend.rev_parse(REPO, "HEAD")

    def test_list_branches_rejects_unsupported_fmt(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"branches": []})))
        with pytest.raises(GitMcpBackendError, match="cannot honour fmt"):
            backend.list_branches(REPO, fmt="%(objectname)")

    def test_list_branches_accepts_refname_short_fmt(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(_content({"branches": [{"name": "main", "type": "local"}]})),
        )
        assert backend.list_branches(REPO, fmt="%(refname:short)") == ["main"]


class TestCapabilities:
    def test_owns_credentials_true(self, backend):
        # git-mcp holds the token and injects it, so cidx must strip any credential
        # before storing a URL — the PAT lives only in git-mcp.
        assert backend.owns_credentials is True


class TestFactory:
    def test_make_git_backend_reads_env(self, monkeypatch):
        monkeypatch.setenv("CIDX_GIT_MCP_URL", "http://custom:9/")
        monkeypatch.setenv("CIDX_GIT_MCP_TIMEOUT", "42")
        b = plugin.make_git_backend()
        assert b._url == "http://custom:9/v1/git/mcp"
        assert b._timeout == 42.0


class TestHistoryOps:
    """cidx temporal history: paged log, batched diffs and object reads.

    git-mcp returns git's stdout base64-encoded; the plugin hands cidx those
    exact bytes and fails as a local ``check=True`` run would.
    """

    @staticmethod
    def _raw(stdout: bytes, exit_code: int = 0, stderr: str = "") -> dict:
        import base64

        return {
            "exit_code": exit_code,
            "stdout_b64": base64.b64encode(stdout).decode(),
            "stderr": stderr,
        }

    def test_log_raw_sends_a_page_and_returns_bytes(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content(self._raw(b"\xff\x1e"))))
        out = backend.log_raw(REPO, "%H", ["abc"], since="2024-01-01", max_count=5)
        assert out == b"\xff\x1e"
        params = calls[0]["body"]["params"]
        assert params["name"] == "git_log_raw"
        assert params["arguments"] == {
            "cow_path": REPO,
            "format": "%H",
            "revs": ["abc"],
            "since": "2024-01-01",
            "max_count": 5,
        }

    def test_log_raw_git_failure_raises_with_git_stderr(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(_content(self._raw(b"", 128, "fatal: bad object"))),
        )
        with pytest.raises(GitMcpBackendError) as exc:
            backend.log_raw(REPO, "%H")
        assert exc.value.returncode == 128
        assert exc.value.stderr == "fatal: bad object"

    def test_diff_commits_is_one_call_per_batch(self, backend, monkeypatch):
        def respond(body):
            pairs = body["params"]["arguments"]["pairs"]
            return _rpc(
                _content({"diffs": [self._raw(p["commit"].encode()) for p in pairs]})
            )

        calls = _install(monkeypatch, respond)
        pairs = [(f"{i:040x}", f"{i + 1:040x}") for i in range(300)]
        out = backend.diff_commits(REPO, pairs, 5)
        assert out == [c.encode() for _, c in pairs]
        assert [len(c["body"]["params"]["arguments"]["pairs"]) for c in calls] == [
            256,
            44,
        ]
        assert calls[0]["body"]["params"]["arguments"]["context_lines"] == 5

    def test_diff_commits_raises_for_a_failed_item(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(_content({"diffs": [self._raw(b"", 128, "bad object")]})),
        )
        with pytest.raises(GitMcpBackendError, match="bad object"):
            backend.diff_commits(REPO, [("a" * 40, "b" * 40)], 5)

    def test_diff_commits_rejects_a_short_answer(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"diffs": []})))
        with pytest.raises(GitMcpBackendError, match="results for 1 items"):
            backend.diff_commits(REPO, [("a" * 40, "b" * 40)], 5)

    def test_show_objects_returns_completed_processes(self, backend, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda b: _rpc(
                _content(
                    {
                        "objects": [
                            self._raw(b"\x00bin"),
                            self._raw(b"", 128, "fatal: path 'x' does not exist"),
                        ]
                    }
                )
            ),
        )
        out = backend.show_objects(REPO, [("a" * 40, "bin.dat"), ("a" * 40, "x")])
        assert calls[0]["body"]["params"]["arguments"]["specs"] == [
            {"rev": "a" * 40, "path": "bin.dat"},
            {"rev": "a" * 40, "path": "x"},
        ]
        assert (out[0].returncode, out[0].stdout) == (0, b"\x00bin")
        assert out[1].returncode == 128 and "does not exist" in out[1].stderr

    def test_a_wrong_shape_item_fails_loud(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"objects": [{"exit_code": 0}]})))
        with pytest.raises(GitMcpBackendError, match="stdout_b64"):
            backend.show_objects(REPO, [("a" * 40, "x")])

    def test_stdout_that_is_not_base64_fails_loud(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                _content({"exit_code": 0, "stdout_b64": "!!!", "stderr": ""})
            ),
        )
        with pytest.raises(GitMcpBackendError, match="not base64"):
            backend.log_raw(REPO, "%H")
