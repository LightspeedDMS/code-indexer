"""An in-process fake of sandbox-mcp, and delegate-shaped clients for it.

sandbox-mcp is the remote service the SCIP and X-Ray seams delegate to. cidx
calls two of its fixed tools (``code_indexer.utils.sandbox_mcp_client``):

* ``sandbox_run_profile`` runs a named profile and answers a run-result
  envelope (``exit_code``, ``output``, ``duration_ms``, ``errors``). An output
  larger than the replica sends inline is PARKED: the envelope then carries an
  empty ``output``, a ``result_handle`` and the number of ``output_chunks``;
* ``sandbox_result_fetch`` pages a parked result: ``{"result_handle",
  "chunk"}`` -> ``{"result_handle", "chunk", "data", "next_chunk"}``
  (``next_chunk`` None on the last page).

sandbox-mcp runs as several replicas behind one endpoint, so the fetch for a
page may land on a replica that did not run the profile. Parked results live
in a :class:`SandboxResultStore` the replicas SHARE (the service's cluster
store); the clients here round-robin their calls over the replicas they are
given, so every paged read crosses replicas. A replica built with its own
store (``FakeSandboxMcp(state, results=SandboxResultStore())``) reproduces
the per-node-state defect: a fetch on the other replica answers 404.

What runs inside a profile is a stand-in, not the real toolchain:

* ``scip``: the "index" is deterministic bytes derived from the project's
  files; a language in :attr:`SandboxState.unsupported_languages` answers
  ``unsupported``; a repository the service does not hold is refused;
* ``xray`` / ``xray-inline`` / ``xray-graph``: the "evaluator" is
  :func:`fake_evaluator` -- Rust-shaped source whose first string literal is a
  needle; every line containing it is a finding. ``xray-inline`` reads the
  files sent with the request, never a repository.

Clients never raise for a known failure: like the real delegates they answer
with a failed SCIP outcome (``backpressure`` on HTTP 429) or X-Ray error
tuples / an ``{"ok": False, "error": ...}`` graph result.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from cidx_sdk.entry_points import (
    SCIP_BACKEND_ENTRY_POINT_GROUP,
    XRAY_BACKEND_ENTRY_POINT_GROUP,
)
from cidx_sdk.seams.resolver import add_entry_points, remove_entry_points
from cidx_sdk.seams.scip import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    ScipBuildOutcome,
    ScipBuildRequest,
    sha256_hex,
)
from cidx_sdk.seams.selection import DELEGATED_MODE
from cidx_sdk.seams.xray import (
    BatchResult,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)

RUN_PROFILE_TOOL = "sandbox_run_profile"
RESULT_FETCH_TOOL = "sandbox_result_fetch"

SCIP_PROFILE = "scip"
XRAY_PROFILE = "xray"
XRAY_INLINE_PROFILE = "xray-inline"
XRAY_GRAPH_PROFILE = "xray-graph"

#: The entry point an X-Ray evaluator must define (as the real engine checks).
EVALUATOR_ENTRY = "fn evaluate_node"
_NEEDLE = re.compile(r'"((?:[^"\\]|\\.)+)"')


class FakeSandboxError(Exception):
    """A tools/call with no usable reply: ``status_code`` as the transport
    reports it (``SandboxMcpError`` carries the same attribute)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def fake_evaluator(needle: str) -> str:
    """Evaluator source the fake runs: findings on lines containing *needle*."""
    escaped = needle.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
        f'    node.lines_containing("{escaped}")\n'
        "}\n"
    )


def evaluator_needle(code: str) -> Optional[str]:
    """The needle of a :func:`fake_evaluator` source; None when it is not one."""
    if EVALUATOR_ENTRY not in code:
        return None
    match = _NEEDLE.search(code)
    if match is None:
        return None
    return re.sub(r"\\(.)", r"\1", match.group(1))


# ── the service ──────────────────────────────────────────────────────────────


@dataclass
class SandboxResultStore:
    """Parked run outputs, by handle: what every replica can page from."""

    chunk_bytes: int = 1024
    parked: Dict[str, bytes] = field(default_factory=dict)
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def park(self, data: bytes) -> Tuple[str, int]:
        handle = f"result-{next(self._ids)}"
        self.parked[handle] = data
        return handle, self.chunks(handle)

    def chunks(self, handle: str) -> int:
        data = self.parked[handle]
        return max(1, -(-len(data) // self.chunk_bytes))

    def page(self, handle: str, chunk: int) -> Dict[str, Any]:
        if handle not in self.parked:
            raise FakeSandboxError(f"unknown result handle {handle}", status_code=404)
        total = self.chunks(handle)
        if chunk < 0 or chunk >= total:
            raise FakeSandboxError(
                f"chunk {chunk} out of range for {handle} ({total} chunks)",
                status_code=400,
            )
        start = chunk * self.chunk_bytes
        data = self.parked[handle][start : start + self.chunk_bytes]
        return {
            "result_handle": handle,
            "chunk": chunk,
            "data": base64.b64encode(data).decode("ascii"),
            "next_chunk": chunk + 1 if chunk + 1 < total else None,
        }


@dataclass
class SandboxState:
    """What the replicas share: the repositories they can address (cidx path
    or golden alias -> directory), the parked results, and switches."""

    results: SandboxResultStore = field(default_factory=SandboxResultStore)
    repos: Dict[str, Path] = field(default_factory=dict)
    #: Outputs longer than this are parked behind a result handle.
    inline_output_bytes: int = 2048
    unsupported_languages: Set[str] = field(default_factory=set)
    #: Answer every run with HTTP 429 (no sandbox capacity).
    busy: bool = False
    healthy: bool = True
    inline_max_files: int = 64
    inline_max_file_bytes: int = 1 << 20
    inline_max_total_bytes: int = 8 << 20

    def serve(self, key: str, directory: Path) -> Path:
        """Make *directory* addressable as *key* (a path or golden alias)."""
        directory.mkdir(parents=True, exist_ok=True)
        self.repos[key] = directory
        return directory


class FakeSandboxMcp:
    """One sandbox-mcp replica over a :class:`SandboxState`.

    ``results`` defaults to the state's shared store; pass a private one to
    model a replica that keeps parked results in its own memory.
    """

    def __init__(
        self,
        state: SandboxState,
        *,
        name: str = "replica",
        results: Optional[SandboxResultStore] = None,
    ) -> None:
        self.state = state
        self.name = name
        self.results = results if results is not None else state.results
        self.requests: List[Tuple[str, Dict[str, Any]]] = []

    # -- tools ----------------------------------------------------------------

    def sandbox_run_profile(
        self, profile: str, arguments: Mapping[str, Any]
    ) -> Dict[str, Any]:
        self.requests.append((RUN_PROFILE_TOOL, {"profile": profile, **arguments}))
        if self.state.busy:
            raise FakeSandboxError("no sandbox capacity", status_code=429)
        runners = {
            SCIP_PROFILE: self._run_scip,
            XRAY_PROFILE: self._run_xray,
            XRAY_INLINE_PROFILE: self._run_xray_inline,
            XRAY_GRAPH_PROFILE: self._run_xray_graph,
        }
        runner = runners.get(profile)
        if runner is None:
            raise FakeSandboxError(f"unknown run profile {profile}", status_code=400)
        exit_code, payload, errors = runner(arguments)
        output = json.dumps(payload).encode("utf-8") if payload is not None else b""
        envelope: Dict[str, Any] = {
            "exit_code": exit_code,
            "output": "",
            "duration_ms": 1,
            "errors": errors,
            "result_handle": None,
            "output_chunks": 0,
        }
        if len(output) > self.state.inline_output_bytes:
            handle, chunks = self.results.park(output)
            envelope["result_handle"] = handle
            envelope["output_chunks"] = chunks
        else:
            envelope["output"] = output.decode("utf-8")
        return envelope

    def sandbox_result_fetch(self, result_handle: str, chunk: int) -> Dict[str, Any]:
        self.requests.append(
            (RESULT_FETCH_TOOL, {"result_handle": result_handle, "chunk": chunk})
        )
        return self.results.page(result_handle, chunk)

    def health(self) -> bool:
        return self.state.healthy

    # -- profiles --------------------------------------------------------------

    def _repo(self, *keys: Optional[str]) -> Optional[Path]:
        for key in keys:
            if key and key in self.state.repos:
                return self.state.repos[key]
        return None

    def _run_scip(
        self, args: Mapping[str, Any]
    ) -> Tuple[int, Optional[Dict[str, Any]], List[str]]:
        repo = self._repo(args.get("golden_alias"), args.get("repo_path"))
        if repo is None:
            return 2, None, [f"cannot address repository {args.get('repo_path')}"]
        language = str(args.get("language", ""))
        if language in self.state.unsupported_languages:
            return 0, {"status": STATUS_UNSUPPORTED, "error": f"no {language}"}, []
        project = (repo / str(args.get("project_path", "."))).resolve()
        if repo.resolve() not in (project, *project.parents) or not project.is_dir():
            return 0, {"status": STATUS_FAILED, "error": "no such project"}, []
        listing = sorted(
            p.relative_to(project).as_posix()
            for p in project.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        index = ("fake-scip\n" + language + "\n" + "\n".join(listing)).encode()
        return (
            0,
            {
                "status": STATUS_OK,
                "index_b64": base64.b64encode(index).decode("ascii"),
                "sha256": sha256_hex(index),
                "indexer_name": f"fake-scip-{language}",
                "indexer_version": "0",
                "degenerate": False,
            },
            [],
        )

    @staticmethod
    def _scan(
        code: str, specs: Sequence[Mapping[str, Any]], read: Any
    ) -> Tuple[int, Optional[Dict[str, Any]], List[str]]:
        needle = evaluator_needle(code)
        if needle is None:
            return 1, {"error_kind": "validation"}, ["evaluator does not compile"]
        files: List[Dict[str, Any]] = []
        for spec in specs:
            path = str(spec.get("file_path", ""))
            data: Optional[bytes] = read(path)
            if data is None:
                files.append({"file_path": path, "error": "unreadable"})
                continue
            lines = data.decode("utf-8", "replace").splitlines()
            findings = [
                {"line": n, "pattern": needle, "snippet": line.strip()}
                for n, line in enumerate(lines, start=1)
                if needle in line
            ]
            files.append({"file_path": path, "findings": findings, "lines": lines})
        return 0, {"files": files, "debug": [f"scanned {len(files)}"]}, []

    def _run_xray(
        self, args: Mapping[str, Any]
    ) -> Tuple[int, Optional[Dict[str, Any]], List[str]]:
        repo = self._repo(args.get("repo_path"))
        if repo is None:
            return 2, None, [f"cannot address repository {args.get('repo_path')}"]

        def read(rel: str) -> Optional[bytes]:
            target = (repo / rel).resolve()
            if repo.resolve() not in target.parents or not target.is_file():
                return None
            return target.read_bytes()

        return self._scan(
            str(args.get("evaluator_code", "")), args.get("file_specs", []), read
        )

    def _run_xray_inline(
        self, args: Mapping[str, Any]
    ) -> Tuple[int, Optional[Dict[str, Any]], List[str]]:
        sent: Mapping[str, str] = args.get("files", {})
        decoded = {rel: base64.b64decode(data) for rel, data in sent.items()}
        total = sum(len(data) for data in decoded.values())
        if (
            len(decoded) > self.state.inline_max_files
            or total > self.state.inline_max_total_bytes
            or any(len(d) > self.state.inline_max_file_bytes for d in decoded.values())
        ):
            raise FakeSandboxError("inline request over its limits", status_code=413)
        return self._scan(
            str(args.get("evaluator_code", "")),
            args.get("file_specs", []),
            decoded.get,
        )

    def _run_xray_graph(
        self, args: Mapping[str, Any]
    ) -> Tuple[int, Optional[Dict[str, Any]], List[str]]:
        sent = args.get("files")
        if sent is not None:
            decoded = {rel: base64.b64decode(data) for rel, data in sent.items()}
            read: Any = decoded.get
        else:
            repo = self._repo(args.get("repo_root"))
            if repo is None:
                return 2, None, [f"cannot address repository {args.get('repo_root')}"]

            def read(rel: str) -> Optional[bytes]:
                target = repo / rel
                return target.read_bytes() if target.is_file() else None

        specs = [{"file_path": p} for p in args.get("file_paths", [])]
        code, payload, errors = self._scan(
            str(args.get("evaluator_code", "")), specs, read
        )
        if code != 0 or payload is None:
            return code, payload, errors
        findings = [
            {"file_path": f["file_path"], "line": hit["line"]}
            for f in payload["files"]
            for hit in f.get("findings", [])
        ]
        return 0, {"graph": True, "findings": findings}, []


# ── delegate-shaped clients ──────────────────────────────────────────────────


class _ReplicaClient:
    """Calls the replicas in turn, as a load balancer in front of them would,
    and reads a parked output back page by page."""

    def __init__(self, replicas: Sequence[FakeSandboxMcp]) -> None:
        if not replicas:
            raise ValueError("at least one sandbox-mcp replica is needed")
        self.replicas = list(replicas)
        self._next = itertools.cycle(self.replicas)

    def _replica(self) -> FakeSandboxMcp:
        return next(self._next)

    def run(self, profile: str, arguments: Mapping[str, Any]) -> Tuple[int, Any, str]:
        """``(exit_code, decoded JSON output or None, joined errors)``.

        Raises:
            FakeSandboxError: the transport failed (status in the error).
        """
        envelope = self._replica().sandbox_run_profile(profile, arguments)
        raw = envelope.get("output", "").encode("utf-8")
        handle = envelope.get("result_handle")
        if handle:
            raw = self._fetch_all(str(handle))
        output = json.loads(raw) if raw else None
        return int(envelope["exit_code"]), output, "; ".join(envelope["errors"])

    def _fetch_all(self, handle: str) -> bytes:
        parts: List[bytes] = []
        chunk: Optional[int] = 0
        while chunk is not None:
            page = self._replica().sandbox_result_fetch(handle, chunk)
            if page.get("chunk") != chunk:
                raise FakeSandboxError(f"result page {chunk} answered out of order")
            parts.append(base64.b64decode(page["data"]))
            chunk = page.get("next_chunk")
        return b"".join(parts)


class FakeSandboxScipBackend(_ReplicaClient):
    """A ``ScipBackend`` that builds through sandbox-mcp's ``scip`` profile."""

    def is_available(self) -> bool:
        return all(replica.health() for replica in self.replicas)

    def build_index(self, request: ScipBuildRequest) -> ScipBuildOutcome:
        arguments = {
            "repo_path": request.repo_path,
            "golden_alias": request.golden_alias,
            "project_path": request.project_path,
            "build_system": request.build_system,
            "language": request.language,
            "allow_degenerate": request.allow_degenerate,
            "timeout_seconds": request.timeout_seconds,
        }
        try:
            exit_code, output, errors = self.run(SCIP_PROFILE, arguments)
        except FakeSandboxError as exc:
            return ScipBuildOutcome(
                status=STATUS_FAILED,
                error=f"sandbox-mcp: {exc}",
                backpressure=exc.status_code == 429,
            )
        if exit_code != 0 or not isinstance(output, dict):
            return ScipBuildOutcome(
                status=STATUS_FAILED, error=errors or f"exit code {exit_code}"
            )
        status = output.get("status")
        if status != STATUS_OK:
            return ScipBuildOutcome(
                status=str(status or STATUS_FAILED), error=str(output.get("error", ""))
            )
        return ScipBuildOutcome(
            status=STATUS_OK,
            index_bytes=base64.b64decode(output["index_b64"]),
            sha256=str(output["sha256"]),
            degenerate=bool(output.get("degenerate")),
            indexer_name=output.get("indexer_name"),
            indexer_version=output.get("indexer_version"),
            duration_ms=1,
        )


@dataclass(frozen=True)
class FakeInlineLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int


def _error_tuple(
    file_path: str, error_type: str, message: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    return (
        [],
        [
            {
                "file_path": file_path,
                "line_number": 0,
                "error_type": error_type,
                "error_message": message,
            }
        ],
        None,
    )


def _graph_error(error_type: str, message: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": {"error_type": error_type, "error_message": message},
        "status": None,
        "findings": [],
        "refine": [],
    }


class FakeSandboxXrayBackend(_ReplicaClient):
    """An ``XrayBackend`` (with the inline extension) over sandbox-mcp."""

    @property
    def inline_limits(self) -> FakeInlineLimits:
        state = self.replicas[0].state
        return FakeInlineLimits(
            max_files=state.inline_max_files,
            max_file_bytes=state.inline_max_file_bytes,
            max_total_bytes=state.inline_max_total_bytes,
        )

    # -- scan -----------------------------------------------------------------

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        return self._scan(
            XRAY_PROFILE,
            request,
            {
                "evaluator_code": request.evaluator_code,
                "file_specs": request.file_specs,
                "repo_path": request.repo_path,
                "timeout_seconds": request.timeout_seconds,
            },
        )

    def execute_scan_inline(
        self, request: XrayScanRequest, files: Mapping[str, bytes]
    ) -> XrayScanOutcome:
        return self._scan(
            XRAY_INLINE_PROFILE,
            request,
            {
                "evaluator_code": request.evaluator_code,
                "file_specs": request.file_specs,
                "files": _encoded(files),
                "timeout_seconds": request.timeout_seconds,
            },
        )

    def _scan(
        self, profile: str, request: XrayScanRequest, arguments: Dict[str, Any]
    ) -> XrayScanOutcome:
        if not request.file_specs:
            return XrayScanOutcome([])
        try:
            exit_code, output, errors = self.run(profile, arguments)
        except FakeSandboxError as exc:
            return XrayScanOutcome([_error_tuple("", "SandboxError", str(exc))])
        if exit_code != 0 or not isinstance(output, dict):
            return XrayScanOutcome(
                [
                    _error_tuple(
                        spec.get("file_path", ""), "ValidationError", errors or "failed"
                    )
                    for spec in request.file_specs
                ]
                if output and output.get("error_kind") == "validation"
                else [_error_tuple("", "XRayCliError", errors or "failed")]
            )
        results: BatchResult = []
        for spec, scanned in zip(request.file_specs, output["files"]):
            path = str(spec.get("file_path", ""))
            if "error" in scanned:
                results.append(_error_tuple(path, "XRayCliError", scanned["error"]))
                continue
            lines: List[str] = scanned["lines"]
            matches = [
                {
                    "line_number": hit["line"],
                    "file_path": path,
                    "language": spec.get("lang", ""),
                    "pattern": hit["pattern"],
                    "snippet": hit["snippet"],
                    "line_content": lines[hit["line"] - 1],
                }
                for hit in scanned["findings"]
            ]
            results.append((matches, [], None))
        return XrayScanOutcome(results, debug_messages=list(output.get("debug", [])))

    # -- graph ----------------------------------------------------------------

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        return self._graph(request, None)

    def execute_graph_analysis_inline(
        self, request: XrayGraphRequest, files: Mapping[str, bytes]
    ) -> Dict[str, Any]:
        return self._graph(request, files)

    def _graph(
        self, request: XrayGraphRequest, files: Optional[Mapping[str, bytes]]
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "evaluator_code": request.evaluator_code,
            "repo_root": request.repo_root,
            "file_paths": list(request.file_paths),
            "refine": request.refine,
            "timeout_seconds": request.timeout_seconds,
        }
        if files is not None:
            arguments["files"] = _encoded(files)
        try:
            exit_code, output, errors = self.run(XRAY_GRAPH_PROFILE, arguments)
        except FakeSandboxError as exc:
            return _graph_error("SandboxError", str(exc))
        if exit_code != 0 or not isinstance(output, dict):
            return _graph_error("ValidationError", errors or "failed")
        return {
            "ok": True,
            "error": None,
            "status": "ok",
            "findings": output["findings"],
            "refine": [],
        }


def _encoded(files: Mapping[str, bytes]) -> Dict[str, str]:
    return {rel: base64.b64encode(data).decode("ascii") for rel, data in files.items()}


# ── registration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Registration:
    name: str
    factory: Any

    def load(self) -> Any:
        return self.factory


def register_as_delegate(*replicas: FakeSandboxMcp) -> Dict[str, List[str]]:
    """Register clients over *replicas* as the ``delegated`` provider of the
    SCIP and X-Ray seams, exactly as an installed delegate is found. Returns
    what to pass to :func:`unregister_delegate`."""
    factories = {
        SCIP_BACKEND_ENTRY_POINT_GROUP: lambda: FakeSandboxScipBackend(replicas),
        XRAY_BACKEND_ENTRY_POINT_GROUP: lambda: FakeSandboxXrayBackend(replicas),
    }
    for group, factory in factories.items():
        add_entry_points(group, [_Registration(DELEGATED_MODE, factory)])
    return {group: [DELEGATED_MODE] for group in factories}


def unregister_delegate(registered: Dict[str, List[str]]) -> None:
    """Withdraw what :func:`register_as_delegate` added."""
    for group, names in registered.items():
        remove_entry_points(group, names)
