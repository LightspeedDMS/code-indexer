"""SCIP indexer-backend factory (EVO-65021).

Mirrors ``CloneBackendFactory`` (``server/storage/shared/clone_backend.py``):
turns a ``scip_backend`` config value into the ``language -> SCIPIndexer`` map
injected into ``SCIPGenerator``.

* ``"local"`` (default) → today's subprocess indexers, one per language. This is
  the canonical home of the local map (previously inlined in
  ``SCIPGenerator.__init__``), so ``local`` behaviour is unchanged.
* ``"delegated"`` → a single :class:`DelegatedIndexer` shared across every
  language (language is passed per call), addressed at the configured endpoint.
* ``"delegated-plugin"`` → a single :class:`PluginScipIndexer` shared across
  every language, over a plugin's ``ScipExecutionBackend`` (which returns the
  index bytes; see ``scip/delegation_api.py``).

The factory itself does NOT health-check or read ``ServerConfig`` — that is the
startup wiring's job (``server/startup/scip_backend_wiring.py``,
``build_scip_generator``), exactly as ``build_snapshot_manager`` wraps
``CloneBackendFactory``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from .base import SCIPIndexer
from .csharp import CSharpIndexer
from code_indexer.utils.remote_endpoint import RemoteServiceEndpoint
from .delegated import DelegatedIndexer
from .plugin_backend import PluginScipIndexer
from .go import GoIndexer
from .java import JavaIndexer
from .python import PythonIndexer
from .typescript import TypeScriptIndexer
from ..delegation_api import ScipExecutionBackend

#: Today's delegated backend: sandbox-mcp's deprecated ``sandbox_scip`` writes
#: ``index.scip`` into the shared golden volume.
SCIP_BACKEND_DELEGATED = "delegated"
#: The plugin backend: a ``code_indexer.scip_backend`` plugin returns the index
#: bytes and cidx writes them under the index root (no shared volume).
SCIP_BACKEND_DELEGATED_PLUGIN = "delegated-plugin"
#: Every backend that builds golden repos off-pod (alias-addressed, queued,
#: never a local build). Shared by the startup wiring and the SCIP funnel.
DELEGATED_SCIP_BACKENDS: Tuple[str, ...] = (
    SCIP_BACKEND_DELEGATED,
    SCIP_BACKEND_DELEGATED_PLUGIN,
)


def is_delegated_scip_backend(backend_type: Optional[str]) -> bool:
    """True for a backend that builds off-pod (see ``DELEGATED_SCIP_BACKENDS``)."""
    return backend_type in DELEGATED_SCIP_BACKENDS


class ScipBackendFactory:
    """Creates the ``language -> SCIPIndexer`` map for a given scip backend."""

    _VALID_BACKENDS = ("local",) + DELEGATED_SCIP_BACKENDS
    DELEGATED_PLUGIN = SCIP_BACKEND_DELEGATED_PLUGIN

    # Languages the delegated backend can build. Kept in sync with the local map
    # keys and with ``discovery.BUILD_FILE_MAPPINGS`` languages (contract §2b).
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
        endpoint_config: Optional[RemoteServiceEndpoint] = None,
        execution_backend: Optional[ScipExecutionBackend] = None,
    ) -> Dict[str, SCIPIndexer]:
        """Build the indexer map for *scip_backend_type*.

        Parameters
        ----------
        scip_backend_type:
            ``"local"``, ``"delegated"`` or ``"delegated-plugin"``.
        repo_root:
            Golden-repo root (== sandbox ``/workspace``). Required for
            ``"delegated"`` so the indexer can derive workspace-relative paths.
        endpoint_config:
            Anything exposing ``endpoint`` and ``api_key`` for the service that
            performs the build (see ``utils.remote_endpoint``); required for
            ``"delegated"``. Which config key supplies it is the startup
            wiring's concern, not this factory's.
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

        if scip_backend_type == SCIP_BACKEND_DELEGATED:
            if endpoint_config is None:
                raise ValueError(
                    "endpoint_config is required when scip_backend_type='delegated'"
                )
            if repo_root is None:
                raise ValueError(
                    "repo_root is required when scip_backend_type='delegated'"
                )
            delegated = DelegatedIndexer(
                config=endpoint_config, repo_root=Path(repo_root)
            )
            return ScipBackendFactory._shared(delegated)

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
