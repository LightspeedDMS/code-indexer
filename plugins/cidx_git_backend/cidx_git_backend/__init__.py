"""
Delegate cidx's internal git operations to the git-mcp spoke (EVO-64616 Phase 3).

cidx's ``GitBackend`` seam (EVO-64678) routes every internal git call through one
object. Its default ``LocalGitBackend`` shells out to git in-process. In the EKS
deployment cidx must do NO git itself — git (like cloning) belongs in a spoke that
owns the credentials and the shared volume. This plugin supplies a ``GitBackend``
that forwards each op to **git-mcp**, exactly as the clone seam forwards cloning to
workspace-mcp.

Addressing is BY PATH, matching the clone seam: git-mcp's tools accept a
``cow_path`` (EVO-64616) that runs the op directly on that path on the shared
FlexVol — cidx passes its own ``repo_path``, no workspace_id it never registered.

Transport: JSON-RPC ``tools/call`` over ``POST {CIDX_GIT_MCP_URL}/v1/git/mcp``,
stdlib ``urllib`` only (no third-party deps in the cidx image). git-mcp returns a
structured JSON result; a few GitBackend methods hand cidx RAW git output it parses
(``status_porcelain``, ``remote_list``, ``diff_name_only``, ``list_branches``,
``show_ref``), so those are reconstructed faithfully here.

Configuration (env)
-------------------
CIDX_GIT_MCP_URL     git-mcp base URL.
                     Default: http://git-mcp.agent-orchestrator.svc.cluster.local:3000
CIDX_GIT_MCP_TOKEN   Bearer token (sent even though git-mcp's LsAuthGuard is a
                     no-op today). Optional.
CIDX_GIT_MCP_TIMEOUT Per-request HTTP timeout seconds (default: 120).

Enable with the server config ``git_backend='delegated'`` (installing the package
registers it on the ``code_indexer.git_backend`` entry point;
``CIDX_GIT_BACKEND=cidx_git_backend:make_git_backend`` is an equivalent override).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

_DEFAULT_URL = "http://git-mcp.agent-orchestrator.svc.cluster.local:3000"

#: Items git-mcp accepts in one git_diff_commits / git_show_objects call. A
#: larger request is split into calls of this size -- still a batch per call.
_MAX_HISTORY_BATCH = 256


class GitMcpBackendError(subprocess.CalledProcessError):
    """A git-mcp call failed (transport, JSON-RPC, or tool error).

    Subclasses ``subprocess.CalledProcessError`` — NOT a bare ``RuntimeError`` —
    because the ``GitBackend`` contract (``storage/shared/git_backend.py``)
    guarantees that a failed op raises ``CalledProcessError`` "whose ``stderr``
    callers already use", and ``LocalGitBackend`` honours that via ``check=True``.
    ~30 cidx call sites do ``except subprocess.CalledProcessError`` and read
    ``e.stderr`` to drive recovery (remote-already-exists fallback, divergent-branch
    auto-recovery, fetch-error classification). A ``RuntimeError`` would miss every
    one of those handlers and escape uncaught, so the delegated backend must raise
    the same type — with ``stderr`` populated from git-mcp's error text so the
    substring checks (`"already exists"`, …) still fire.
    """

    def __init__(
        self,
        message: str,
        *,
        stderr: Optional[str] = None,
        returncode: int = 1,
        cmd: str = "git-mcp",
    ) -> None:
        super().__init__(
            returncode,
            cmd,
            output=None,
            stderr=stderr if stderr is not None else message,
        )
        self.message = message

    def __str__(self) -> str:
        return self.message


class GitMcpBackend:
    """A ``GitBackend`` that forwards every op to git-mcp, addressed by cow_path.

    Implements the same method surface as ``LocalGitBackend`` (structural typing —
    cidx's ``GitBackend`` is a ``typing.Protocol``), so it needs no import from cidx
    and stays a stdlib-only plugin.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        base = (base_url or os.environ.get("CIDX_GIT_MCP_URL") or _DEFAULT_URL).rstrip(
            "/"
        )
        self._url = base + "/v1/git/mcp"
        self._token = token or os.environ.get("CIDX_GIT_MCP_TOKEN") or ""
        env_timeout = os.environ.get("CIDX_GIT_MCP_TIMEOUT")
        self._timeout = timeout or (float(env_timeout) if env_timeout else 120.0)
        self._id = 0
        self._id_lock = threading.Lock()

    # ── capabilities ───────────────────────────────────────────────────────────

    @property
    def owns_credentials(self) -> bool:
        """True: git-mcp holds the git credential (``GIT_CLONE_TOKEN`` → its own
        credential helper) and injects it for every network op, so cidx must NOT
        embed a token in repo URLs and should strip any credential before storing
        one. The PAT then lives ONLY in git-mcp (EVO-64616)."""
        return True

    # ── transport ────────────────────────────────────────────────────────────

    def _call(
        self, tool: str, arguments: Dict[str, Any], timeout: Any = None
    ) -> Dict[str, Any]:
        with self._id_lock:
            self._id += 1
            call_id = self._id
        body = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        http_timeout = timeout if isinstance(timeout, (int, float)) else self._timeout
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            raise GitMcpBackendError(
                f"git-mcp {tool} request to {self._url} failed: {exc}", stderr=str(exc)
            ) from exc
        payload = json.loads(raw) if raw else {}
        if "error" in payload:
            raise GitMcpBackendError(
                f"git-mcp {tool} JSON-RPC error: {payload['error']}",
                stderr=str(payload["error"]),
            )
        result = payload.get("result", {}) or {}
        text = self._text(result)
        if result.get("isError"):
            # git-mcp surfaces the underlying git stderr as the tool's text, so
            # feed it to .stderr — the recovery handlers pattern-match on it.
            raise GitMcpBackendError(f"git-mcp {tool} tool error: {text}", stderr=text)
        return self._parse(tool, text)

    @staticmethod
    def _text(result: Dict[str, Any]) -> str:
        for chunk in result.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                return str(chunk.get("text", ""))
        return ""

    @staticmethod
    def _parse(tool: str, text: str) -> Dict[str, Any]:
        # A 200 with an empty body is legitimate for mutations (they return
        # nothing the backend reads). A 200 with a NON-empty body that is not JSON,
        # however, must NOT degrade to a benign default — a read like
        # status_porcelain / ls_files would then report a dirty repo as clean or an
        # indexed repo as empty, silently corrupting cidx's git-state view. So an
        # unparseable non-empty body fails loud; per-method key validation
        # (``_require``) guards the structured reads against a wrong-shape 200.
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitMcpBackendError(
                f"git-mcp {tool} returned an unparseable body: {text[:200]!r}",
                stderr=text,
            ) from exc
        return parsed if isinstance(parsed, dict) else {"_list": parsed}

    @staticmethod
    def _require(tool: str, result: Dict[str, Any], key: str) -> Any:
        """Return ``result[key]`` or fail loud — a structured read whose expected
        key is absent means git-mcp returned a wrong-shape (but valid-JSON) 200;
        defaulting it would mask a dirty/indexed repo as clean/empty."""
        if key not in result:
            raise GitMcpBackendError(
                f"git-mcp {tool} response missing {key!r} (got keys {sorted(result)})"
            )
        return result[key]

    def _cow(self, repo_path: str, **extra: Any) -> Dict[str, Any]:
        args: Dict[str, Any] = {"cow_path": repo_path}
        args.update({k: v for k, v in extra.items() if v is not None})
        return args

    # ── remote-only ops ──────────────────────────────────────────────────────

    def ls_remote(self, url: str, timeout: Any = None) -> bool:
        r = self._call("git_ls_remote", {"remote_url": url}, timeout)
        return bool(self._require("git_ls_remote", r, "reachable"))

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
        timeout: Any = None,
        retry: bool = False,
    ) -> None:
        self._call(
            "git_clone", self._cow(dest_path, remote_url=url, branch=branch), timeout
        )

    # ── working-tree / index ops (cow_path-addressed) ────────────────────────

    def fetch(
        self,
        repo_path: str,
        remote: str = "origin",
        no_fsync: bool = False,
        timeout: Any = None,
        retry: bool = False,
    ) -> None:
        self._call("git_fetch", self._cow(repo_path, remote=remote), timeout)

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
        timeout: Any = None,
        retry: bool = False,
    ) -> None:
        args = self._cow(repo_path, remote=remote, branch=branch)
        # Map every documented LocalGitBackend pull mode to its git-mcp flag. A
        # mode that matched no branch used to degrade to a plain `git pull` (so
        # 'no-ff' silently fast-forwarded — the opposite of --no-ff); reject an
        # unrecognised mode instead of silently downgrading.
        mode_flags = {
            "rebase": "rebase",
            "ff-only": "ff_only",
            "ff_only": "ff_only",
            "no-ff": "no_ff",
            "no_ff": "no_ff",
        }
        if mode not in ("default", None):
            flag = mode_flags.get(mode)
            if flag is None:
                raise GitMcpBackendError(f"git-mcp pull: unsupported mode {mode!r}")
            args[flag] = True
        self._call("git_pull", args, timeout)

    def merge(self, repo_path: str, ref: str, timeout: Any = None) -> None:
        self._call("git_merge", self._cow(repo_path, branch=ref), timeout)

    def checkout(
        self,
        repo_path: str,
        ref: str,
        create: bool = False,
        force_new: bool = False,
        force: bool = False,
        start_point: Optional[str] = None,
        timeout: Any = None,
    ) -> None:
        # Forward create (-b), force_new (-B), and force (-f) as DISTINCT signals —
        # git-mcp orders them as git does (-f, then -B|-b). Collapsing force_new
        # into create would break cidx's force-switch strategy (checkout of an
        # existing branch with force_new=True must force-reset, not fail as -b).
        self._call(
            "git_checkout",
            self._cow(
                repo_path,
                target=ref,
                create=create or None,
                force_new=force_new or None,
                force=force or None,
                start_point=start_point,
            ),
            timeout,
        )

    def current_branch(self, repo_path: str, timeout: Any = None) -> str:
        r = self._call("git_symbolic_ref", self._cow(repo_path, short=True), timeout)
        return str(self._require("git_symbolic_ref", r, "ref")).strip()

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Any = None,
    ) -> List[str]:
        # git-mcp git_branch_list already returns names in `%(refname:short)` shape
        # (local -> `main`, remote -> `origin/main`), which is the only fmt any cidx
        # caller passes, so that fmt is honoured by the reconstruction below. Any
        # OTHER fmt cannot be reproduced (git-mcp exposes no --format), so fail loud
        # rather than silently return the wrong string shape.
        if fmt not in (None, "%(refname:short)"):
            raise GitMcpBackendError(
                f"git-mcp git_branch_list cannot honour fmt={fmt!r}; only the "
                "default / '%(refname:short)' shape is available"
            )
        result = self._call(
            "git_branch_list",
            self._cow(repo_path, include_remote=bool(remote or all_refs)),
            timeout,
        )
        branches = self._require("git_branch_list", result, "branches")
        names: List[str] = []
        for b in branches:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if (
                all_refs
                or (remote and btype == "remote")
                or (not remote and btype == "local")
            ):
                names.append(str(b.get("name", "")))
        return [n for n in names if n]

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        r = self._call("git_rev_parse", self._cow(repo_path, args=list(args)), timeout)
        return str(self._require("git_rev_parse", r, "output")).strip()

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Any = None
    ) -> int:
        r = self._call(
            "git_rev_list_count", self._cow(repo_path, range=range_expr), timeout
        )
        return int(self._require("git_rev_list_count", r, "count"))

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Any = None
    ) -> str:
        r = self._call("git_symbolic_ref", self._cow(repo_path, short=short), timeout)
        return str(self._require("git_symbolic_ref", r, "ref")).strip()

    def status_porcelain(self, repo_path: str, timeout: Any = None) -> str:
        # git-mcp git_status is structured; reconstruct `git status --porcelain`
        # faithfully enough for cidx (which filters untracked `??` and warns on any
        # remaining TRACKED line).
        r = self._call("git_status", self._cow(repo_path), timeout)
        # Guard against a valid-JSON but wrong-shape 200 (e.g. `{}`) reading as a
        # clean tree: a real git_status always carries these buckets.
        if not any(k in r for k in ("staged", "unstaged", "untracked", "conflicts")):
            raise GitMcpBackendError(
                f"git-mcp git_status returned no status buckets (keys {sorted(r)})"
            )
        lines: List[str] = []
        for e in r.get("staged") or []:
            lines.append(f"M  {e.get('path', '')}")
        for e in r.get("unstaged") or []:
            code = "D" if e.get("status") == "deleted" else "M"
            lines.append(f" {code} {e.get('path', '')}")
        for e in r.get("conflicts") or []:
            lines.append(f"UU {e.get('path', '')}")
        for e in r.get("untracked") or []:
            lines.append(f"?? {e.get('path', '')}")
        return "\n".join(lines)

    def diff_name_only(
        self, repo_path: str, spec: str, timeout: Any = None
    ) -> List[str]:
        # Use git-mcp's name_only mode, which returns git's OWN `git diff
        # --name-only -z` path list. Parsing the changed paths out of a unified
        # diff was lossy — a quoted path (non-ASCII / special chars) has no literal
        # `" b/"` token, so it was dropped and the file left un-reindexed. These
        # paths feed reindex decisions, so a dropped file diverges the index.
        r = self._call(
            "git_diff", self._cow(repo_path, base=spec, name_only=True), timeout
        )
        return list(self._require("git_diff", r, "files"))

    def ls_files(self, repo_path: str, timeout: Any = None) -> List[str]:
        r = self._call("git_ls_files", self._cow(repo_path), timeout)
        return list(self._require("git_ls_files", r, "files"))

    def show_ref(
        self, repo_path: str, *args: str, timeout: Any = None
    ) -> subprocess.CompletedProcess:
        r = self._call("git_show_ref", self._cow(repo_path, args=list(args)), timeout)
        output = str(r.get("output", ""))
        return subprocess.CompletedProcess(
            args=["git", "show-ref", *args],
            returncode=0 if r.get("found") else 1,
            stdout=output,
            stderr="",
        )

    def reset(
        self, repo_path: str, mode: str, target: str, timeout: Any = None
    ) -> None:
        self._call("git_reset", self._cow(repo_path, mode=mode, target=target), timeout)

    def restore(self, repo_path: str, pathspec: str = ".", timeout: Any = None) -> None:
        self._call("git_restore", self._cow(repo_path, pathspec=pathspec), timeout)

    def update_index_refresh(self, repo_path: str, timeout: Any = None) -> None:
        self._call("git_update_index_refresh", self._cow(repo_path), timeout)

    def config_set(
        self, repo_path: str, key: str, value: str, timeout: Any = None
    ) -> None:
        self._call(
            "git_config_set", self._cow(repo_path, key=key, value=value), timeout
        )

    # ── history (cidx temporal indexing and queries) ─────────────────────────
    # git-mcp returns git's own stdout base64-encoded, so these hand cidx the
    # exact bytes a local run would; cidx decodes them as local git output.

    def log_raw(
        self,
        repo_path: str,
        fmt: str,
        revs: Sequence[str] = (),
        *,
        all_refs: bool = False,
        since: Optional[str] = None,
        skip: Optional[int] = None,
        max_count: Optional[int] = None,
        timeout: Any = None,
    ) -> bytes:
        r = self._call(
            "git_log_raw",
            self._cow(
                repo_path,
                format=fmt,
                revs=list(revs) or None,
                all=all_refs or None,
                since=since,
                skip=skip or None,
                max_count=max_count,
            ),
            timeout,
        )
        out = self._raw_output("git_log_raw", r)
        self._check("git_log_raw", out)
        return bytes(out.stdout)

    def diff_commits(
        self,
        repo_path: str,
        pairs: Sequence[Tuple[str, str]],
        context_lines: int,
        timeout: Any = None,
    ) -> List[bytes]:
        diffs: List[bytes] = []
        for batch in self._batches(pairs):
            r = self._call(
                "git_diff_commits",
                self._cow(
                    repo_path,
                    pairs=[{"base": base, "commit": commit} for base, commit in batch],
                    context_lines=context_lines,
                ),
                timeout,
            )
            items = self._items("git_diff_commits", r, "diffs", len(batch))
            for item in items:
                out = self._raw_output("git_diff_commits", item)
                self._check("git_diff_commits", out)
                diffs.append(bytes(out.stdout))
        return diffs

    def show_objects(
        self,
        repo_path: str,
        specs: Sequence[Tuple[str, str]],
        timeout: Any = None,
    ) -> List[subprocess.CompletedProcess]:
        objects: List[subprocess.CompletedProcess] = []
        for batch in self._batches(specs):
            r = self._call(
                "git_show_objects",
                self._cow(
                    repo_path, specs=[{"rev": rev, "path": path} for rev, path in batch]
                ),
                timeout,
            )
            items = self._items("git_show_objects", r, "objects", len(batch))
            objects.extend(self._raw_output("git_show_objects", i) for i in items)
        return objects

    @staticmethod
    def _batches(items: Sequence[Any]) -> List[Sequence[Any]]:
        return [
            items[i : i + _MAX_HISTORY_BATCH]
            for i in range(0, len(items), _MAX_HISTORY_BATCH)
        ]

    def _items(
        self, tool: str, result: Dict[str, Any], key: str, expected: int
    ) -> List[Dict[str, Any]]:
        """``result[key]``: exactly one dict per requested item, or fail loud."""
        items = self._require(tool, result, key)
        if not isinstance(items, list) or len(items) != expected:
            raise GitMcpBackendError(
                f"git-mcp {tool} returned {len(items) if isinstance(items, list) else items!r} "
                f"results for {expected} items"
            )
        return [i if isinstance(i, dict) else {} for i in items]

    def _raw_output(
        self, tool: str, result: Dict[str, Any]
    ) -> subprocess.CompletedProcess:
        """One git process's result: returncode, raw stdout bytes, stderr text."""
        returncode = self._require(tool, result, "exit_code")
        encoded = self._require(tool, result, "stdout_b64")
        try:
            stdout = base64.b64decode(str(encoded), validate=True)
        except ValueError as exc:
            raise GitMcpBackendError(
                f"git-mcp {tool} returned stdout that is not base64"
            ) from exc
        return subprocess.CompletedProcess(
            args=["git-mcp", tool],
            returncode=int(returncode),
            stdout=stdout,
            stderr=str(result.get("stderr", "")),
        )

    @staticmethod
    def _check(tool: str, out: subprocess.CompletedProcess) -> None:
        """Raise as a local ``check=True`` run would, with git's own stderr."""
        if out.returncode != 0:
            raise GitMcpBackendError(
                f"git-mcp {tool}: git exited {out.returncode}: {out.stderr}",
                stderr=out.stderr,
                returncode=out.returncode,
            )

    # ── cidx's git tools on an activated working copy (GitCommandRunner) ─────

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Any = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """``git <args>`` through git-mcp ``git_exec``: git's own output, the
        exit code returned (not raised) exactly as a local ``check=False`` run."""
        seconds = int(timeout) if isinstance(timeout, (int, float)) else None
        result = self._call(
            "git_exec",
            self._cow(
                repo_path,
                args=list(args),
                timeout_seconds=seconds,
                identity=dict(identity) if identity else None,
            ),
            None if seconds is None else seconds + 30,
        )
        out = self._raw_output("git_exec", result)
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=out.returncode,
            stdout=bytes(out.stdout).decode("utf-8", "replace"),
            stderr=out.stderr,
        )

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Any = None,
    ) -> subprocess.CompletedProcess:
        """git-mcp ``git_push`` with git-mcp's credentials; raises on failure."""
        result = self._call(
            "git_push", self._cow(repo_path, remote=remote, branch=branch), timeout
        )
        guidance = result.get("_guidance") or {}
        message = str(guidance.get("message", "")) if isinstance(guidance, dict) else ""
        return subprocess.CompletedProcess(
            args=["git", "push", remote] + ([branch] if branch else []),
            returncode=0,
            stdout=message,
            stderr="",
        )

    # ── remotes ──────────────────────────────────────────────────────────────

    def remote_list(
        self, repo_path: str, verbose: bool = False, timeout: Any = None
    ) -> str:
        result = self._call("git_remote_list", self._cow(repo_path), timeout)
        remotes = self._require("git_remote_list", result, "remotes") or []
        if not verbose:
            return "\n".join(
                sorted({str(r.get("name", "")) for r in remotes if r.get("name")})
            )
        lines: List[str] = []
        for r in remotes:
            name = str(r.get("name", ""))
            lines.append(f"{name}\t{r.get('fetch', '')} (fetch)")
            lines.append(f"{name}\t{r.get('push', '')} (push)")
        return "\n".join(lines)

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> str:
        r = self._call("git_remote_get_url", self._cow(repo_path, name=name), timeout)
        return str(self._require("git_remote_get_url", r, "url")).strip()

    def remote_add(
        self, repo_path: str, name: str, url: str, timeout: Any = None
    ) -> None:
        self._call("git_set_remote", self._cow(repo_path, name=name, url=url), timeout)

    def remote_set_url(
        self, repo_path: str, name: str, url: str, timeout: Any = None
    ) -> None:
        self._call("git_set_remote", self._cow(repo_path, name=name, url=url), timeout)

    def remote_rename(
        self, repo_path: str, old: str, new: str, timeout: Any = None
    ) -> None:
        self._call(
            "git_remote_rename",
            self._cow(repo_path, old_name=old, new_name=new),
            timeout,
        )

    def remote_remove(self, repo_path: str, name: str, timeout: Any = None) -> None:
        self._call("git_remote_remove", self._cow(repo_path, name=name), timeout)


def make_git_backend() -> GitMcpBackend:
    """Factory for the ``code_indexer.git_backend`` seam. Reads config from env."""
    return GitMcpBackend()
