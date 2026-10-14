"""Golden git history on a pod without the repository: git-mcp ``git_exec``.

With ``content_backend: delegated`` the golden history tools that git-mcp has
no typed operation for -- blame, file history, commit-message and pickaxe
search, diffs, path/author/until-filtered logs, commit stats and patches -- run
the SAME argv they run locally through git-mcp ``git_exec``, read-only.

The "git-mcp" here is the real ``cidx_git_backend`` plugin over an emulated
HTTP endpoint that runs real git the way git-mcp does (raw stdout bytes,
base64, git's ``GIT_*`` environment stripped). The pod path does not exist, and
local git is forbidden in the services under test, so only the seam can answer.

Pinned:

* parity -- byte-identical answers locally and through the seam, on a real
  repository with renames, binary files, unicode names/contents/messages,
  CRLF, a merge and commits in several timezones;
* local default -- the local path calls ``run_git_command`` exactly as before;
* read-only -- an allowlist of subcommands and whole options (git accepts
  abbreviated long options, so a denylist leaks) refused before any request;
* fail-loud -- a backend failure is an error, never "no commits"/"not found";
* multi-tenancy -- access is checked before any backend call.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import subprocess
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos import git_operations as golden_git
from code_indexer.global_repos.git_operations import GitOperationsService as Golden
from code_indexer.server.services import git_operations_service as user_git_module
from code_indexer.server.services.git_tools_seam import (
    GitToolsUnavailableError,
    ReadOnlyGitViolation,
    require_read_only_git,
)
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring

_PLUGIN = (
    Path(__file__).resolve().parents[4]
    / "plugins"
    / "cidx_git_backend"
    / "cidx_git_backend"
    / "__init__.py"
)


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cidx_git_backend_seam", _PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()

# ── a repository with the shapes git output varies on ──────────────────────


def _git(repo: Path, *args: str, when: Optional[str] = None, who: str = "Ada") -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=who,
        GIT_AUTHOR_EMAIL=f"{who.lower()}@example.com",
        GIT_COMMITTER_NAME=who,
        GIT_COMMITTER_EMAIL=f"{who.lower()}@example.com",
    )
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


def _commit(repo: Path, message: str, when: str, who: str = "Ada") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when, who=who)


def build_history_repo(root: Path) -> Path:
    """A repository at *root* with renames, binary files, unicode, CRLF, a
    merge, several timezones and an undecodable (latin-1) branch."""
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    (root / "bin.dat").write_bytes(b"\x00\x01\xff\xfe binary\n")
    (root / "ünï cödé.txt").write_text("grüße – 日本語\n")
    (root / "crlf.txt").write_bytes(b"x\r\ny\r\n")
    _commit(root, "first", "2024-01-02T03:04:05+0000")
    _git(root, "mv", "a.txt", "renamed.txt")
    (root / "renamed.txt").write_text("one\ntwo\nthree\nfour\n")
    (root / "bin.dat").write_bytes(b"\x00\x02\xff\xfe binary v2\n")
    _commit(
        root,
        "rename a -> renamed\n\nbody line\nsecond body",
        "2024-02-03T04:05:06+0530",
    )
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "feature.txt").write_text("the needle is here\n")
    _commit(root, "Fix ✓ émoji 🚀 needle", "2024-03-04T05:06:07-0800", who="Bob")
    _git(root, "checkout", "-q", "main")
    (root / "crlf.txt").write_bytes(b"x\r\nz\r\n")
    (root / "renamed.txt").write_text("zero\none\ntwo\nthree\nfour\n")
    _commit(
        root, 'main: subject with "quotes" and \\ backslash', "2024-04-05T06:07:08+0200"
    )
    _git(
        root,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge feature\n\nmulti-line merge body",
        "feature",
        when="2024-05-06T07:08:09-0500",
    )
    _git(root, "checkout", "-q", "-b", "latin")
    (root / "latin.txt").write_bytes(b"caf\xe9\n")
    _commit(root, "latin-1 content", "2024-06-07T08:09:10+0900")
    _git(root, "checkout", "-q", "main")
    return root


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_history_repo(tmp_path_factory.mktemp("golden") / "repo")


# ── git-mcp: the real plugin over an emulated endpoint ─────────────────────


class GitMcp:
    """git-mcp's ``git_exec`` over HTTP: real git at the mapped path, raw bytes."""

    def __init__(self, pod: Path, real: Path) -> None:
        self.pod, self.real = str(pod), str(real)
        self.calls: List[Dict[str, Any]] = []
        self.unreachable = False
        self.timed_out = False

    def urlopen(self, req: Any, timeout: Any = None) -> Any:
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append(body["params"])
        if self.unreachable:
            raise urllib.error.URLError("git-mcp unreachable")
        name, args = body["params"]["name"], body["params"]["arguments"]
        assert args["cow_path"] == self.pod, args["cow_path"]
        if self.timed_out:
            return self._reply(body, self._error("git blame timed out after 30s"))
        if name == "git_exec":
            proc = self._git(args["args"])
            payload: Dict[str, Any] = self._raw(proc)
        elif name == "git_log_raw":  # !188's typed log (unfiltered history)
            argv = ["log", f"--format={args['format']}"]
            argv += [f"--since={args['since']}"] if args.get("since") else []
            argv += [f"--skip={args['skip']}"] if args.get("skip") else []
            argv += [f"-n{args['max_count']}"] if args.get("max_count") else []
            payload = self._raw(self._git(argv + list(args.get("revs") or [])))
        elif name == "git_show_objects":  # !188's typed `git show <rev>:<path>`
            payload = {
                "objects": [
                    self._raw(self._git(["show", f"{o['rev']}:{o['path']}"]))
                    for o in args["specs"]
                ]
            }
        elif name == "git_rev_parse":
            proc = self._git(["rev-parse", *args["args"]])
            if proc.returncode != 0:
                return self._reply(body, self._error(proc.stderr.decode()))
            payload = {"output": proc.stdout.decode()}
        else:
            raise AssertionError(f"unexpected git-mcp tool {name}")
        text = json.dumps(payload)
        return self._reply(body, {"content": [{"type": "text", "text": text}]})

    def _git(self, argv: List[str]) -> "subprocess.CompletedProcess[bytes]":
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *argv],
            cwd=self.real,
            capture_output=True,
            env=env,
        )

    @staticmethod
    def _raw(proc: "subprocess.CompletedProcess[bytes]") -> Dict[str, Any]:
        return {
            "exit_code": proc.returncode,
            "stdout_b64": base64.b64encode(proc.stdout).decode(),
            "stderr": proc.stderr.decode("utf-8", "replace"),
        }

    @staticmethod
    def _error(text: str) -> Dict[str, Any]:
        return {"isError": True, "content": [{"type": "text", "text": text}]}

    @staticmethod
    def _reply(body: Dict[str, Any], result: Dict[str, Any]) -> "_Response":
        raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return _Response(raw.encode())


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


@pytest.fixture
def pod(tmp_path: Path) -> Path:
    path = tmp_path / "pod" / "golden-repos" / "repo"
    assert not path.exists()
    return path


@pytest.fixture
def git_mcp(repo: Path, pod: Path, monkeypatch: pytest.MonkeyPatch) -> GitMcp:
    server = GitMcp(pod, repo)
    monkeypatch.setattr(plugin.urllib.request, "urlopen", server.urlopen)
    return server


@pytest.fixture
def backend(git_mcp: GitMcp) -> Any:
    return plugin.GitMcpBackend(base_url="http://git-mcp.test")


def _forbid_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"local git ran: {args[:1]}")

    monkeypatch.setattr(golden_git, "run_git_command", forbidden)
    monkeypatch.setattr(user_git_module, "run_git_command", forbidden)


def _timeless(result: Any) -> Any:
    if hasattr(result, "search_time_ms"):
        result.search_time_ms = 0.0
    return result


# ── parity: the golden history service ─────────────────────────────────────

GOLDEN_CALLS = [
    lambda s: s.get_blame("renamed.txt"),
    lambda s: s.get_blame("renamed.txt", start_line=2, end_line=4),
    lambda s: s.get_blame("renamed.txt", start_line=3),
    lambda s: s.get_blame("renamed.txt", revision="HEAD~1"),
    lambda s: s.get_blame("ünï cödé.txt"),
    lambda s: s.get_blame("crlf.txt"),
    lambda s: s.get_file_history("renamed.txt"),
    lambda s: s.get_file_history("renamed.txt", follow_renames=False),
    lambda s: s.get_file_history("renamed.txt", limit=1),
    lambda s: s.get_file_history("bin.dat"),
    lambda s: s.search_commits("needle"),
    lambda s: s.search_commits("F.x|merge", is_regex=True),
    lambda s: s.search_commits("e", author="Bob"),
    lambda s: s.search_commits("e", since="2024-02-01", until="2024-04-30"),
    lambda s: s.search_commits("no such text anywhere"),
    lambda s: s.search_diffs(search_string="needle"),
    lambda s: s.search_diffs(search_pattern="ne+dle"),
    lambda s: s.search_diffs(search_string="four", path="renamed.txt"),
    lambda s: s.search_diffs(search_string="one", since="2024-01-10"),
    lambda s: s.get_diff("HEAD~2", "HEAD"),
    lambda s: s.get_diff("HEAD~3", "HEAD", stat_only=True),
    lambda s: s.get_diff("HEAD~3", "HEAD", path="crlf.txt", context_lines=0),
    lambda s: s.get_diff("HEAD~3"),
    lambda s: s.get_log(path="renamed.txt"),
    lambda s: s.get_log(author="Bob"),
    lambda s: s.get_log(until="2024-03-01"),
    lambda s: s.get_log(branch="feature", path="feature.txt", limit=1),
    lambda s: s.show_commit("HEAD"),
    lambda s: s.show_commit("HEAD", include_diff=True),
    lambda s: s.show_commit("HEAD~1", include_stats=True, include_diff=True),
    lambda s: s.show_commit("HEAD^2", include_diff=True),
    lambda s: s.show_commit(":/rename", include_stats=True, include_diff=True),
]


@pytest.mark.parametrize("call", GOLDEN_CALLS)
def test_golden_history_matches_local_byte_for_byte(
    repo: Path,
    pod: Path,
    backend: Any,
    git_mcp: GitMcp,
    monkeypatch: pytest.MonkeyPatch,
    call: Any,
) -> None:
    expected = _timeless(call(Golden(repo)))
    _forbid_local_git(monkeypatch)
    actual = _timeless(call(Golden(pod, git_backend=backend)))
    assert actual == expected
    assert git_mcp.calls, "the seam answered"


def test_the_fixture_exercises_what_it_claims(repo: Path) -> None:
    service = Golden(repo)
    log = service.get_log(limit=20)
    assert {c.author_date[-6:] for c in log.commits} >= {"+05:30", "-08:00", "+02:00"}
    merge = service.show_commit("HEAD", include_stats=False)
    assert len(merge.parents) == 2
    renamed = service.show_commit(":/rename", include_stats=True)
    assert {s.status for s in renamed.stats or []} >= {"renamed"}
    binary = [
        f for f in service.get_diff("HEAD~3", "HEAD").files if f.path == "bin.dat"
    ]
    assert binary and (binary[0].insertions, binary[0].deletions) == (0, 0)
    assert service.get_blame("crlf.txt").lines[0].content == "x"


def test_undecodable_output_raises_as_local_does(
    repo: Path, pod: Path, backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(UnicodeDecodeError) as local:
        Golden(repo).get_diff("main", "latin")
    _forbid_local_git(monkeypatch)
    with pytest.raises(UnicodeDecodeError) as remote:
        Golden(pod, git_backend=backend).get_diff("main", "latin")
    assert str(remote.value) == str(local.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_blame("missing.txt"),
        lambda s: s.get_blame("renamed.txt", revision="no-such-rev"),
        lambda s: s.show_commit("no-such-rev"),
    ],
)
def test_golden_git_errors_match_local(
    repo: Path, pod: Path, backend: Any, monkeypatch: pytest.MonkeyPatch, call: Any
) -> None:
    with pytest.raises(ValueError) as local:
        call(Golden(repo))
    _forbid_local_git(monkeypatch)
    with pytest.raises(ValueError) as remote:
        call(Golden(pod, git_backend=backend))
    assert str(remote.value) == str(local.value)


def test_golden_blame_timeout_is_the_local_timeout_result(
    pod: Path, backend: Any, git_mcp: GitMcp
) -> None:
    git_mcp.timed_out = True
    result = Golden(pod, git_backend=backend).get_blame("renamed.txt")
    assert isinstance(result, golden_git.BlameErrorResult)
    assert result.error == "Git blame timed out after 30 seconds"
    assert git_mcp.calls[-1]["arguments"]["timeout_seconds"] == 30


# ── parity: the server git_log / git_diff on a golden repository ───────────


@pytest.fixture
def user_service() -> Any:
    from code_indexer.server.utils.config_manager import (
        ApiLimitsConfig,
        GitTimeoutsConfig,
    )

    service = user_git_module.GitOperationsService()
    service._git_timeouts = GitTimeoutsConfig(git_local_timeout=30)
    service._api_limits = ApiLimitsConfig(max_log_commits=500)
    return service


USER_CALLS = [
    lambda s, p, kw: s.git_log(p, since_date="2024-02-01", **kw),
    lambda s, p, kw: s.git_log(p, until="2024-04-01", author="Ada", **kw),
    lambda s, p, kw: s.git_log(p, path="renamed.txt", limit=1, offset=1, **kw),
    lambda s, p, kw: s.git_log(p, branch="feature", author="Bob", **kw),
    lambda s, p, kw: s.git_diff(p, from_revision="HEAD~2", to_revision="HEAD", **kw),
    lambda s, p, kw: s.git_diff(p, from_revision="HEAD~3", stat_only=True, **kw),
    lambda s, p, kw: s.git_diff(
        p, from_revision="HEAD~3", path="ünï cödé.txt", context_lines=1, **kw
    ),
    lambda s, p, kw: s.git_diff(p, from_revision="HEAD~3", limit=3, offset=2, **kw),
]


@pytest.mark.parametrize("call", USER_CALLS)
def test_server_git_log_and_diff_match_local(
    repo: Path,
    pod: Path,
    backend: Any,
    user_service: Any,
    monkeypatch: pytest.MonkeyPatch,
    call: Any,
) -> None:
    expected = call(user_service, repo, {})
    _forbid_local_git(monkeypatch)
    assert call(user_service, pod, {"git_backend": backend}) == expected


def test_local_default_calls_run_git_command_exactly_as_before(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[Any] = []
    real = golden_git.run_git_command

    def spy(cmd: Any, **kwargs: Any) -> Any:
        seen.append((list(cmd), kwargs))
        return real(cmd, **kwargs)

    monkeypatch.setattr(golden_git, "run_git_command", spy)
    service = Golden(repo)
    service.get_blame("renamed.txt")
    service.search_commits("needle")
    assert seen[0] == (
        ["git", "blame", "--porcelain", "--", "renamed.txt"],
        {"cwd": repo, "check": True, "timeout": golden_git.BLAME_TIMEOUT_SECONDS},
    )
    assert seen[1][1] == {"cwd": repo, "check": True}


# ── fail loud ──────────────────────────────────────────────────────────────

FAIL_CALLS = [
    lambda s: s.get_blame("renamed.txt"),
    lambda s: s.get_file_history("renamed.txt"),
    lambda s: s.search_commits("needle"),
    lambda s: s.search_diffs(search_string="needle"),
    lambda s: s.get_diff("HEAD~1", "HEAD"),
    lambda s: s.get_log(path="renamed.txt"),
]


@pytest.mark.parametrize("call", FAIL_CALLS)
def test_a_backend_failure_is_an_error_not_an_empty_answer(
    pod: Path, backend: Any, git_mcp: GitMcp, call: Any
) -> None:
    git_mcp.unreachable = True
    with pytest.raises(GitToolsUnavailableError, match="git-mcp could not run"):
        call(Golden(pod, git_backend=backend))


def test_commit_stats_failure_is_an_error_not_empty_stats(
    pod: Path, backend: Any, git_mcp: GitMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Golden(pod, git_backend=backend)
    monkeypatch.setattr(service, "_backend_log", lambda *a, **k: "")
    git_mcp.unreachable = True
    with pytest.raises(GitToolsUnavailableError):
        service._get_commit_stats("HEAD")


def test_server_git_log_failure_is_an_error(
    pod: Path, backend: Any, git_mcp: GitMcp, user_service: Any
) -> None:
    git_mcp.unreachable = True
    with pytest.raises(GitToolsUnavailableError):
        user_service.git_log(pod, author="Ada", git_backend=backend)
    with pytest.raises(GitToolsUnavailableError):
        user_service.git_diff(pod, from_revision="HEAD~1", git_backend=backend)


# ── read-only: an allowlist, enforced before any request ───────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "commit", "-m", "x"],
        ["git", "push", "origin"],
        ["git", "checkout", "main"],
        ["git", "reset", "--hard"],
        ["git", "config", "--get", "user.name"],
        ["git", "-c", "core.pager=sh", "log"],
        ["git", "blame", "--contents", "/etc/passwd", "--", "a"],
        ["git", "blame", "--cont=/etc/passwd", "--", "a"],
        ["git", "blame", "-S/etc/passwd", "--", "a"],
        ["git", "blame", "--ignore-revs-file=/etc/passwd", "--", "a"],
        ["git", "log", "--output=/tmp/x"],
        ["git", "log", "--outp=/tmp/x"],
        ["git", "log", "-p", "--ext-diff"],
        ["git", "show", "--textconv", "HEAD"],
        ["git", "diff", "-O/etc/passwd"],
        ["git", "diff", "--no-index", "/etc/passwd", "a"],
        ["git", "log", "--exec-path"],
        ["git", "rev-parse", "--git-path", "x"],
        ["git", "log", "--git-dir=/tmp"],
        ["git", "log", "-"],
        ["git", "log", "x\0y"],
        ["git", "blame", "-L"],
        ["git"],
    ],
)
def test_anything_but_a_read_only_history_argv_is_refused(cmd: List[str]) -> None:
    with pytest.raises(ReadOnlyGitViolation):
        require_read_only_git(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "log", "--format=%H", "-51", "--author=--output=/x", "--", "-p"],
        ["git", "log", "-S--output=x", "-G.*", "-n5", "--skip=2", "main"],
        ["git", "rev-list", "--count", "HEAD", "--author", "--output=x"],
        ["git", "diff", "-U0", "--stat", "--end-of-options", "a..b", "--", "x"],
        ["git", "blame", "--porcelain", "-L", "1,5", "HEAD", "--", "f"],
        ["git", "show", "abc", "--numstat", "-M", "--format="],
    ],
)
def test_the_history_tools_argvs_are_allowed(cmd: List[str]) -> None:
    require_read_only_git(cmd)


def test_a_refused_argv_never_reaches_git_mcp(
    pod: Path, backend: Any, git_mcp: GitMcp
) -> None:
    from code_indexer.server.services.git_tools_seam import GoldenGitReader

    with pytest.raises(ReadOnlyGitViolation):
        GoldenGitReader(backend).run(["git", "commit", "-m", "x"], pod)
    assert git_mcp.calls == []


# ── MCP handlers: seam resolution and multi-tenancy ────────────────────────


@pytest.fixture
def delegated(monkeypatch: pytest.MonkeyPatch, backend: Any, pod: Path) -> Any:
    from code_indexer.server.mcp.handlers import _legacy

    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: backend
    )
    monkeypatch.setattr(_legacy, "_get_golden_repos_dir", lambda: str(pod.parent))
    monkeypatch.setattr(
        _legacy, "_get_global_repo", lambda alias: {"repo_url": "git@x:r.git"}
    )
    monkeypatch.setattr(_legacy, "_resolve_repo_path", lambda alias, d: str(pod))
    monkeypatch.setattr(
        _legacy,
        "_get_access_filtering_service",
        lambda: SimpleNamespace(get_accessible_repos=lambda user: {"repo"}),
    )
    from code_indexer.server.mcp.handlers import git_read

    return git_read


def _payload(response: Any) -> Any:
    return json.loads(response["content"][0]["text"])


def test_git_diff_handler_reads_a_golden_repo_read_only_through_the_seam(
    delegated: Any, git_mcp: GitMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services import git_tools_seam

    checked: List[List[str]] = []
    real_check = git_tools_seam.require_read_only_git

    def check(cmd: Any) -> None:
        checked.append(list(cmd))
        real_check(cmd)

    monkeypatch.setattr(git_tools_seam, "require_read_only_git", check)
    _forbid_local_git(monkeypatch)
    user = SimpleNamespace(username="u")
    data = _payload(
        delegated.git_diff(
            {"repository_alias": "repo-global", "from_revision": "HEAD~1"}, user
        )
    )
    assert data["success"] is True, data
    assert "diff --git" in data["diff_text"]
    # The golden read-only reader ran it -- never the activated-copy funnel,
    # which lets writes through.
    assert checked == [["git", "diff", "--end-of-options", "HEAD~1"]]


@pytest.mark.parametrize(
    "tool, args",
    [
        ("git_diff", {"from_revision": "HEAD~1"}),
        ("handle_git_blame", {"path": "renamed.txt"}),
        ("handle_git_file_history", {"path": "renamed.txt"}),
        ("handle_git_search_commits", {"query": "needle"}),
        ("handle_git_search_diffs", {"search_string": "needle"}),
    ],
)
def test_an_invisible_golden_repo_is_not_found_before_any_backend_call(
    delegated: Any, git_mcp: GitMcp, tool: str, args: Dict[str, Any]
) -> None:
    user = SimpleNamespace(username="u")
    data = _payload(
        getattr(delegated, tool)({"repository_alias": "other-global", **args}, user)
    )
    assert data["success"] is False
    assert data["error"] == "Repository 'other-global' not found."
    assert git_mcp.calls == []


@pytest.mark.parametrize(
    "tool, args",
    [
        ("handle_git_blame", {"path": "renamed.txt"}),
        ("handle_git_file_history", {"path": "renamed.txt"}),
        ("handle_git_search_commits", {"query": "needle"}),
        ("handle_git_search_diffs", {"search_string": "needle"}),
        ("git_diff", {"from_revision": "HEAD~1"}),
    ],
)
def test_handlers_report_a_backend_failure_as_an_error(
    delegated: Any, git_mcp: GitMcp, tool: str, args: Dict[str, Any]
) -> None:
    git_mcp.unreachable = True
    user = SimpleNamespace(username="u")
    data = _payload(
        getattr(delegated, tool)({"repository_alias": "repo-global", **args}, user)
    )
    assert data["success"] is False
    assert "git-mcp" in data["error"]
