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


class _FakeBytesResp(_FakeResp):
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


def _http_error(url: str, code: int, payload: dict) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        url, code, "error", {}, io.BytesIO(json.dumps(payload).encode())
    )


#: How NestJS answers a route it does not have: a git-mcp predating raw exec.
_NO_RAW_ROUTE = {"statusCode": 404, "message": "Cannot POST /v1/git/exec/raw"}


def _install(monkeypatch, responder, raw=None):
    """Patch urlopen; `responder(parsed_body) -> raw_json_str` answers the MCP
    endpoint, `raw(parsed_body) -> bytes | HTTPError` the raw exec endpoint
    (default: a git-mcp without it, 404). Records calls."""
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append({"req": req, "body": body, "timeout": timeout})
        if req.full_url.endswith("/v1/git/exec/raw"):
            answer = (
                raw(body)
                if raw is not None
                else _http_error(req.full_url, 404, _NO_RAW_ROUTE)
            )
            if isinstance(answer, Exception):
                raise answer
            return _FakeBytesResp(answer)
        return _FakeResp(responder(body))

    monkeypatch.setattr(plugin.urllib.request, "urlopen", fake_urlopen)
    return calls


def _tool_calls(calls):
    """The MCP ``tools/call`` requests among *calls* (``run`` first tries
    the raw exec endpoint)."""
    return [c for c in calls if c["body"].get("method") == "tools/call"]


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

    def test_fetch_of_one_branch_sends_ref(self, backend, monkeypatch):
        """EVO-64616: change_branch fetches only the target branch, never the
        clone's refspec (which may name a branch upstream deleted)."""
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.fetch(REPO, remote="origin", ref="master")
        args = calls[-1]["body"]["params"]["arguments"]
        assert calls[-1]["body"]["params"]["name"] == "git_fetch"
        assert args["ref"] == "master" and args["remote"] == "origin"

    def test_plain_fetch_sends_no_ref(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({})))
        backend.fetch(REPO)
        assert "ref" not in calls[-1]["body"]["params"]["arguments"]


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

    def test_for_each_ref_sends_one_call_and_returns_bytes(self, backend, monkeypatch):
        calls = _install(
            monkeypatch, lambda b: _rpc(_content(self._raw(b"main|\xff\n")))
        )
        out = backend.for_each_ref(
            REPO,
            "%(refname:short)|%(objectname)",
            ["refs/heads/", "refs/remotes/origin/"],
            sort="-committerdate",
            count=5,
            timeout=7,
        )
        assert out == b"main|\xff\n"
        assert len(calls) == 1
        params = calls[0]["body"]["params"]
        assert params["name"] == "git_for_each_ref"
        assert params["arguments"] == {
            "cow_path": REPO,
            "format": "%(refname:short)|%(objectname)",
            "patterns": ["refs/heads/", "refs/remotes/origin/"],
            "sort": "-committerdate",
            "count": 5,
        }
        assert calls[0]["timeout"] == 7

    def test_for_each_ref_omits_unset_options(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content(self._raw(b""))))
        assert backend.for_each_ref(REPO, "%(refname)") == b""
        assert calls[0]["body"]["params"]["arguments"] == {
            "cow_path": REPO,
            "format": "%(refname)",
        }

    def test_for_each_ref_git_failure_raises_with_git_stderr(
        self, backend, monkeypatch
    ):
        _install(
            monkeypatch,
            lambda b: _rpc(_content(self._raw(b"", 129, "error: unknown field"))),
        )
        with pytest.raises(GitMcpBackendError) as exc:
            backend.for_each_ref(REPO, "%(bogus)")
        assert exc.value.returncode == 129
        assert exc.value.stderr == "error: unknown field"

    def test_for_each_ref_wrong_shape_fails_loud(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content({"exit_code": 0})))
        with pytest.raises(GitMcpBackendError, match="stdout_b64"):
            backend.for_each_ref(REPO, "%(refname)")

    def test_for_each_ref_tool_error_fails_loud(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                {"isError": True, "content": [{"type": "text", "text": "bad sort"}]}
            ),
        )
        with pytest.raises(GitMcpBackendError, match="bad sort"):
            backend.for_each_ref(REPO, "%(refname)", sort="--x")

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


class TestGitToolsRunner:
    """cidx's git tools on an activated copy: git_exec (raw, exit code
    returned) and git_push (git-mcp's credentials)."""

    @staticmethod
    def _raw(stdout: bytes, exit_code: int = 0, stderr: str = "") -> dict:
        import base64

        return {
            "exit_code": exit_code,
            "stdout_b64": base64.b64encode(stdout).decode(),
            "stderr": stderr,
        }

    def test_backend_is_a_git_command_runner(self, backend):
        from code_indexer.server.storage.shared.git_backend import GitCommandRunner

        assert isinstance(backend, GitCommandRunner)

    def test_run_sends_git_exec_and_returns_text(self, backend, monkeypatch):
        calls = _install(
            monkeypatch, lambda b: _rpc(_content(self._raw(" M a.txt\n".encode())))
        )
        out = backend.run(
            REPO,
            ["status", "--porcelain"],
            timeout=30,
            identity={"author_name": "A", "author_email": "a@example.com"},
        )
        assert (out.returncode, out.stdout, out.args) == (
            0,
            " M a.txt\n",
            ["git", "status", "--porcelain"],
        )
        params = _tool_calls(calls)[0]["body"]["params"]
        assert params["name"] == "git_exec"
        assert params["arguments"] == {
            "cow_path": REPO,
            "args": ["status", "--porcelain"],
            "timeout_seconds": 30,
            "identity": {"author_name": "A", "author_email": "a@example.com"},
        }

    def test_run_returns_a_git_failure_instead_of_raising(self, backend, monkeypatch):
        _install(monkeypatch, lambda b: _rpc(_content(self._raw(b"", 1, "fatal: x"))))
        out = backend.run(REPO, ["checkout", "nope"])
        assert (out.returncode, out.stderr) == (1, "fatal: x")

    def test_run_omits_unset_options(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content(self._raw(b""))))
        backend.run(REPO, ["log"])
        assert _tool_calls(calls)[0]["body"]["params"]["arguments"] == {
            "cow_path": REPO,
            "args": ["log"],
        }

    @staticmethod
    def _local_text(raw: bytes, tmp_path) -> str:
        """What a local ``subprocess.run(text=True)`` returns for *raw* stdout."""
        path = tmp_path / "stdout.bin"
        path.write_bytes(raw)
        return subprocess.run(
            ["cat", str(path)], capture_output=True, text=True, check=True
        ).stdout

    def test_run_decodes_exactly_as_a_local_run(self, backend, monkeypatch, tmp_path):
        raw = "crlf\r\nlone cr\rünïcode – 日本\n".encode()
        _install(monkeypatch, lambda b: _rpc(_content(self._raw(raw))))
        out = backend.run(REPO, ["diff"])
        assert out.stdout == self._local_text(raw, tmp_path)
        assert out.stdout == "crlf\nlone cr\nünïcode – 日本\n"

    def test_run_undecodable_output_raises_as_a_local_run_does(
        self, backend, monkeypatch, tmp_path
    ):
        raw = b"caf\xe9\n"
        _install(monkeypatch, lambda b: _rpc(_content(self._raw(raw))))
        with pytest.raises(UnicodeDecodeError):
            backend.run(REPO, ["diff"])
        with pytest.raises(UnicodeDecodeError):
            self._local_text(raw, tmp_path)

    def test_run_transport_error_raises(self, backend, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(plugin.urllib.request, "urlopen", boom)
        with pytest.raises(GitMcpBackendError):
            backend.run(REPO, ["status"])

    def test_push_sends_git_push(self, backend, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda b: _rpc(
                _content({"branch": "w", "_guidance": {"message": "Pushed w"}})
            ),
        )
        out = backend.push(REPO, "origin", "w")
        assert (out.returncode, out.stdout) == (0, "Pushed w")
        params = calls[0]["body"]["params"]
        assert params["name"] == "git_push"
        assert params["arguments"] == {
            "cow_path": REPO,
            "remote": "origin",
            "branch": "w",
        }

    def test_push_tool_error_raises(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                {"isError": True, "content": [{"type": "text", "text": "rejected"}]}
            ),
        )
        with pytest.raises(GitMcpBackendError) as exc:
            backend.push(REPO, "origin", "main")
        assert exc.value.stderr == "rejected"


class TestPaginatedResultsRefused:
    """git-mcp's response layer pages an oversized result and marks it with
    ``_pagination``. A page is never the answer: every tool refuses it."""

    @staticmethod
    def _page(payload: dict) -> dict:
        return {**payload, "_pagination": {"page": 1, "total_pages": 3}}

    def test_git_exec_page_is_an_error(self, backend, monkeypatch):
        import base64

        page = self._page(
            {
                "exit_code": 0,
                "stdout_b64": base64.b64encode(b"a" * 30).decode(),
                "stderr": "",
            }
        )
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        with pytest.raises(plugin.GitMcpPaginatedResultError, match="paginated"):
            backend.run(REPO, ["log"])

    def test_page_with_cut_base64_is_reported_as_pagination(self, backend, monkeypatch):
        page = self._page({"exit_code": 0, "stdout_b64": "YWJj", "stderr": ""})
        page["stdout_b64"] = "YWJjZG"  # cut mid-quantum, as a real page is
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        with pytest.raises(plugin.GitMcpPaginatedResultError):
            backend.log_raw(REPO, "%H")

    @pytest.mark.parametrize(
        "call, payload",
        [
            (lambda b: b.ls_files(REPO), {"files": ["a.py"]}),
            (lambda b: b.remote_list(REPO), {"remotes": [{"name": "origin"}]}),
            (lambda b: b.ls_remote("https://example.com/r.git"), {"reachable": True}),
        ],
    )
    def test_structured_tool_page_is_an_error(
        self, backend, monkeypatch, call, payload
    ):
        _install(monkeypatch, lambda b: _rpc(_content(self._page(payload))))
        with pytest.raises(plugin.GitMcpPaginatedResultError):
            call(backend)

    def test_page_in_a_later_content_chunk_is_an_error(self, backend, monkeypatch):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"reachable": True})},
                {"type": "text", "text": json.dumps({"_pagination": {"page": 1}})},
            ]
        }
        _install(monkeypatch, lambda b: _rpc(result))
        with pytest.raises(plugin.GitMcpPaginatedResultError):
            backend.ls_remote("https://example.com/r.git")

    def test_pagination_error_is_a_backend_error(self):
        assert issubclass(plugin.GitMcpPaginatedResultError, GitMcpBackendError)

    def test_the_word_alone_in_output_is_not_a_page(self, backend, monkeypatch):
        """A commit subject mentioning _pagination is data, not the marker."""
        import base64

        raw = {
            "exit_code": 0,
            "stdout_b64": base64.b64encode(b"fix _pagination bug\n").decode(),
            "stderr": "",
        }
        _install(monkeypatch, lambda b: _rpc(_content(raw)))
        assert backend.run(REPO, ["log"]).stdout == "fix _pagination bug\n"


def _raw_body(
    stdout: bytes,
    exit_code: object = 0,
    stderr: str = "",
    **overrides: object,
) -> bytes:
    trailer = {
        "exit_code": exit_code,
        "stderr": stderr,
        "stdout_bytes": len(stdout),
        "truncated": False,
        "timed_out": False,
    }
    trailer.update(overrides)
    return stdout + b"\x00" + json.dumps(trailer).encode()


def _no_mcp(body):
    raise AssertionError("MCP git_exec used although raw exec answered")


class TestRawExec:
    """``run`` goes through git-mcp's raw exec endpoint, falling back to MCP
    ``git_exec`` only for a git-mcp without it (404) or without its secret
    (503). A partial answer is never accepted."""

    def test_raw_request_and_exact_bytes(self, monkeypatch):
        backend = GitMcpBackend(base_url="http://git-mcp:3000", token="s3cret")
        calls = _install(
            monkeypatch, _no_mcp, raw=lambda b: _raw_body(b"a\x00b\r\nc\n")
        )
        out = backend.run(
            REPO,
            ["blame", "--porcelain", "--", "a.py"],
            timeout=20,
            identity={"author_name": "A"},
        )
        assert (out.returncode, out.stdout) == (0, "a\x00b\nc\n")
        assert out.args == ["git", "blame", "--porcelain", "--", "a.py"]
        (call,) = calls
        assert call["req"].full_url == "http://git-mcp:3000/v1/git/exec/raw"
        assert call["req"].headers["Authorization"] == "Bearer s3cret"
        assert call["timeout"] == 50
        assert call["body"] == {
            "cow_path": REPO,
            "args": ["blame", "--porcelain", "--", "a.py"],
            "timeout_seconds": 20,
            "identity": {"author_name": "A"},
        }

    @pytest.mark.parametrize("given,sent", [(14.2, 15), (0.3, 1), (30, 30)])
    def test_a_fractional_timeout_rounds_up(self, backend, monkeypatch, given, sent):
        calls = _install(monkeypatch, _no_mcp, raw=lambda b: _raw_body(b""))
        backend.run(REPO, ["log"], timeout=given)
        assert calls[0]["body"]["timeout_seconds"] == sent

    def test_git_failure_is_returned_not_raised(self, backend, monkeypatch):
        _install(monkeypatch, _no_mcp, raw=lambda b: _raw_body(b"", 128, "fatal: x"))
        out = backend.run(REPO, ["show", "nope"])
        assert (out.returncode, out.stderr) == (128, "fatal: x")

    @pytest.mark.parametrize("code", [404, 503])
    def test_missing_endpoint_or_secret_falls_back_and_is_remembered(
        self, backend, monkeypatch, code
    ):
        import base64

        raw_calls = []

        def raw(body):
            raw_calls.append(body)
            return _http_error("http://git-mcp:3000/v1/git/exec/raw", code, {})

        mcp = {"exit_code": 0, "stdout_b64": base64.b64encode(b"ok\n").decode()}
        calls = _install(monkeypatch, lambda b: _rpc(_content(mcp)), raw=raw)
        assert backend.run(REPO, ["log"]).stdout == "ok\n"
        assert backend.run(REPO, ["log"]).stdout == "ok\n"
        assert len(raw_calls) == 1  # not asked again within the retry window
        assert len(_tool_calls(calls)) == 2
        monkeypatch.setattr(
            plugin.time, "monotonic", lambda: backend._raw_unavailable_until + 1
        )
        backend.run(REPO, ["log"])
        assert len(raw_calls) == 2  # and asked again after it

    def test_a_token_bearing_backend_falls_back_on_503(self, monkeypatch):
        """The production failure mode: cidx holds a token but git-mcp has no
        shared secret configured (or it was rotated away) -- 503, not 401."""
        import base64

        backend = GitMcpBackend(base_url="http://git-mcp:3000", token="s3cret")
        mcp = {"exit_code": 0, "stdout_b64": base64.b64encode(b"ok\n").decode()}
        calls = _install(
            monkeypatch,
            lambda b: _rpc(_content(mcp)),
            raw=lambda b: _http_error(
                "http://git-mcp:3000/v1/git/exec/raw", 503, {"message": "no secret"}
            ),
        )
        assert backend.run(REPO, ["log"]).stdout == "ok\n"
        assert calls[0]["req"].headers["Authorization"] == "Bearer s3cret"
        assert calls[-1]["req"].headers["Authorization"] == "Bearer s3cret"

    @pytest.mark.parametrize(
        "code,error",
        [
            (400, GitMcpBackendError),
            (401, GitMcpBackendError),
            (504, plugin.GitMcpTimeoutError),
        ],
    )
    def test_a_refusal_raises_and_never_falls_back(
        self, backend, monkeypatch, code, error
    ):
        _install(
            monkeypatch,
            _no_mcp,
            raw=lambda b: _http_error(
                "http://git-mcp:3000/v1/git/exec/raw",
                code,
                {"message": "git subcommand not allowed"},
            ),
        )
        with pytest.raises(error) as raised:
            backend.run(REPO, ["gc"])
        if code == 400:
            assert raised.value.stderr == "git subcommand not allowed"

    @pytest.mark.parametrize(
        "body,error",
        [
            (b"partial output, connection cut", GitMcpBackendError),
            (b"out\x00{not json", GitMcpBackendError),
            (_raw_body(b"out", None), GitMcpBackendError),
            (_raw_body(b"out", "0"), GitMcpBackendError),
            (_raw_body(b"out", 0, truncated=True), plugin.GitMcpOutputTooLargeError),
            (_raw_body(b"out", None, timed_out=True), plugin.GitMcpTimeoutError),
            (_raw_body(b"out", 0, error="spawn failed"), GitMcpBackendError),
            (_raw_body(b"out", 0, stdout_bytes=10), GitMcpBackendError),
            (_raw_body(b"out", 0, truncated=None), GitMcpBackendError),
        ],
        ids=[
            "no-trailer",
            "bad-trailer",
            "null-exit",
            "string-exit",
            "truncated",
            "timed-out",
            "error",
            "short",
            "no-flag",
        ],
    )
    def test_an_incomplete_answer_is_never_accepted(
        self, backend, monkeypatch, body, error
    ):
        _install(monkeypatch, _no_mcp, raw=lambda b: body)
        with pytest.raises(error):
            backend.run(REPO, ["log"])

    def test_the_split_is_at_the_last_nul(self, backend, monkeypatch):
        stdout = b"a\x00b\x00"
        _install(monkeypatch, _no_mcp, raw=lambda b: _raw_body(stdout))
        assert backend.run(REPO, ["ls-files", "-z"]).stdout == "a\x00b\x00"

    def test_a_raw_timeout_is_a_typed_timeout(self, backend, monkeypatch):
        import socket

        def raw(body):
            return urllib.error.URLError(socket.timeout("timed out"))

        _install(monkeypatch, _no_mcp, raw=raw)
        with pytest.raises(plugin.GitMcpTimeoutError):
            backend.run(REPO, ["log"], timeout=5)

    def test_output_too_large_from_a_byte_tool_is_typed(self, backend, monkeypatch):
        _install(
            monkeypatch,
            lambda b: _rpc(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"code": "OUTPUT_TOO_LARGE", "message": "over 128 MiB"}
                            ),
                        }
                    ],
                }
            ),
        )
        with pytest.raises(plugin.GitMcpOutputTooLargeError):
            backend.log_raw(REPO, "%H")

    def test_typed_tools_never_use_raw(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({"reachable": True})))
        backend.ls_remote("https://example.com/r.git")
        assert [c["req"].full_url for c in calls] == ["http://git-mcp:3000/v1/git/mcp"]


def _auto_page(payload: dict, path: str, kept: int) -> dict:
    """*payload* as @lightspeed/mcp-tools' autoPage cuts it: the list at
    *path* sliced to *kept* items, the scalars untouched, a ``_pagination``
    block naming the cut list."""
    full = payload[path]
    return {
        **payload,
        path: full[:kept],
        "_pagination": {
            "truncated": True,
            "totalItems": len(full),
            "returnedItems": kept,
            "offset": 0,
            "nextOffset": kept,
            "arrayKey": path,
            "path": path,
            "hint": "Response exceeded 80000 bytes.",
        },
    }


def _too_large(body) -> str:
    """A byte tool refusing output over the MCP ceiling."""
    return _rpc(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"code": "OUTPUT_TOO_LARGE", "message": "over 80000"}
                    ),
                }
            ],
        }
    )


def _raw_requests(calls):
    return [c["body"] for c in calls if c["req"].full_url.endswith("/v1/git/exec/raw")]


class TestLsRemoteManyRefs:
    """EVO-64616: a remote with ~900 refs (fresca) made git_ls_remote's ref list
    exceed git-mcp's MCP ceiling; the page was refused and cidx reported the
    repo as invalid. Only ``reachable`` is read, and a page cut at ``refs``
    keeps it whole."""

    _REFS = [{"sha": f"{i:040x}", "ref": f"refs/heads/b{i}"} for i in range(919)]

    def test_page_cut_at_refs_answers_reachable(self, backend, monkeypatch):
        page = _auto_page(
            {"reachable": True, "ref_count": 919, "refs": self._REFS}, "refs", 600
        )
        calls = _install(monkeypatch, lambda b: _rpc(_content(page)))
        assert backend.ls_remote("https://example.com/fresca.git") is True
        assert _raw_requests(calls) == []

    def test_page_cut_at_refs_still_reports_unreachable(self, backend, monkeypatch):
        page = _auto_page({"reachable": False, "refs": [{"ref": "x"}]}, "refs", 1)
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        assert backend.ls_remote("https://example.com/r.git") is False

    def test_page_cut_elsewhere_is_an_error(self, backend, monkeypatch):
        page = _auto_page(
            {"reachable": True, "refs": [], "other": [1, 2, 3]}, "other", 1
        )
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        with pytest.raises(plugin.GitMcpPaginatedResultError):
            backend.ls_remote("https://example.com/r.git")

    def test_page_without_reachable_is_an_error(self, backend, monkeypatch):
        page = _auto_page({"refs": [{"ref": "a"}, {"ref": "b"}]}, "refs", 1)
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        with pytest.raises(GitMcpBackendError, match="reachable"):
            backend.ls_remote("https://example.com/r.git")

    def test_other_tools_never_accept_a_page_cut_at_refs(self, backend, monkeypatch):
        page = _auto_page({"remotes": [], "refs": [1, 2]}, "refs", 1)
        _install(monkeypatch, lambda b: _rpc(_content(page)))
        with pytest.raises(plugin.GitMcpPaginatedResultError):
            backend.remote_list(REPO)


class TestLargeOutputThroughRawExec:
    """A read whose MCP answer is over the ceiling (a page, or a byte tool's
    OUTPUT_TOO_LARGE) re-runs the same git command through raw exec: the whole
    output, or an error -- never the page."""

    @staticmethod
    def _paged(key: str):
        payload = {key: [f"p{i}" for i in range(5)]}
        return lambda b: _rpc(_content(_auto_page(payload, key, 2)))

    def test_ls_files(self, backend, monkeypatch):
        calls = _install(
            monkeypatch,
            self._paged("files"),
            raw=lambda b: _raw_body(b"a.py\nsrc/b.py\n"),
        )
        assert backend.ls_files(REPO) == ["a.py", "src/b.py"]
        (req,) = _raw_requests(calls)
        assert req["cow_path"] == REPO
        assert req["args"] == ["ls-files"]

    def test_diff_name_only_keeps_nul_delimited_paths(self, backend, monkeypatch):
        calls = _install(
            monkeypatch,
            self._paged("files"),
            raw=lambda b: _raw_body("a b.py\x00ü.py\x00".encode()),
        )
        assert backend.diff_name_only(REPO, "A..B") == ["a b.py", "ü.py"]
        assert _raw_requests(calls)[0]["args"] == [
            "diff",
            "--name-only",
            "-z",
            "A..B",
        ]

    @pytest.mark.parametrize(
        "kwargs, scope",
        [({}, []), ({"remote": True}, ["-r"]), ({"all_refs": True}, ["-a"])],
    )
    def test_list_branches(self, backend, monkeypatch, kwargs, scope):
        calls = _install(
            monkeypatch,
            self._paged("branches"),
            raw=lambda b: _raw_body(b"main\norigin/dev\n\n"),
        )
        assert backend.list_branches(REPO, **kwargs) == ["main", "origin/dev"]
        assert _raw_requests(calls)[0]["args"] == [
            "branch",
            *scope,
            "--format=%(refname:short)",
        ]

    def test_status_porcelain_is_gits_own(self, backend, monkeypatch):
        calls = _install(
            monkeypatch,
            self._paged("untracked"),
            raw=lambda b: _raw_body(b" M a.py\n?? c.py\n"),
        )
        assert backend.status_porcelain(REPO) == " M a.py\n?? c.py\n"
        assert _raw_requests(calls)[0]["args"] == ["status", "--porcelain"]

    def test_for_each_ref_too_large_returns_exact_bytes(self, backend, monkeypatch):
        listing = b"refs/heads/a\x00abc\n" * 3
        calls = _install(monkeypatch, _too_large, raw=lambda b: _raw_body(listing))
        out = backend.for_each_ref(
            REPO,
            "%(refname)%00%(objectname)",
            ["refs/heads", "refs/remotes/origin"],
            sort="-committerdate",
            count=5,
            timeout=10,
        )
        assert out == listing
        req = _raw_requests(calls)[0]
        assert req["args"] == [
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
            "--sort=-committerdate",
            "--count=5",
            "--",
            "refs/heads",
            "refs/remotes/origin",
        ]
        assert req["timeout_seconds"] == 10

    def test_log_raw_too_large_returns_exact_bytes(self, backend, monkeypatch):
        calls = _install(
            monkeypatch, _too_large, raw=lambda b: _raw_body(b"c1\r\nc2\n")
        )
        out = backend.log_raw(
            REPO,
            "%H",
            ["main"],
            all_refs=True,
            since="2020-01-01",
            skip=4,
            max_count=50,
        )
        assert out == b"c1\r\nc2\n"
        assert _raw_requests(calls)[0]["args"] == [
            "log",
            "--format=%H",
            "--all",
            "--since=2020-01-01",
            "--skip=4",
            "--max-count=50",
            "main",
            "--",
        ]

    def test_small_results_never_use_raw(self, backend, monkeypatch):
        calls = _install(monkeypatch, lambda b: _rpc(_content({"files": ["a"]})))
        assert backend.ls_files(REPO) == ["a"]
        assert _raw_requests(calls) == []

    def test_truncated_raw_output_is_an_error(self, backend, monkeypatch):
        _install(
            monkeypatch,
            self._paged("files"),
            raw=lambda b: _raw_body(b"a.py\n", truncated=True),
        )
        with pytest.raises(plugin.GitMcpOutputTooLargeError):
            backend.ls_files(REPO)

    def test_git_failure_raises_like_check_true(self, backend, monkeypatch):
        _install(
            monkeypatch,
            _too_large,
            raw=lambda b: _raw_body(b"", exit_code=128, stderr="fatal: bad ref"),
        )
        with pytest.raises(GitMcpBackendError) as exc:
            backend.for_each_ref(REPO, "%(refname)")
        assert exc.value.returncode == 128
        assert exc.value.stderr == "fatal: bad ref"

    def test_no_raw_endpoint_and_mcp_too_large_is_an_error(self, backend, monkeypatch):
        # A git-mcp without raw exec (404) falls back to MCP git_exec, which
        # refuses the same oversized output: an error, never the first page.
        def responder(body):
            if body["params"]["name"] == "git_exec":
                return _too_large(body)
            return self._paged("files")(body)

        _install(monkeypatch, responder)
        with pytest.raises(plugin.GitMcpOutputTooLargeError):
            backend.ls_files(REPO)
