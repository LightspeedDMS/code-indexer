"""
X-Ray execution backend wiring, run by the feature's ``on_startup``.

The X-Ray sibling of the core's ``ripgrep_backend_wiring``. Selects where
X-Ray compiles and runs caller-supplied evaluators, by the ``xray_backend``
setting -- a bootstrap key of the feature's config section (see
``cidx_xray/execution_backend.py`` for the seam). The content-seam pieces it
consults (pod-local repositories, the content root, the delegated refusal,
the seam plugin resolver) come from the server platform
(``cidx_xray._platform``).

:class:`FeaturePlacementHost` is what the feature installs in the kernel's
``XRAY_PLACEMENT_HOST`` slot, so every ``RustNativeBackend`` asks it where a
request executes.

Fail-fast policy (mirrors the git, content and ripgrep wiring):
- ``local``     : the evaluator is compiled and run in this pod by xray-cli.
                  This is the DEFAULT, so an unset or absent config behaves
                  exactly as cidx does today. Nothing is built here: each
                  ``RustNativeBackend`` executes in-process with its own caches.
- ``delegated`` : the evaluator runs wherever the X-Ray backend PLUGIN runs
                  it (``code_indexer.xray_backend`` entry point, or
                  ``CIDX_XRAY_BACKEND``; see ``backend_plugin``)
                  -- evo-sandbox's runs it in sandbox-mcp. There is
                  deliberately **no fallback to local**: delegation exists so
                  caller-supplied code never runs in the cidx pod, and a pod
                  built for it may hold neither the repositories nor rustc. No
                  plugin installed, a plugin that fails to load or construct,
                  or one that is not an ``XrayExecutionBackend`` stops startup.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.

Per repository, :func:`xray_execution_backend_for` -- the ONE place that
decides where a given request executes -- adjusts that selection in two cases
(see its docstring): under delegation a pod-local repository's files are read
here and sent INLINE to the delegated sandbox (the evaluator still never runs
here), and an in-pod execution against a repository this pod does not hold is
refused.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from cidx_xray._platform import capabilities, server_platform
from cidx_xray.execution_backend import XrayExecutionBackend

logger = logging.getLogger(__name__)

VALID_XRAY_BACKENDS = ("local", "delegated")

_NO_LOCAL_FALLBACK = (
    "Refusing to fall back to local execution: delegation exists so "
    "caller-supplied evaluators never compile or run in the cidx pod."
)


def build_configured_xray_execution_backend(
    config: Any,
) -> Optional[XrayExecutionBackend]:
    """The X-Ray execution backend *config* selects; None means local.

    ``config`` is read via ``getattr`` so a partial/bootstrap config still
    gets the ``local`` default rather than an AttributeError.

    Raises:
        RuntimeError: ``xray_backend`` is unrecognised, or is ``delegated``
            and no usable plugin backend could be built.
    """
    backend_type = getattr(config, "xray_backend", "local") or "local"

    if backend_type not in VALID_XRAY_BACKENDS:
        raise RuntimeError(
            f"Unknown xray_backend {backend_type!r}. "
            f"Valid values are {VALID_XRAY_BACKENDS}."
        )

    if backend_type == "delegated":
        return _build_delegated_backend()

    logger.info("XrayExecutionBackend: using local xray-cli execution (this pod)")
    _warn_local_execution_under_delegated_content(config)
    return None


def _warn_local_execution_under_delegated_content(config: Any) -> None:
    """Say loudly, at startup, that in-pod X-Ray needs the repositories here.

    ``content_backend=delegated`` with ``xray_backend=local`` is a legitimate
    rollout state: a pod that still has the repository mount reads candidates
    through the seam and runs xray-cli here, over the files on its disk. It is
    NOT refused at startup, because no cheap startup probe can tell "mount
    present" from "mount gone" reliably (an empty registry, a cidx-meta-only
    registry or a not-yet-cloned repo all look like "absent") -- and refusing
    a pod that has the mount would remove a working feature. Instead every
    request is checked against the files it is about to read
    (:func:`xray_execution_backend_for`) and refused, before xray-cli runs,
    when this pod does not hold them.
    """
    content_backend = getattr(config, "content_backend", "local") or "local"
    if content_backend == "local":
        return
    logger.warning(
        "XrayExecutionBackend: xray_backend='local' with content_backend=%r: "
        "xray-cli runs in THIS pod and reads candidate files from THIS pod's "
        "disk. X-Ray works only for repositories whose files are on this pod "
        "(a repository mount, or pod-local local:// repositories); requests "
        "against any other repository are refused. Set "
        "xray_backend='delegated' to execute where the repositories are.",
        content_backend,
    )


def _build_delegated_backend() -> XrayExecutionBackend:
    """The plugin's backend, or RuntimeError -- never None, never local."""
    # Imported lazily so the local default never imports the plugin seam.
    from cidx_xray.server.backend_plugin import (
        ENTRY_POINT_GROUP,
        ENV_VAR,
        resolve_xray_backend,
    )

    try:
        backend = resolve_xray_backend()
    except Exception as exc:
        raise RuntimeError(
            "xray_backend='delegated' selected but the X-Ray backend plugin "
            f"could not be loaded or constructed: {type(exc).__name__}: {exc}. "
            f"{_NO_LOCAL_FALLBACK}"
        ) from exc
    if backend is None:
        raise RuntimeError(
            "xray_backend='delegated' selected but no X-Ray backend plugin is "
            "installed. Install a plugin registering a factory on the "
            f"{ENTRY_POINT_GROUP} entry point (cidx-content-backend >= 0.7.0), "
            f"or set {ENV_VAR}='package.module:factory'. {_NO_LOCAL_FALLBACK}"
        )
    if not isinstance(backend, XrayExecutionBackend):
        raise RuntimeError(
            "xray_backend='delegated' selected but the X-Ray backend plugin "
            f"returned a {type(backend).__name__}, which has no "
            f"execute_scan/execute_graph_analysis. {_NO_LOCAL_FALLBACK}"
        )
    logger.info(
        "XrayExecutionBackend: using delegated execution (%s)",
        type(backend).__name__,
    )
    return backend


class FeaturePlacementHost:
    """``XRAY_PLACEMENT_HOST``: where an X-Ray evaluator is executed.

    The configured backend is the one ``on_startup`` built
    (:func:`server_xray_execution_backend`; None under ``xray_backend=local``,
    so ``RustNativeBackend`` executes in-process, as before); the
    per-request decision is :func:`xray_execution_backend_for`. Both are
    looked up when called.
    """

    def configured_backend(self) -> Optional[XrayExecutionBackend]:
        return server_xray_execution_backend()

    def backend_for(
        self,
        configured: Optional[XrayExecutionBackend],
        repo_path: Optional[str],
        rel_paths: Sequence[str],
    ) -> Optional[XrayExecutionBackend]:
        return xray_execution_backend_for(configured, repo_path, rel_paths)


def server_xray_execution_backend() -> Optional[XrayExecutionBackend]:
    """The backend the running feature built at startup, or None.

    None outside a server process (CLI, tests), before the feature started,
    and under ``xray_backend=local``; ``RustNativeBackend`` then executes
    in-process, as before.
    """
    from cidx_xray.server import runtime

    return runtime.EXECUTION_BACKEND.get()


#: The feature named in an in-pod X-Ray refusal.
_LOCAL_XRAY_FEATURE = "X-Ray with xray_backend='local'"


def xray_execution_backend_for(
    configured: Optional[XrayExecutionBackend],
    repo_path: Optional[str],
    rel_paths: Sequence[str],
) -> Optional[XrayExecutionBackend]:
    """Where ONE X-Ray request over *repo_path* executes; None means this pod.

    The single per-repository decision, made by ``RustNativeBackend`` before
    every scan and graph analysis. *configured* is the backend the server
    selected (``xray_backend``), or None for in-pod execution; *rel_paths* are
    the candidate files, relative to *repo_path*, that execution will read.

    * delegated execution, pod-local repository (a ``local://`` repo other
      than cidx-meta, e.g. Langfuse trace folders; ``is_pod_local_golden_path``,
      shared with the content seam and the ripgrep target) -> the configured
      backend, wrapped in ``PodLocalInlineXrayBackend``: its files exist only
      here and the plugin's sandbox cannot mount them, so this pod READS the
      candidates (its own data; Phase 1 already reads such a repo locally) and
      sends their bytes to the sandbox, batched under the plugin's limits. The
      caller's evaluator never runs in this pod. Golden (cidx-meta, git) and
      activated repositories keep the configured backend unchanged.
    * in-pod execution while the content backend is delegated -> the
      candidate files are probed on this pod's disk. If NONE of them exists,
      and the repository is not pod-local, this pod does not hold the
      repository (no mount): refused with ``DelegatedRepositoryUnsupportedError``
      before xray-cli runs, so no partial result is produced. One file found is
      enough to run: the mount is there, and a single missing file is a per-file
      xray-cli error exactly as on a local pod.
    * everything else (``xray_backend=local`` with local content, the CLI,
      tests without a running server) -> unchanged: *configured*.

    Blocking (a registry read, filesystem stats): call it off the event loop.

    Raises:
        DelegatedRepositoryUnsupportedError: in-pod execution under a
            delegated content backend, and this pod holds none of the files.
    """
    if repo_path is None:
        return configured
    if configured is not None:
        if server_platform().is_pod_local_golden_path(repo_path):
            from cidx_xray.inline_execution import PodLocalInlineXrayBackend

            logger.info(
                "X-Ray: pod-local repository %s -- candidate files read here and "
                "sent inline to the delegated sandbox",
                repo_path,
            )
            return PodLocalInlineXrayBackend(configured)
        return _ContentRootXrayBackend.wrapping(configured, repo_path)
    _refuse_in_pod_execution_without_repository(repo_path, rel_paths)
    return None


def _refuse_in_pod_execution_without_repository(
    repo_path: str, rel_paths: Sequence[str]
) -> None:
    """Refuse in-pod execution under delegated content when no candidate is here.

    A presence probe of THIS pod's disk (stat only, stopping at the first file
    found): it never reads repository content and never turns an absence into
    an answer -- absence is the refusal.
    """
    if not rel_paths or not capabilities.content_seam_delegated():
        return
    root = Path(repo_path)
    if any((root / rel).is_file() for rel in rel_paths):
        return
    platform = server_platform()
    if platform.is_pod_local_golden_path(repo_path):
        return
    raise platform.delegated_repository_unsupported(
        _LOCAL_XRAY_FEATURE,
        f"xray-cli runs in this pod and reads the candidate files from its "
        f"disk, but none of the {len(rel_paths)} candidate file(s) exists "
        f"under {repo_path}; set xray_backend='delegated' to execute where "
        "the repository is",
    )


class _ContentRootXrayBackend:
    """A delegated X-Ray backend asked about the repository's CONTENT root.

    Phase 1 (candidate selection) reads a golden repo through the
    content seam at ``golden_repo_content_root`` -- a versioned snapshot maps
    to its base clone. Phase 2 used to hand the delegated backend the raw
    snapshot path, which under a delegated IndexStore holds only the index
    state: the sandbox mounted an empty tree, xray-cli parsed nothing, and
    every scan reported "completed, 0 matches". Both phases now read the same
    tree. Results carry repository-relative paths, so nothing maps back.
    """

    def __init__(self, inner: XrayExecutionBackend, content_root: str) -> None:
        self._inner = inner
        self._content_root = content_root

    @classmethod
    def wrapping(
        cls, configured: XrayExecutionBackend, repo_path: str
    ) -> XrayExecutionBackend:
        """*configured*, asked at the content root when that differs from
        *repo_path* under a delegated content backend; else unchanged."""
        if not capabilities.content_seam_delegated():
            return configured
        content_root = str(
            server_platform().golden_repo_content_root(repo_path, consumer="X-Ray")
        )
        if content_root == str(repo_path):
            return configured
        return cls(configured, content_root)

    def execute_scan(self, request: Any) -> Any:
        return self._inner.execute_scan(
            dataclasses.replace(request, repo_path=self._content_root)
        )

    def execute_graph_analysis(self, request: Any) -> Any:
        return self._inner.execute_graph_analysis(
            dataclasses.replace(request, repo_root=self._content_root)
        )
