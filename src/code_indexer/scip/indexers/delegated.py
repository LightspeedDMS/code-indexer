"""SCIP indexer that delegates the compile step to an MCP server.

A SCIP index for a compiled language needs a working build toolchain and
resolved dependencies. An indexing process that has neither can still produce
one by handing the compile step to a service that does. This indexer is the
generic half of that arrangement: it calls a named MCP tool over JSON-RPC and
reads the index the tool writes into a shared workspace.

Nothing here is specific to a particular server. The target is supplied as an
``endpoint``/``api_key`` pair (see ``utils.remote_endpoint``), and the tool
name, JSON-RPC path, health path and timeout are all constructor arguments.
The defaults match the deployment this was written for -- a sandbox-mcp
service exposing ``sandbox_scip`` -- but any MCP server offering an equivalent
tool works without changing this module.

Originally EVO-65021; mirrors the ``DelegatedCloneBackend`` seam (EVO-64616). cidx stays the source of truth for *what* to build (project
discovery) and for the on-disk output path; the sandbox only runs the one
indexer it is told and writes ``index.scip`` into the shared workspace.

Contract (FROZEN — ``EVO-65021 FROZEN-CONTRACT.md``):

* The sandbox exposes a fixed named MCP tool ``sandbox_scip`` (never an
  agent-chosen exec string). cidx calls it via JSON-RPC ``tools/call``.
* The build target is addressed by EITHER a workspace-mcp ``workspace_id`` OR
  (EVO-65365 Option C) a golden-repo ``golden_alias`` — exactly one, never both
  and never neither. cidx's golden repos are cidx-owned dirs at
  ``/data/data/golden-repos/<alias>`` with NO workspace_id, so a standing
  sandbox-mcp mounts the golden FlexVol and cidx sends the bare ``golden_alias``;
  the sandbox resolves it to ``<GOLDEN_REPOS_ROOT>/<alias>`` and owns the
  confinement — cidx sends the alias verbatim and NEVER builds a path.
  ``project_path`` / ``output_dir`` stay repo-root-relative in both modes.
* Because the golden repo *is* a workspace-mcp workspace bind-mounted at
  ``/workspace`` inside the sandbox container, the ``index.scip`` lands in a
  directory cidx already reads — there is NO artifact transfer. cidx therefore
  owns the output path and verifies the file with ``os.path.exists``; the
  wrapper's stdout JSON line is observability-only, never the success signal.
* Success ⇔ ``exit_code == 0`` **AND** ``output_dir/index.scip`` exists.
* **Fail-loud, no fallback:** any transport error or non-zero exit yields
  ``IndexerStatus.FAILED`` (loud in the per-project result). It NEVER silently
  falls back to a local build — the pod has no toolchain and that is exactly the
  work this backend exists to move off cidx.

Auth/transport: a direct JSON-RPC call carrying a bearer ``api_key``, against the
base ``endpoint`` -- the same two-field surface the delegated clone backend uses
(``utils.remote_endpoint.RemoteServiceEndpoint``). Which config key supplies that
pair, and any fallback between keys, is the startup wiring's business: this
indexer is source-agnostic and simply consumes the resolved endpoint. The
transport itself is ``utils.sandbox_mcp_client.SandboxMcpClient``, shared with
the other sandbox-mcp consumers. No API-GW authorizer secret is distributed to cidx; the
route is constrained by K8s NetworkPolicy + service account.
"""

from __future__ import annotations
from code_indexer.utils.remote_endpoint import RemoteServiceEndpoint
from code_indexer.utils.sandbox_mcp_client import (
    DEFAULT_HEALTH_PATH,
    DEFAULT_JSONRPC_PATH,
    SandboxMcpClient,
    SandboxMcpError,
)

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import IndexerResult, IndexerStatus, SCIPIndexer


logger = logging.getLogger(__name__)

# The MCP tool name the sandbox image exposes for SCIP generation. sandbox-mcp
# prefixes every ``.mcp-sandbox.yaml`` tool with ``sandbox_`` — the YAML tool is
# ``scip`` → the MCP tool is ``sandbox_scip`` (contract §2).
_SCIP_TOOL_NAME = "sandbox_scip"

# HTTP request timeout for the delegated build, aligned to the local indexers'
# existing 30-minute subprocess timeout (typescript.py, java.py) and the tool's
# server-side ``timeout_s: 1800`` (contract §2a / §5).
_DEFAULT_TIMEOUT_SECONDS = 1800

# EVO-65021: bounded budget for the post-build ``index.scip`` visibility check
# (see ``DelegatedIndexer._index_written``). ``output_dir`` is a shared NetApp/ONTAP
# FlexVol that cidx and the sandbox mount through DIFFERENT NFS clients, so a freshly
# written index can be briefly hidden by cidx's stale directory/negative-dentry cache.
# A READDIR usually busts it immediately, but under heavy concurrent churn (verified in
# tools-test EKS: a first golden SCIP build right after a 6-pod rollout, while the startup
# reconcile was CoW-cloning ~31 repos at once) the FlexVol attribute cache held stale past
# 5s and the build fail-soft dropped, only registering on the next maintenance retry. The
# budget is therefore sized to outlast that transient (~ONTAP acdirmax) so a first attempt
# succeeds; it is paid ONLY on the miss path (gated on exit_code==0), which is rare.
_INDEX_SETTLE_SECONDS = 20.0
_INDEX_POLL_SECONDS = 0.25


class DelegatedIndexer(SCIPIndexer):
    """SCIP indexer that delegates the compile step to sandbox-mcp.

    A single instance handles every language: the target language is passed per
    call (``generate(..., language=...)``) and forwarded to the sandbox tool,
    mirroring how the local map already points both ``java`` and ``kotlin`` at
    one ``JavaIndexer``.
    """

    def __init__(
        self,
        config: RemoteServiceEndpoint,
        repo_root: Path,
        *,
        tool_name: str = _SCIP_TOOL_NAME,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        jsonrpc_path: str = DEFAULT_JSONRPC_PATH,
        health_path: str = DEFAULT_HEALTH_PATH,
    ) -> None:
        """Initialize the delegated indexer.

        Args:
            config: Anything exposing ``endpoint`` and ``api_key``
                (:class:`~code_indexer.utils.remote_endpoint.RemoteServiceEndpoint`).
                ``endpoint`` is the MCP server's *base* URL (e.g.
                ``http://sandbox-mcp:8200``); the JSON-RPC and health paths below
                are appended to it. Which config key supplies this is resolved by
                the startup wiring, not here.
            repo_root: Golden-repo root (== the sandbox ``/workspace`` mount).
                Used to derive the workspace-relative ``project_path`` /
                ``output_dir`` the sandbox expects.
            tool_name: MCP tool name to call (default ``sandbox_scip``).
            timeout_seconds: HTTP request timeout (default 1800s).
            jsonrpc_path: Path appended to the base ``endpoint`` for the
                JSON-RPC POST. Default ``/v1/sandbox/mcp`` (the sandbox-mcp MCP
                front door — reconciled seam #2).
            health_path: Path appended to the base ``endpoint`` for the health
                probe. Default ``/health`` (un-versioned, un-authed).
        """
        # Transport (endpoint, bearer auth, tools/call, envelope parsing,
        # health) is the shared sandbox-mcp client's. It fetches ``requests``
        # through ``self._requests`` so a test can swap the module per instance.
        self._client = SandboxMcpClient(
            config,
            timeout_seconds=timeout_seconds,
            jsonrpc_path=jsonrpc_path,
            health_path=health_path,
            requests_provider=lambda: self._requests(),
        )
        self._endpoint = self._client.endpoint
        self._api_key = self._client.api_key
        self._repo_root = Path(repo_root)
        self._tool_name = tool_name
        self._timeout = timeout_seconds
        # FlexVol index-visibility settle budget (overridable in tests).
        self._index_settle_seconds = _INDEX_SETTLE_SECONDS
        self._index_poll_seconds = _INDEX_POLL_SECONDS

    # ------------------------------------------------------------------ helpers
    def _requests(self) -> Any:
        """Return the lazily-imported ``requests`` module (keeps startup fast)."""
        import requests  # noqa: PLC0415

        return requests

    def _relative(self, path: Path) -> str:
        """Workspace-relative POSIX string for *path* under ``repo_root``.

        A project at the repo root yields ``"."`` (contract §1).
        """
        return path.resolve().relative_to(self._repo_root.resolve()).as_posix()

    def _build_request(
        self,
        *,
        workspace_id: Optional[str],
        golden_alias: Optional[str],
        project_path: str,
        build_system: str,
        language: str,
        output_dir: str,
        allow_degenerate: bool = False,
    ) -> Dict[str, Any]:
        """Build the ``tools/call`` request for ``sandbox_scip``.

        Puts EXACTLY ONE of ``workspace_id`` / ``golden_alias`` into
        ``arguments`` (never both, never neither — ``generate`` enforces the XOR
        before calling here). Everything else is repo-root-relative and
        unchanged across both addressing modes.

        EVO-65365: ``allow_degenerate`` is added ONLY when True (a golden repo
        opts into the sandbox's locals-only fallback for a bare/unbuilt repo);
        omitted otherwise so a non-golden request is byte-identical to before.
        """
        arguments: Dict[str, Any] = {
            "project_path": project_path,
            "build_system": build_system,
            "language": language,
            "output_dir": output_dir,
        }
        if golden_alias:
            arguments["golden_alias"] = golden_alias
        else:
            arguments["workspace_id"] = workspace_id
        if allow_degenerate:
            arguments["allow_degenerate"] = True
        return self._client.build_tool_call(self._tool_name, arguments)

    # --------------------------------------------------------------------- ABC
    def generate(
        self,
        project_dir: Path,
        output_dir: Path,
        build_system: str,
        *,
        workspace_id: Optional[str] = None,
        golden_alias: Optional[str] = None,
        language: Optional[str] = None,
        allow_degenerate: bool = False,
    ) -> IndexerResult:
        """Delegate SCIP generation for one project to sandbox-mcp.

        The build target is addressed by EXACTLY ONE of ``workspace_id`` (a
        workspace-mcp id) or ``golden_alias`` (EVO-65365 Option C — a bare
        golden-repo alias the sandbox resolves under its golden-repos root).
        Passing neither or both is a contract violation and fails loud.

        Returns an :class:`IndexerResult`. On any error (missing wiring,
        transport failure, non-zero exit, or missing output file) the status is
        ``FAILED`` and ``output_file`` is ``None`` — never a raised exception,
        never a local fallback (contract §6).
        """
        start_time = time.time()

        if bool(workspace_id) == bool(golden_alias):
            return self._failure(
                start_time,
                "DelegatedIndexer requires EXACTLY ONE of {workspace_id, "
                "golden_alias} to address the build target; got "
                f"workspace_id={'set' if workspace_id else 'unset'}, "
                f"golden_alias={'set' if golden_alias else 'unset'}. Is "
                "scip_backend wiring passing exactly one through?",
            )
        if not language:
            return self._failure(
                start_time,
                "DelegatedIndexer requires the discovered project language; "
                "none was provided.",
            )
        if not self._endpoint:
            return self._failure(
                start_time,
                "DelegatedIndexer has no endpoint configured. The startup "
                "wiring resolves which config key supplies it and reports the "
                "deployment-specific name; see scip_backend_wiring.",
            )

        try:
            project_path = self._relative(project_dir)
            output_rel = self._relative(output_dir)
        except ValueError as exc:
            return self._failure(
                start_time,
                f"project_dir/output_dir not under repo_root {self._repo_root}: {exc}",
            )

        # EVO-65021: success is judged by the presence of ``output_dir/index.scip``
        # AFTER this call (``_map_result``). ``output_dir`` is the bind-mounted
        # golden-repo path REUSED across runs, so a stale ``index.scip`` from a
        # prior successful build would otherwise satisfy that check even if the
        # sandbox produced nothing this run (a no-op / short-circuit that still
        # exits 0). Delete it first so the existence check can only pass on a file
        # THIS call wrote — keeping cidx's determination consistent with the
        # sandbox wrapper, which itself deletes stale output and gates on a fresh
        # non-empty file.
        stale_removed = self._remove_stale_index(output_dir)
        if stale_removed is not None:
            return self._failure(start_time, stale_removed)

        request = self._build_request(
            workspace_id=workspace_id,
            golden_alias=golden_alias,
            project_path=project_path,
            build_system=build_system,
            language=language,
            output_dir=output_rel,
            allow_degenerate=allow_degenerate,
        )

        target = (
            f"golden_alias={golden_alias}"
            if golden_alias
            else f"workspace_id={workspace_id}"
        )
        logger.info(
            "DelegatedIndexer: requesting sandbox_scip for project '%s' "
            "(build_system=%s, language=%s, %s)",
            project_path,
            build_system,
            language,
            target,
        )
        try:
            run_result = self._client.post(request)
        except SandboxMcpError as exc:
            return self._failure(start_time, str(exc))

        return self._map_result(
            run_result, output_dir=output_dir, start_time=start_time
        )

    def _remove_stale_index(self, output_dir: Path) -> Optional[str]:
        """Delete a pre-existing ``output_dir/index.scip`` before dispatch.

        Returns ``None`` on success (nothing there, or removed), or an error
        message string when the stale file could not be removed — in which case
        the caller fails loud rather than risk judging success against a file
        this run did not write.
        """
        scip_out = output_dir / "index.scip"
        try:
            scip_out.unlink()
            logger.info(
                "DelegatedIndexer: removed stale index.scip at %s before "
                "delegating (this-run success gating)",
                scip_out,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            return (
                f"could not remove stale index.scip at {scip_out} before the "
                f"delegated build: {exc}"
            )
        return None

    def _index_written(self, scip_file: Path) -> bool:
        """Return True once the sandbox's ``index.scip`` is visible on cidx's mount.

        EVO-65021: ``output_dir`` lives on a shared NetApp/ONTAP FlexVol that cidx and
        the sandbox mount through DIFFERENT NFS clients. ``_remove_stale_index`` unlinks
        the old ``index.scip`` through cidx's client, priming a NEGATIVE dentry cache for
        that name; the sandbox then writes a fresh ``index.scip`` through ITS client.
        cidx's immediate ``Path.exists()`` reads the stale negative cache and misses the
        file — which silently dropped a valid index on every delegated golden build. A
        READDIR of the parent invalidates the cached directory listing, so we
        ``os.listdir`` before re-checking; a short bounded poll backstops any residual
        write-propagation delay. The wait is paid ONLY on the miss path: a normally
        visible index returns on the first probe, and a genuine no-index build spends the
        (small) budget once — rare, and already fail-soft downstream.
        """
        if scip_file.exists():
            return True
        deadline = time.monotonic() + self._index_settle_seconds
        parent = scip_file.parent
        while True:
            # Bust the NFS negative-dentry / directory-attribute cache, then re-check.
            try:
                os.listdir(parent)
            except OSError:
                pass
            if scip_file.exists():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._index_poll_seconds)

    def _map_result(
        self,
        run_result: Dict[str, Any],
        *,
        output_dir: Path,
        start_time: float,
    ) -> IndexerResult:
        """Map a ``SandboxRunResult`` onto an ``IndexerResult`` (contract §4)."""
        exit_code = run_result.get("exit_code")
        if not isinstance(exit_code, int):
            exit_code = -1
        combined_output = str(run_result.get("output") or "")
        duration_ms = run_result.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            duration = float(duration_ms) / 1000.0
        else:
            duration = time.time() - start_time

        # cidx owns the output path; it never trusts an echoed one. Only the
        # exit_code==0 branch cares whether the file is present, so gate the
        # (potentially settling) visibility check on it — a non-zero exit is FAILED
        # regardless and must not pay the FlexVol settle budget.
        scip_file = output_dir / "index.scip"
        file_exists = exit_code == 0 and self._index_written(scip_file)

        if exit_code == 0 and file_exists:
            # EVO-65365: the sandbox reports ``degenerate`` in its envelope when it
            # emitted a locals-only index (the ``allow_degenerate`` path for a
            # bare/unbuilt golden repo). Surface it on the result so a degenerate
            # index is OBSERVABLE and distinct from a full one — the delegated-build
            # worker records the ``delegated_degenerate`` metric from this flag.
            degenerate = bool(run_result.get("degenerate"))
            if degenerate:
                logger.info(
                    "DelegatedIndexer: sandbox produced a DEGENERATE (locals-only) "
                    "index at %s (allow_degenerate path)",
                    scip_file,
                )
            return IndexerResult(
                status=IndexerStatus.SUCCESS,
                duration_seconds=duration,
                output_file=scip_file,
                stdout=combined_output,
                stderr="",
                exit_code=exit_code,
                degenerate=degenerate,
            )

        errors = run_result.get("errors")
        stderr_parts = []
        if isinstance(errors, list) and errors:
            stderr_parts.append("\n".join(str(e) for e in errors))
        if exit_code == 0 and not file_exists:
            stderr_parts.append(
                f"sandbox_scip exited 0 but no index.scip was written at {scip_file}"
            )
        if not stderr_parts:
            stderr_parts.append(combined_output[-2000:])

        return IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=duration,
            output_file=None,
            stdout=combined_output,
            stderr="\n".join(p for p in stderr_parts if p),
            exit_code=exit_code,
        )

    def _failure(self, start_time: float, message: str) -> IndexerResult:
        """Build a loud FAILED result (logged) with no output file."""
        logger.warning("DelegatedIndexer: %s", message)
        return IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=time.time() - start_time,
            output_file=None,
            stdout="",
            stderr=message,
            exit_code=-1,
        )

    def is_available(self) -> bool:
        """Probe sandbox-mcp reachability (contract §5) — NOT ``shutil.which``.

        Returns True iff the health endpoint answers 2xx. Any transport error or
        non-2xx yields False (the startup wiring turns that into a fail-loud
        ``RuntimeError``; per-call generation independently fail-softs).
        """
        return self._client.is_healthy()

    def get_version(self) -> Optional[str]:
        """Return None — the remote indexer version is not locally determinable.

        (The wrapper reports its ``indexer_version`` in the observability JSON
        line, which is logged from ``output`` but is not load-bearing.)
        """
        return None
