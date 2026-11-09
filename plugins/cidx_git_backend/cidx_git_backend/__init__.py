"""
Delegate cidx's internal git operations to the the git service spoke ().

cidx's ``GitBackend`` seam routes every internal git call through one
object. Its default ``LocalGitBackend`` shells out to git in-process. In the EKS
deployment cidx must do NO git itself — git (like cloning) belongs in a spoke that
owns the credentials and the shared volume. This plugin supplies a ``GitBackend``
that forwards each op to **the git service**, exactly as the clone seam forwards cloning to
the content service.

Addressing is BY PATH, matching the clone seam: the git service's tools accept a
``cow_path`` that runs the op directly on that path on the shared
FlexVol — cidx passes its own ``repo_path``, no workspace_id it never registered.

Transport: JSON-RPC ``tools/call`` over ``POST {CIDX_GIT_MCP_URL}/v1/git/mcp``,
stdlib ``urllib`` only (no third-party deps in the cidx image). the git service returns a
structured JSON result; a few GitBackend methods hand cidx RAW git output it parses
(``status_porcelain``, ``remote_list``, ``diff_name_only``, ``list_branches``,
``show_ref``), so those are reconstructed faithfully here.

``run`` (cidx's git tools: blame, log, show, ...) uses the git service's raw endpoint,
``POST {CIDX_GIT_MCP_URL}/v1/git/exec/raw``: git's stdout as raw bytes, then a
NUL, then a JSON trailer proving the output is whole. A the git service without it (404)
or without its shared secret (503) gets MCP ``git_exec`` instead. No answer is
ever accepted in part: a ``_pagination`` page from ANY tool, a truncated raw
stream and a byte tool's ``OUTPUT_TOO_LARGE`` all raise.

Large-but-legitimate output: a read whose MCP tool answers with a page or
``OUTPUT_TOO_LARGE`` (``ls_files``, ``diff_name_only``, ``list_branches``,
``status_porcelain``, ``log_raw``, ``for_each_ref`` on a big repo) re-runs the
same git command through the raw endpoint, whose whole output -- or an error
-- is the answer. ``ls_remote`` reads only ``reachable``; a page that cut
nothing but the ``refs`` list (a remote with many refs) still answers it whole.

Configuration (env)
-------------------
CIDX_GIT_MCP_URL     the git service base URL.
                     Default: http://localhost:3000
CIDX_GIT_MCP_TOKEN   Bearer token. The raw exec endpoint requires it to equal
                     the git service's GIT_MCP_SHARED_SECRET (401 otherwise).
CIDX_GIT_MCP_TIMEOUT Per-request HTTP timeout seconds (default: 120).

Enable with the server config ``git_backend='delegated'`` (installing the package
registers it on the ``code_indexer.git_backend`` entry point;
``CIDX_GIT_BACKEND=cidx_git_backend:make_git_backend`` is an equivalent override).
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple


import socket


def _is_timeout(exc: BaseException) -> bool:
    """True when *exc* is a transport-level timeout (socket or urllib)."""
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(
        exc.reason, (socket.timeout, TimeoutError)
    ):
        return True
    if isinstance(exc, urllib.error.URLError) and "timed out" in str(exc.reason):
        return True
    return False


def _decode_like_text_mode(raw: bytes) -> str:
    """*raw* git stdout exactly as a local ``subprocess.run(text=True)`` (cidx's
    ``run_git_command``) returns it: the locale encoding, strict errors (an
    undecodable byte raises ``UnicodeDecodeError`` as the local run does), and
    universal newlines (``\\r\\n`` / ``\\r`` -> ``\\n``)."""
    with io.TextIOWrapper(io.BytesIO(raw), encoding=None) as text:
        return text.read()


_DEFAULT_URL = "http://localhost:3000"

#: Items the git service accepts in one git_diff_commits / git_show_objects call. A
#: larger request is split into calls of this size -- still a batch per call.
_MAX_HISTORY_BATCH = 256


class GitMcpBackendError(subprocess.CalledProcessError):
    """A the git service call failed (transport, JSON-RPC, or tool error).

    Subclasses ``subprocess.CalledProcessError`` — NOT a bare ``RuntimeError`` —
    because the ``GitBackend`` contract (``storage/shared/git_backend.py``)
    guarantees that a failed op raises ``CalledProcessError`` "whose ``stderr``
    callers already use", and ``LocalGitBackend`` honours that via ``check=True``.
    ~30 cidx call sites do ``except subprocess.CalledProcessError`` and read
    ``e.stderr`` to drive recovery (remote-already-exists fallback, divergent-branch
    auto-recovery, fetch-error classification). A ``RuntimeError`` would miss every
    one of those handlers and escape uncaught, so the delegated backend must raise
    the same type — with ``stderr`` populated from the git service's error text so the
    substring checks (`"already exists"`, …) still fire.
    """

    def __init__(
        self,
        message: str,
        *,
        stderr: Optional[str] = None,
        returncode: int = 1,
        cmd: str = "the git service",
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


class GitMcpTimeoutError(GitMcpBackendError):
    """A the git service call failed because of a transport or git-level timeout."""


class GitMcpPaginatedResultError(GitMcpBackendError):
    """A the git service tool result carried ``_pagination``: it is a PAGE, not the result.

    the git service's shared MCP response layer pages any result over its size budget
    and marks it with a ``_pagination`` key. For ``git_exec`` that cuts
    ``stdout_b64`` mid-stream -- what arrives is a prefix of git's output (and
    usually not even valid base64); for a structured tool it is a truncated
    list. No caller here pages, and accepting a prefix silently would report a
    short history, a partial blame or a missing file as the whole answer. So
    the marker is a hard error for EVERY tool, wherever it appears.
    """


#: The key the git service's response layer adds to a result it truncated into pages.
_PAGINATION_KEY = "_pagination"

#: the git service's raw exec endpoint: ``git_exec``'s request, answered with git's
#: stdout as raw bytes, one NUL, then a JSON trailer -- never paged, never
#: base64, never truncated silently (see :meth:`GitMcpBackend._exec_raw`).
_RAW_EXEC_ROUTE = "/v1/git/exec/raw"
#: The trailer's separator from stdout. The split is at the LAST one: stdout
#: may contain NULs (``-z`` output), the JSON trailer never does.
_RAW_TRAILER_SEPARATOR = b"\x00"
#: How long a the git service without the raw endpoint (404) or without its shared
#: secret (503) is not asked again before falling back to MCP ``git_exec``.
_RAW_RETRY_SECONDS = 300.0
#: The ``code`` a byte tool's ``isError`` result carries when git's output is
#: over the git service's budget (it refuses rather than truncating).
_OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"


class GitMcpOutputTooLargeError(GitMcpBackendError):
    """git's output exceeded the git service's budget and was refused, not truncated.

    Raised for a byte tool's ``OUTPUT_TOO_LARGE`` error and for a raw exec
    whose trailer says ``truncated``: a prefix of the output is never handed
    on as all of it.
    """


class _RawExecUnavailable(Exception):
    """The raw exec endpoint cannot serve this the git service: use MCP ``git_exec``."""


#: What a too-big MCP answer raises: a page (a structured tool's list cut
#: into pages) or a byte tool's refusal. A read that can be large legitimately
#: answers either by re-running its git command through the raw endpoint.
_OVER_MCP_CEILING = (GitMcpPaginatedResultError, GitMcpOutputTooLargeError)


def _page_marker(text: str) -> Optional[Dict[str, Any]]:
    """The ``_pagination`` block when one text chunk of a tool result is a
    the git service page (an empty dict when the block is not an object), else None."""
    if _PAGINATION_KEY not in text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or _PAGINATION_KEY not in parsed:
        return None
    marker = parsed[_PAGINATION_KEY]
    return marker if isinstance(marker, dict) else {}


# simple-git's timeout plugin kills the subprocess with this exact message.
# git itself (via GIT_HTTP_LOW_SPEED_TIME) produces curl errors that vary by
# libcurl version, so "timed out" is kept as a broad fallback.
_TOOL_TIMEOUT_PATTERNS = ("block timeout reached", "timed out")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """the git service's refusal message from an HTTP error body (bounded)."""
    try:
        raw = exc.read(4096)
    except OSError:
        raw = b""
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return text or str(exc)
    if isinstance(parsed, dict):
        message = parsed.get("message") or parsed.get("error")
        if message:
            return str(message)
    return text


def _complete_raw_output(
    body: bytes, args: Sequence[str]
) -> subprocess.CompletedProcess:
    """The raw exec 200 body as a finished git run, or a raise.

    Split at the LAST NUL; none means the connection was cut.
    """
    at = body.rfind(_RAW_TRAILER_SEPARATOR)
    if at < 0:
        raise GitMcpBackendError(
            "the git service raw exec response has no trailer: the stream was cut, so "
            "the output may be partial"
        )
    stdout, raw_trailer = body[:at], body[at + 1 :]
    try:
        trailer = json.loads(raw_trailer.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitMcpBackendError(
            f"the git service raw exec trailer is not JSON: {exc}"
        ) from exc
    if not isinstance(trailer, dict):
        raise GitMcpBackendError("the git service raw exec trailer is not an object")
    stderr = str(trailer.get("stderr") or "")
    if trailer.get("timed_out") is not False:
        if trailer.get("timed_out") is True:
            raise GitMcpTimeoutError(
                f"the git service raw exec: git timed out: {stderr}", stderr=stderr
            )
        raise GitMcpBackendError("the git service raw exec trailer has no timed_out flag")
    if trailer.get("truncated") is not False:
        if trailer.get("truncated") is True:
            raise GitMcpOutputTooLargeError(
                "the git service raw exec: git's output is over the git service's budget",
                stderr=stderr,
            )
        raise GitMcpBackendError("the git service raw exec trailer has no truncated flag")
    if trailer.get("error"):
        raise GitMcpBackendError(
            f"the git service raw exec could not run git: {trailer['error']}", stderr=stderr
        )
    exit_code = trailer.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise GitMcpBackendError(
            f"the git service raw exec trailer exit_code is not a number: {exit_code!r}"
        )
    declared = trailer.get("stdout_bytes")
    if isinstance(declared, bool) or declared != len(stdout):
        raise GitMcpBackendError(
            f"the git service raw exec sent {len(stdout)} stdout bytes but declared "
            f"{declared!r}: the output is not whole"
        )
    return subprocess.CompletedProcess(
        args=["the git service", "git_exec", *args],
        returncode=exit_code,
        stdout=stdout,
        stderr=stderr,
    )


class GitMcpBackend:
    """A ``GitBackend`` that forwards every op to the git service, addressed by cow_path.

    Implements the same method surface as ``LocalGitBackend`` (structural typing —
    cidx's ``GitBackend`` is a ``typing.Protocol``), so it needs no import from cidx
    and stays a stdlib-only plugin.

    ``credentials_url`` on the network operations is accepted and not
    forwarded: the git service supplies its own credential (``owns_credentials``), so
    cidx's registered URL credential never leaves the pod. A request is
    bounded by its HTTP timeout rather than killed mid-flight -- cidx checks
    its job's cancellation between operations.
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
        self._raw_url = base + _RAW_EXEC_ROUTE
        self._token = token or os.environ.get("CIDX_GIT_MCP_TOKEN") or ""
        env_timeout = os.environ.get("CIDX_GIT_MCP_TIMEOUT")
        self._timeout = timeout or (float(env_timeout) if env_timeout else 120.0)
        self._id = 0
        self._id_lock = threading.Lock()
        #: ``time.monotonic()`` before which the raw exec endpoint is not
        #: tried (it answered 404 or 503); 0.0 = try it.
        self._raw_unavailable_until = 0.0

    # ── capabilities ───────────────────────────────────────────────────────────

    @property
    def owns_credentials(self) -> bool:
        """True: the git service holds the git credential (``GIT_CLONE_TOKEN`` → its own
        credential helper) and injects it for every network op, so cidx must NOT
        embed a token in repo URLs and should strip any credential before storing
        one. The PAT then lives ONLY in the git service."""
        return True

    # ── transport ────────────────────────────────────────────────────────────

    def _call(
        self,
        tool: str,
        arguments: Dict[str, Any],
        timeout: Any = None,
        *,
        unread_list: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One tool call's parsed result, or a raise.

        *unread_list* names a top-level list of the result the caller never
        reads: a page that cut THAT list -- and nothing else -- is accepted,
        because every field the caller does read arrived whole. Any other page
        raises :class:`GitMcpPaginatedResultError`.
        """
        result = self._rpc(
            "tools/call", {"name": tool, "arguments": arguments}, tool, timeout
        )
        text = self._text(result)
        if result.get("isError"):
            if _OUTPUT_TOO_LARGE in text:
                raise GitMcpOutputTooLargeError(
                    f"the git service {tool}: git's output is over the git service's budget: {text}",
                    stderr=text,
                )
            if any(pattern in text for pattern in _TOOL_TIMEOUT_PATTERNS):
                raise GitMcpTimeoutError(
                    f"the git service {tool} tool error: {text}", stderr=text
                )
            raise GitMcpBackendError(f"the git service {tool} tool error: {text}", stderr=text)
        self._refuse_paginated(tool, result, unread_list)
        return self._parse(tool, text)

    @staticmethod
    def _refuse_paginated(
        tool: str, result: Dict[str, Any], unread_list: Optional[str] = None
    ) -> None:
        """Raise when ANY text chunk of *result* is a the git service page -- except a
        page whose cut ``path`` is exactly *unread_list*."""
        for chunk in result.get("content") or []:
            if not (isinstance(chunk, dict) and chunk.get("type") == "text"):
                continue
            marker = _page_marker(str(chunk.get("text", "")))
            if marker is None:
                continue
            if unread_list is None or marker.get("path") != unread_list:
                raise GitMcpPaginatedResultError(
                    f"the git service {tool} returned a paginated (truncated) result; "
                    "refusing to treat one page of the output as all of it"
                )

    def _rpc(
        self, method: str, params: Dict[str, Any], tool: str, timeout: Any
    ) -> Dict[str, Any]:
        """One JSON-RPC request; its ``result`` object, or a raise. *tool*
        names the request in errors."""
        with self._id_lock:
            self._id += 1
            call_id = self._id
        body = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": method,
            "params": params,
        }
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        http_timeout = timeout if isinstance(timeout, (int, float)) else self._timeout
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            if _is_timeout(exc):
                raise GitMcpTimeoutError(
                    f"the git service {tool} request to {self._url} timed out: {exc}",
                    stderr=str(exc),
                ) from exc
            raise GitMcpBackendError(
                f"the git service {tool} request to {self._url} failed: {exc}", stderr=str(exc)
            ) from exc
        payload = json.loads(raw) if raw else {}
        if "error" in payload:
            raise GitMcpBackendError(
                f"the git service {tool} JSON-RPC error: {payload['error']}",
                stderr=str(payload["error"]),
            )
        result = payload.get("result", {}) or {}
        return result if isinstance(result, dict) else {}

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    def _exec_raw(
        self, arguments: Dict[str, Any], args: Sequence[str], http_timeout: float
    ) -> subprocess.CompletedProcess:
        """``git_exec`` through the git service's raw endpoint: git's exact stdout bytes.

        200 is ``<stdout bytes> 0x00 <JSON trailer>``; the output is the
        WHOLE output only when the trailer parses, carries a numeric
        ``exit_code``, is neither ``truncated`` nor ``timed_out``, has no
        ``error``, and ``stdout_bytes`` equals the bytes received. Anything
        else raises -- a cut or capped stream is never returned as complete.
        404 (a the git service predating the endpoint) and 503 (no shared secret
        configured) raise :class:`_RawExecUnavailable` so the caller falls
        back to MCP ``git_exec``; they are remembered for
        ``_RAW_RETRY_SECONDS``. Any other refusal is the git service's answer and
        raises, as MCP ``git_exec`` would have.
        """
        req = urllib.request.Request(
            self._raw_url,
            data=json.dumps(arguments).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            if exc.code in (404, 503):
                self._raw_unavailable_until = time.monotonic() + _RAW_RETRY_SECONDS
                raise _RawExecUnavailable(f"{exc.code}: {detail}") from exc
            if exc.code == 504:
                raise GitMcpTimeoutError(
                    f"the git service raw exec timed out: {detail}", stderr=detail
                ) from exc
            raise GitMcpBackendError(
                f"the git service raw exec refused git {args[0] if args else ''}: {detail}",
                stderr=detail,
                returncode=128,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            if _is_timeout(exc):
                raise GitMcpTimeoutError(
                    f"the git service raw exec to {self._raw_url} timed out: {exc}",
                    stderr=str(exc),
                ) from exc
            raise GitMcpBackendError(
                f"the git service raw exec to {self._raw_url} failed: {exc}", stderr=str(exc)
            ) from exc
        return _complete_raw_output(body, args)

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
                f"the git service {tool} returned an unparseable body: {text[:200]!r}",
                stderr=text,
            ) from exc
        return parsed if isinstance(parsed, dict) else {"_list": parsed}

    @staticmethod
    def _require(tool: str, result: Dict[str, Any], key: str) -> Any:
        """Return ``result[key]`` or fail loud — a structured read whose expected
        key is absent means the git service returned a wrong-shape (but valid-JSON) 200;
        defaulting it would mask a dirty/indexed repo as clean/empty."""
        if key not in result:
            raise GitMcpBackendError(
                f"the git service {tool} response missing {key!r} (got keys {sorted(result)})"
            )
        return result[key]

    def _cow(self, repo_path: str, **extra: Any) -> Dict[str, Any]:
        args: Dict[str, Any] = {"cow_path": repo_path}
        args.update({k: v for k, v in extra.items() if v is not None})
        return args

    # ── remote-only ops ──────────────────────────────────────────────────────

    def ls_remote(
        self, url: str, timeout: Any = None, credentials_url: Any = None
    ) -> bool:
        # git_ls_remote also lists every ref; for a remote with many (~900+)
        # that list is over the git service's MCP ceiling and arrives as a page cut at
        # ``refs``. Only ``reachable`` is read, and a page keeps it whole -- so
        # that page is the answer, while a page cut anywhere else still raises.
        # (Raw exec cannot serve this: git_exec runs no network subcommand.)
        r = self._call(
            "git_ls_remote", {"remote_url": url}, timeout, unread_list="refs"
        )
        return bool(self._require("git_ls_remote", r, "reachable"))

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
        timeout: Any = None,
        retry: bool = False,
        credentials_url: Any = None,
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
        credentials_url: Any = None,
        ref: Optional[str] = None,
    ) -> None:
        # ``ref``: only that branch (refs/remotes/<remote>/<ref>), never the
        # clone's configured refspec -- which may name a deleted branch.
        self._call("git_fetch", self._cow(repo_path, remote=remote, ref=ref), timeout)

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
        timeout: Any = None,
        retry: bool = False,
        credentials_url: Any = None,
    ) -> None:
        args = self._cow(repo_path, remote=remote, branch=branch)
        # Map every documented LocalGitBackend pull mode to its the git service flag. A
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
                raise GitMcpBackendError(f"the git service pull: unsupported mode {mode!r}")
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
        # the git service orders them as git does (-f, then -B|-b). Collapsing force_new
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
        # the git service git_branch_list already returns names in `%(refname:short)` shape
        # (local -> `main`, remote -> `origin/main`), which is the only fmt any cidx
        # caller passes, so that fmt is honoured by the reconstruction below. Any
        # OTHER fmt cannot be reproduced (the git service exposes no --format), so fail loud
        # rather than silently return the wrong string shape.
        if fmt not in (None, "%(refname:short)"):
            raise GitMcpBackendError(
                f"the git service git_branch_list cannot honour fmt={fmt!r}; only the "
                "default / '%(refname:short)' shape is available"
            )
        try:
            result = self._call(
                "git_branch_list",
                self._cow(repo_path, include_remote=bool(remote or all_refs)),
                timeout,
            )
        except _OVER_MCP_CEILING:
            # Too many branches for one MCP answer: git's own listing, whole.
            scope = ["-a"] if all_refs else ["-r"] if remote else []
            stdout = self._exec_text(
                repo_path,
                ["branch", *scope, "--format=%(refname:short)"],
                timeout,
            )
            return [line.strip() for line in stdout.splitlines() if line.strip()]
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
        # the git service git_status is structured; reconstruct `git status --porcelain`
        # faithfully enough for cidx (which filters untracked `??` and warns on any
        # remaining TRACKED line).
        try:
            r = self._call("git_status", self._cow(repo_path), timeout)
        except _OVER_MCP_CEILING:
            # Too many changed paths for one MCP answer: git's own porcelain.
            return self._exec_text(repo_path, ["status", "--porcelain"], timeout)
        # Guard against a valid-JSON but wrong-shape 200 (e.g. `{}`) reading as a
        # clean tree: a real git_status always carries these buckets.
        if not any(k in r for k in ("staged", "unstaged", "untracked", "conflicts")):
            raise GitMcpBackendError(
                f"the git service git_status returned no status buckets (keys {sorted(r)})"
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
        # Use the git service's name_only mode, which returns git's OWN `git diff
        # --name-only -z` path list. Parsing the changed paths out of a unified
        # diff was lossy — a quoted path (non-ASCII / special chars) has no literal
        # `" b/"` token, so it was dropped and the file left un-reindexed. These
        # paths feed reindex decisions, so a dropped file diverges the index.
        try:
            r = self._call(
                "git_diff", self._cow(repo_path, base=spec, name_only=True), timeout
            )
        except _OVER_MCP_CEILING:
            # Too many paths for one MCP answer: the same NUL-delimited list.
            stdout = self._exec_text(
                repo_path, ["diff", "--name-only", "-z", spec], timeout
            )
            return [path for path in stdout.split("\x00") if path]
        return list(self._require("git_diff", r, "files"))

    def ls_files(self, repo_path: str, timeout: Any = None) -> List[str]:
        try:
            r = self._call("git_ls_files", self._cow(repo_path), timeout)
        except _OVER_MCP_CEILING:
            # Too many files for one MCP answer: git's own listing, whole.
            stdout = self._exec_text(repo_path, ["ls-files"], timeout)
            return [line for line in stdout.splitlines() if line]
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
    # the git service returns git's own stdout base64-encoded, so these hand cidx the
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
        try:
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
        except _OVER_MCP_CEILING:
            # The argv git_log_raw runs, through the raw endpoint.
            args = ["log", f"--format={fmt}"]
            if all_refs:
                args.append("--all")
            if since is not None:
                args.append(f"--since={since}")
            if skip:
                args.append(f"--skip={skip}")
            if max_count is not None:
                args.append(f"--max-count={max_count}")
            return self._exec_checked(
                "git_log_raw", repo_path, [*args, *revs, "--"], timeout
            )
        out = self._raw_output("git_log_raw", r)
        self._check("git_log_raw", out)
        return bytes(out.stdout)

    def for_each_ref(
        self,
        repo_path: str,
        fmt: str,
        patterns: Sequence[str] = (),
        *,
        sort: Optional[str] = None,
        count: Optional[int] = None,
        timeout: Any = None,
    ) -> bytes:
        """``git for-each-ref`` in ONE call: every matching ref with its fields.

        the git service validates the arguments (patterns are never option-shaped and
        at most 256; ``sort`` is a key name, optionally ``-``-prefixed) and
        runs ``git for-each-ref --format=<fmt> [--sort] [--count] -- <patterns>``.
        """
        try:
            r = self._call(
                "git_for_each_ref",
                self._cow(
                    repo_path,
                    format=fmt,
                    patterns=list(patterns) or None,
                    sort=sort,
                    count=count,
                ),
                timeout,
            )
        except _OVER_MCP_CEILING:
            # Many refs (a big repo's branch listing): the argv
            # git_for_each_ref runs, through the raw endpoint.
            args = ["for-each-ref", f"--format={fmt}"]
            if sort is not None:
                args.append(f"--sort={sort}")
            if count is not None:
                args.append(f"--count={count}")
            return self._exec_checked(
                "git_for_each_ref", repo_path, [*args, "--", *patterns], timeout
            )
        out = self._raw_output("git_for_each_ref", r)
        self._check("git_for_each_ref", out)
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
                f"the git service {tool} returned {len(items) if isinstance(items, list) else items!r} "
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
                f"the git service {tool} returned stdout that is not base64"
            ) from exc
        return subprocess.CompletedProcess(
            args=["the git service", tool],
            returncode=int(returncode),
            stdout=stdout,
            stderr=str(result.get("stderr", "")),
        )

    @staticmethod
    def _check(tool: str, out: subprocess.CompletedProcess) -> None:
        """Raise as a local ``check=True`` run would, with git's own stderr."""
        if out.returncode != 0:
            raise GitMcpBackendError(
                f"the git service {tool}: git exited {out.returncode}: {out.stderr}",
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
        """``git <args>`` through the git service ``git_exec``: git's own output, the
        exit code returned (not raised) exactly as a local ``check=False`` run."""
        out = self._exec_bytes(repo_path, args, timeout, identity)
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=out.returncode,
            stdout=_decode_like_text_mode(bytes(out.stdout)),
            stderr=out.stderr,
        )

    def _exec_checked(
        self, tool: str, repo_path: str, args: Sequence[str], timeout: Any
    ) -> bytes:
        """git's exact stdout bytes for *args*; raises as ``check=True`` would."""
        out = self._exec_bytes(repo_path, args, timeout)
        self._check(tool, out)
        return bytes(out.stdout)

    def _exec_text(self, repo_path: str, args: Sequence[str], timeout: Any) -> str:
        """git's stdout for *args* as a local text-mode run decodes it; raises
        as ``check=True`` would."""
        return _decode_like_text_mode(
            self._exec_checked(f"git {args[0]}", repo_path, args, timeout)
        )

    def _exec_bytes(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Any = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """``git <args>`` via raw exec (MCP ``git_exec`` when the git service lacks
        it): returncode, git's exact stdout BYTES, stderr. Whole or a raise."""
        # Whole seconds, rounded UP and at least 1 (the git service's minimum): a
        # fractional remaining budget is never cut to 0 or silently shortened.
        seconds = (
            max(1, math.ceil(timeout)) if isinstance(timeout, (int, float)) else None
        )
        arguments = self._cow(
            repo_path,
            args=list(args),
            timeout_seconds=seconds,
            identity=dict(identity) if identity else None,
        )
        http_timeout = self._timeout if seconds is None else seconds + 30
        out: Optional[subprocess.CompletedProcess] = None
        if time.monotonic() >= self._raw_unavailable_until:
            # The raw endpoint first: unpaged, unencoded, with a trailer that
            # proves the output is whole -- blame and every large output.
            try:
                out = self._exec_raw(arguments, args, http_timeout)
            except _RawExecUnavailable:
                out = None
        if out is None:
            result = self._call("git_exec", arguments, http_timeout)
            out = self._raw_output("git_exec", result)
        return out

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Any = None,
    ) -> subprocess.CompletedProcess:
        """the git service ``git_push`` with the git service's credentials; raises on failure."""
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
