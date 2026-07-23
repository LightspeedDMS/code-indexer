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

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_DEFAULT_URL = "http://git-mcp.agent-orchestrator.svc.cluster.local:3000"


class GitMcpBackendError(RuntimeError):
    """A git-mcp call failed (transport, JSON-RPC, or tool error)."""


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
                f"git-mcp {tool} request to {self._url} failed: {exc}"
            ) from exc
        payload = json.loads(raw) if raw else {}
        if "error" in payload:
            raise GitMcpBackendError(
                f"git-mcp {tool} JSON-RPC error: {payload['error']}"
            )
        result = payload.get("result", {}) or {}
        text = self._text(result)
        if result.get("isError"):
            raise GitMcpBackendError(f"git-mcp {tool} tool error: {text}")
        return self._parse(text)

    @staticmethod
    def _text(result: Dict[str, Any]) -> str:
        for chunk in result.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                return str(chunk.get("text", ""))
        return ""

    @staticmethod
    def _parse(text: str) -> Dict[str, Any]:
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
        return parsed if isinstance(parsed, dict) else {"_list": parsed}

    def _cow(self, repo_path: str, **extra: Any) -> Dict[str, Any]:
        args: Dict[str, Any] = {"cow_path": repo_path}
        args.update({k: v for k, v in extra.items() if v is not None})
        return args

    # ── remote-only ops ──────────────────────────────────────────────────────

    def ls_remote(self, url: str, timeout: Any = None) -> bool:
        return bool(
            self._call("git_ls_remote", {"remote_url": url}, timeout).get("reachable")
        )

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
        if mode == "rebase":
            args["rebase"] = True
        elif mode in ("ff-only", "ff_only"):
            args["ff_only"] = True
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
        self._call(
            "git_checkout",
            self._cow(
                repo_path,
                target=ref,
                create=(create or force_new) or None,
                force=force or None,
                start_point=start_point,
            ),
            timeout,
        )

    def current_branch(self, repo_path: str, timeout: Any = None) -> str:
        return str(
            self._call(
                "git_symbolic_ref", self._cow(repo_path, short=True), timeout
            ).get("ref", "")
        ).strip()

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Any = None,
    ) -> List[str]:
        result = self._call(
            "git_branch_list",
            self._cow(repo_path, include_remote=bool(remote or all_refs)),
            timeout,
        )
        branches = result.get("branches") or result.get("_list") or []
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
        return str(
            self._call(
                "git_rev_parse", self._cow(repo_path, args=list(args)), timeout
            ).get("output", "")
        ).strip()

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Any = None
    ) -> int:
        return int(
            self._call(
                "git_rev_list_count", self._cow(repo_path, range=range_expr), timeout
            ).get("count", 0)
        )

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Any = None
    ) -> str:
        return str(
            self._call(
                "git_symbolic_ref", self._cow(repo_path, short=short), timeout
            ).get("ref", "")
        ).strip()

    def status_porcelain(self, repo_path: str, timeout: Any = None) -> str:
        # git-mcp git_status is structured; reconstruct `git status --porcelain`
        # faithfully enough for cidx (which filters untracked `??` and warns on any
        # remaining TRACKED line).
        r = self._call("git_status", self._cow(repo_path), timeout)
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
        # git-mcp git_diff returns a unified diff; extract the changed paths from its
        # `diff --git a/PATH b/PATH` headers.
        r = self._call("git_diff", self._cow(repo_path, base=spec), timeout)
        diff = str(r.get("diff", ""))
        files: List[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                marker = line.split(" b/", 1)
                if len(marker) == 2 and marker[1] not in files:
                    files.append(marker[1])
        return files

    def ls_files(self, repo_path: str, timeout: Any = None) -> List[str]:
        return list(
            self._call("git_ls_files", self._cow(repo_path), timeout).get("files") or []
        )

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

    # ── remotes ──────────────────────────────────────────────────────────────

    def remote_list(
        self, repo_path: str, verbose: bool = False, timeout: Any = None
    ) -> str:
        remotes = (
            self._call("git_remote_list", self._cow(repo_path), timeout).get("remotes")
            or []
        )
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
        return str(
            self._call(
                "git_remote_get_url", self._cow(repo_path, name=name), timeout
            ).get("url", "")
        ).strip()

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
