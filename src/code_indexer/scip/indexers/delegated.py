"""Delegated SCIP indexer (EVO-65021).

Mirrors the ``DelegatedCloneBackend`` seam (EVO-64616): instead of running a
build toolchain inside the (toolchain-less) cidx indexing pod, the *compile*
step is delegated to **sandbox-mcp**, which already builds repos with resolved
dependencies. cidx stays the source of truth for *what* to build (project
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

Auth/transport (contract §5): a direct in-cluster JSON-RPC call reusing the same
client-config surface cidx already uses for the delegated clone backend
(``WorkspaceMcpConfig`` — endpoint + bearer api_key), pointed at the sandbox-mcp
service. EVO-65365: the endpoint this indexer receives is resolved by
``scip_backend_wiring._resolve_scip_sandbox_config`` from the ``scip_sandbox``
config block (primary), falling back to ``workspace_mcp`` for backward
compatibility — the DelegatedIndexer itself is source-agnostic and just consumes
the resolved endpoint. No API-GW authorizer secret is distributed to cidx; the
route is constrained by K8s NetworkPolicy + service account.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

from code_indexer.utils.git_runner import get_current_commit

from .base import IndexerResult, IndexerStatus, SCIPIndexer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from code_indexer.server.utils.config_manager import WorkspaceMcpConfig

logger = logging.getLogger(__name__)

# Sentinel for the per-instance project-version cache: distinguishes "not yet
# resolved" from a legitimately cached ``None`` (a repo with no resolvable HEAD),
# so a None/empty commit is cached too and never re-shells to git (MAJOR-1).
_PROJECT_VERSION_UNSET = object()


# The MCP tool name the sandbox image exposes for SCIP generation. sandbox-mcp
# prefixes every ``.mcp-sandbox.yaml`` tool with ``sandbox_`` — the YAML tool is
# ``scip`` → the MCP tool is ``sandbox_scip`` (contract §2).
_SCIP_TOOL_NAME = "sandbox_scip"

# HTTP request timeout for the delegated build, aligned to the local indexers'
# existing 30-minute subprocess timeout (typescript.py, java.py) and the tool's
# server-side ``timeout_s: 1800`` (contract §2a / §5).
_DEFAULT_TIMEOUT_SECONDS = 1800

# Path appended to the sandbox-mcp base endpoint for the JSON-RPC front door
# (reconciled EVO-65021 seam #2 — the authoritative sandbox contract). The
# resolved ``scip_sandbox.endpoint`` (or ``workspace_mcp.endpoint`` as a
# fallback, EVO-65365) is the sandbox-mcp *base* URL (e.g.
# ``http://sandbox-mcp:8200``); ``SandboxMcpController`` mounts the MCP handler
# at ``POST /v1/sandbox/mcp`` (URI versioning prefix 'v', v1). A dedicated param
# keeps it tunable without a config-schema change if the route ever moves.
_DEFAULT_JSONRPC_PATH = "/v1/sandbox/mcp"

# Relative path (under the configured base endpoint) probed by ``is_available``
# and the startup health check. Health is un-versioned and un-authed
# (``GET /health``, no ``/v1`` prefix) per the sandbox contract.
_DEFAULT_HEALTH_PATH = "/health"


class DelegatedIndexer(SCIPIndexer):
    """SCIP indexer that delegates the compile step to sandbox-mcp.

    A single instance handles every language: the target language is passed per
    call (``generate(..., language=...)``) and forwarded to the sandbox tool,
    mirroring how the local map already points both ``java`` and ``kotlin`` at
    one ``JavaIndexer``.
    """

    def __init__(
        self,
        config: "WorkspaceMcpConfig",
        repo_root: Path,
        *,
        tool_name: str = _SCIP_TOOL_NAME,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        jsonrpc_path: str = _DEFAULT_JSONRPC_PATH,
        health_path: str = _DEFAULT_HEALTH_PATH,
    ) -> None:
        """Initialize the delegated indexer.

        Args:
            config: Reused ``WorkspaceMcpConfig`` (endpoint + bearer api_key),
                resolved by ``_resolve_scip_sandbox_config`` from the
                ``scip_sandbox`` block (primary) or ``workspace_mcp`` (fallback,
                EVO-65365), whose ``endpoint`` is the sandbox-mcp *base* URL (e.g.
                ``http://sandbox-mcp:8200``); the JSON-RPC front door and health
                routes are appended per the sandbox contract (§5, seam #2).
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
        self._endpoint = (config.endpoint or "").rstrip("/")
        self._api_key = config.api_key or ""
        self._repo_root = Path(repo_root)
        self._tool_name = tool_name
        self._timeout = timeout_seconds
        self._jsonrpc_path = jsonrpc_path
        self._health_path = health_path
        self._request_id = 0
        # MAJOR-1: memoise the repo's git HEAD (see _project_version). repo_root is
        # fixed for the instance's life, so it is resolved at most ONCE.
        self._project_version_cache: Any = _PROJECT_VERSION_UNSET

    # ------------------------------------------------------------------ helpers
    def _requests(self) -> Any:
        """Return the lazily-imported ``requests`` module (keeps startup fast)."""
        import requests  # noqa: PLC0415

        return requests

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _rpc_url(self) -> str:
        return f"{self._endpoint}{self._jsonrpc_path}"

    def _project_version(self) -> Optional[str]:
        """Return the repo's git HEAD commit, memoised per instance (MAJOR-1).

        ``get_current_commit`` shells to git (``git rev-parse HEAD``) and, because
        ``repo_root`` is fixed at construction, returns the SAME HEAD on every
        ``generate()`` call. Resolve it AT MOST ONCE and cache the result — even a
        ``None``/empty commit is cached (via ``_PROJECT_VERSION_UNSET``) so a repo
        with no resolvable HEAD does not re-shell to git per project. Collapses the
        old N-git-calls-per-build (one per project) down to one.
        """
        if self._project_version_cache is _PROJECT_VERSION_UNSET:
            self._project_version_cache = get_current_commit(self._repo_root)
        return cast(Optional[str], self._project_version_cache)

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
        project_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the ``tools/call`` request for ``sandbox_scip``.

        Puts EXACTLY ONE of ``workspace_id`` / ``golden_alias`` into
        ``arguments`` (never both, never neither — ``generate`` enforces the XOR
        before calling here). Everything else is repo-root-relative and
        unchanged across both addressing modes.

        EVO-65365: ``allow_degenerate`` is added ONLY when True (a golden repo
        opts into the sandbox's locals-only fallback for a bare/unbuilt repo);
        omitted otherwise so a non-golden request is byte-identical to before.

        EVO-65365: ``project_version`` (the repo's git HEAD commit) is added when
        non-empty. The sandbox wrapper passes it as ``--project-version`` so scip
        stops shelling to git for the version (git-mcp owns git); the field is
        harmless if the sandbox ignores it. Omitted when empty/None.
        """
        self._request_id += 1
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
        if project_version:
            arguments["project_version"] = project_version
        return {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": self._tool_name,
                "arguments": arguments,
            },
        }

    @staticmethod
    def _extract_run_result(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Pull the ``SandboxRunResult`` envelope out of a JSON-RPC response.

        Handles the three shapes the tool-exec path may return (contract §2c):
        the envelope inline under ``result``, MCP ``structuredContent``, or a
        JSON string in ``result.content[0].text``. Returns ``None`` when the
        payload is a JSON-RPC error or the envelope cannot be located (→ the
        caller treats it as a failure).
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("error") is not None:
            return None

        result = payload.get("result", payload)
        if isinstance(result, dict):
            if "exit_code" in result:
                return result
            structured = result.get("structuredContent")
            if isinstance(structured, dict) and "exit_code" in structured:
                return structured
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                text = first.get("text") if isinstance(first, dict) else None
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError):
                        return None
                    if isinstance(parsed, dict) and "exit_code" in parsed:
                        return parsed
        return None

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
                "DelegatedIndexer has no endpoint configured (set "
                "scip_sandbox.endpoint, or workspace_mcp.endpoint as a fallback, "
                "pointed at sandbox-mcp).",
            )

        try:
            project_path = self._relative(project_dir)
            output_rel = self._relative(output_dir)
        except ValueError as exc:
            return self._failure(
                start_time,
                f"project_dir/output_dir not under repo_root "
                f"{self._repo_root}: {exc}",
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

        # EVO-65365: send the repo's git HEAD as project_version so the sandbox
        # wrapper passes it as ``--project-version`` and scip stops shelling to git
        # for the version (git-mcp owns git). Reuses the existing commit helper
        # (also used by scip_lifecycle for the alias@commit dedupe key) — NO new
        # git subprocess here. MAJOR-1: memoised on the instance so the git call
        # runs at most once per indexer, not once per generate(). Best-effort:
        # omitted from the request when empty/None.
        project_version = self._project_version()

        request = self._build_request(
            workspace_id=workspace_id,
            golden_alias=golden_alias,
            project_path=project_path,
            build_system=build_system,
            language=language,
            output_dir=output_rel,
            allow_degenerate=allow_degenerate,
            project_version=project_version,
        )

        requests = self._requests()
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
            response = requests.post(
                self._rpc_url(),
                json=request,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            return self._failure(
                start_time,
                f"sandbox_scip request to '{self._rpc_url()}' failed: {exc}",
            )

        if response.status_code >= 300:
            body = ""
            try:
                body = response.text[:500]
            except Exception:  # pragma: no cover - defensive
                pass
            return self._failure(
                start_time,
                f"sandbox_scip failed (HTTP {response.status_code}): {body}",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return self._failure(
                start_time, f"sandbox_scip returned non-JSON response: {exc}"
            )

        run_result = self._extract_run_result(payload)
        if run_result is None:
            return self._failure(
                start_time,
                f"sandbox_scip response contained no SandboxRunResult envelope: "
                f"{json.dumps(payload)[:500]}",
            )

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

        # cidx owns the output path; it never trusts an echoed one.
        scip_file = output_dir / "index.scip"
        file_exists = scip_file.exists()

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
                f"sandbox_scip exited 0 but no index.scip was written at "
                f"{scip_file}"
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
        if not self._endpoint:
            return False
        requests = self._requests()
        health_url = f"{self._endpoint}{self._health_path}"
        try:
            resp = requests.get(health_url, headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            logger.warning(
                "DelegatedIndexer: health probe to '%s' failed: %s",
                health_url,
                exc,
            )
            return False
        return bool(resp.status_code < 300)

    def get_version(self) -> Optional[str]:
        """Return None — the remote indexer version is not locally determinable.

        (The wrapper reports its ``indexer_version`` in the observability JSON
        line, which is logged from ``output`` but is not load-bearing.)
        """
        return None
