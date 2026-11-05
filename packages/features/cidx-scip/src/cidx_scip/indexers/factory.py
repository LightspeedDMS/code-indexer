"""SCIP indexer-backend factory (EVO-65021).

Mirrors ``CloneBackendFactory`` (``server/storage/shared/clone_backend.py``):
turns a ``scip_backend`` config value into the ``language -> SCIPIndexer`` map
injected into ``SCIPGenerator``.

* ``"local"`` (default) → today's subprocess indexers, one per language. This is
  the canonical home of the local map (previously inlined in
  ``SCIPGenerator.__init__``), so ``local`` behaviour is unchanged.
* ``"delegated-plugin"`` → a single :class:`PluginScipIndexer` shared across
  every language, over a plugin's ``ScipExecutionBackend`` (which returns the
  index bytes; see ``scip/delegation_api.py``).

``"delegated"`` (sandbox-mcp's ``sandbox_scip`` writing ``index.scip`` onto the
shared golden volume) was removed; :data:`REMOVED_SCIP_BACKENDS` carries the
migration message startup fails with.

The factory itself does NOT health-check or read ``ServerConfig`` — that is the
startup wiring's job (``server/startup/scip_backend_wiring.py``,
``build_scip_generator``), exactly as ``build_snapshot_manager`` wraps
``CloneBackendFactory``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from .base import SCIPIndexer
from .csharp import CSharpIndexer
from .plugin_backend import PluginScipIndexer
from .go import GoIndexer
from .java import JavaIndexer
from .python import PythonIndexer
from .typescript import TypeScriptIndexer
from ..delegation_api import ScipExecutionBackend

#: The plugin backend: a ``code_indexer.scip_backend`` plugin returns the index
#: bytes and cidx writes them under the index root (no shared volume).
SCIP_BACKEND_DELEGATED_PLUGIN = "delegated-plugin"
#: Every backend that builds golden repos off-pod (alias-addressed, queued,
#: never a local build). Shared by the startup wiring and the SCIP funnel.
DELEGATED_SCIP_BACKENDS: Tuple[str, ...] = (SCIP_BACKEND_DELEGATED_PLUGIN,)
#: Backend values that existed once, each mapped to what to set instead. Startup
#: refuses them with this message rather than the generic unknown-value one, so
#: an un-migrated deployment names its own fix.
REMOVED_SCIP_BACKENDS: Mapping[str, str] = {
    "delegated": (
        "scip_backend='delegated' (sandbox-mcp's sandbox_scip writing index.scip "
        "onto the shared golden volume) was removed. Set "
        "scip_backend='delegated-plugin', which needs the cidx-content-backend "
        "SCIP plugin (code_indexer.scip_backend entry point) in the image, "
        "CIDX_SCIP_SANDBOX_MCP_URL on the cidx pod, and SCIP_RUN_SANDBOX_IMAGE on "
        "sandbox-mcp; or set scip_backend='local'. Drop scip_build_stateless: "
        "it has no effect any more."
    ),
}


def is_delegated_scip_backend(backend_type: Optional[str]) -> bool:
    """True for a backend that builds off-pod (see ``DELEGATED_SCIP_BACKENDS``)."""
    return backend_type in DELEGATED_SCIP_BACKENDS


class ScipBackendFactory:
    """Creates the ``language -> SCIPIndexer`` map for a given scip backend."""

    _VALID_BACKENDS = ("local",) + DELEGATED_SCIP_BACKENDS
    DELEGATED_PLUGIN = SCIP_BACKEND_DELEGATED_PLUGIN

    # Languages the delegated-plugin backend can build. Kept in sync with the
    # local map keys and ``discovery.BUILD_FILE_MAPPINGS`` languages (§2b).
    _DELEGATED_LANGUAGES = (
        "java",
        "kotlin",
        "typescript",
        "python",
        "csharp",
        "go",
    )

    @staticmethod
    def create_local_indexer_map() -> Dict[str, SCIPIndexer]:
        """Return the canonical local indexer map (one subprocess indexer/lang).

        ``kotlin`` shares ``JavaIndexer`` (scip-java handles both). NOTE: keep
        this in sync with the inline default map in ``SCIPGenerator.__init__``;
        the two cannot share one builder without a generator<->factory import
        cycle, and existing tests patch the indexer classes at the *generator*
        module, so the generator keeps its own inline copy.
        """
        return {
            "java": JavaIndexer(),
            "kotlin": JavaIndexer(),  # Kotlin uses scip-java
            "typescript": TypeScriptIndexer(),
            "python": PythonIndexer(),
            "csharp": CSharpIndexer(),
            "go": GoIndexer(),
        }

    @staticmethod
    def create_indexer_map(
        scip_backend_type: str,
        *,
        repo_root: Optional[Path] = None,
        execution_backend: Optional[ScipExecutionBackend] = None,
    ) -> Dict[str, SCIPIndexer]:
        """Build the indexer map for *scip_backend_type*.

        Parameters
        ----------
        scip_backend_type:
            ``"local"`` or ``"delegated-plugin"``.
        repo_root:
            Golden-repo root (== sandbox ``/workspace``). Required for
            ``"delegated-plugin"`` so the indexer can derive workspace-relative
            paths.
        execution_backend:
            The plugin's ``ScipExecutionBackend``; required for
            ``"delegated-plugin"`` (resolved by the startup wiring).

        Raises
        ------
        ValueError
            On an unknown backend type or a missing required argument.
        """
        if scip_backend_type == "local":
            return ScipBackendFactory.create_local_indexer_map()

        if scip_backend_type == SCIP_BACKEND_DELEGATED_PLUGIN:
            if execution_backend is None:
                raise ValueError(
                    "execution_backend is required when "
                    "scip_backend_type='delegated-plugin'"
                )
            if repo_root is None:
                raise ValueError(
                    "repo_root is required when scip_backend_type='delegated-plugin'"
                )
            return ScipBackendFactory._shared(
                PluginScipIndexer(execution_backend, Path(repo_root))
            )

        raise ValueError(
            f"Unsupported scip_backend type: '{scip_backend_type}'. "
            f"Valid options: {', '.join(ScipBackendFactory._VALID_BACKENDS)}"
        )

    @staticmethod
    def _shared(indexer: SCIPIndexer) -> Dict[str, SCIPIndexer]:
        """Every delegated language mapped to one instance (language is passed
        per call), mirroring the local map's java/kotlin sharing."""
        return {
            language: indexer for language in ScipBackendFactory._DELEGATED_LANGUAGES
        }
