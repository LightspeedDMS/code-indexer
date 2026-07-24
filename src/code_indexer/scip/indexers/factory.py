"""SCIP indexer-backend factory (EVO-65021).

Mirrors ``CloneBackendFactory`` (``server/storage/shared/clone_backend.py``):
turns a ``scip_backend`` config value into the ``language -> SCIPIndexer`` map
injected into ``SCIPGenerator``.

* ``"local"`` (default) → today's subprocess indexers, one per language. This is
  the canonical home of the local map (previously inlined in
  ``SCIPGenerator.__init__``), so ``local`` behaviour is unchanged.
* ``"delegated"`` → a single :class:`DelegatedIndexer` shared across every
  language (language is passed per call), addressed at the sandbox-mcp endpoint.

The factory itself does NOT health-check or read ``ServerConfig`` — that is the
startup wiring's job (``server/startup/scip_backend_wiring.py``,
``build_scip_generator``), exactly as ``build_snapshot_manager`` wraps
``CloneBackendFactory``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from .base import SCIPIndexer
from .csharp import CSharpIndexer
from .delegated import DelegatedIndexer
from .go import GoIndexer
from .java import JavaIndexer
from .python import PythonIndexer
from .typescript import TypeScriptIndexer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from code_indexer.server.utils.config_manager import WorkspaceMcpConfig


class ScipBackendFactory:
    """Creates the ``language -> SCIPIndexer`` map for a given scip backend."""

    _VALID_BACKENDS = ("local", "delegated")

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
        workspace_mcp_config: Optional["WorkspaceMcpConfig"] = None,
    ) -> Dict[str, SCIPIndexer]:
        """Build the indexer map for *scip_backend_type*.

        Parameters
        ----------
        scip_backend_type:
            ``"local"`` or ``"delegated"``.
        repo_root:
            Golden-repo root (== sandbox ``/workspace``). Required for
            ``"delegated"`` so the indexer can derive workspace-relative paths.
        workspace_mcp_config:
            ``WorkspaceMcpConfig`` (endpoint + api_key) pointed at sandbox-mcp;
            required for ``"delegated"``.

        Raises
        ------
        ValueError
            On an unknown backend type or a missing required argument.
        """
        if scip_backend_type == "local":
            return ScipBackendFactory.create_local_indexer_map()

        if scip_backend_type == "delegated":
            if workspace_mcp_config is None:
                raise ValueError(
                    "workspace_mcp_config is required when "
                    "scip_backend_type='delegated'"
                )
            if repo_root is None:
                raise ValueError(
                    "repo_root is required when scip_backend_type='delegated'"
                )
            delegated = DelegatedIndexer(
                config=workspace_mcp_config, repo_root=Path(repo_root)
            )
            # Every language shares the one instance (language is passed per
            # call), mirroring the local map's java/kotlin sharing.
            return {
                language: delegated
                for language in ScipBackendFactory._DELEGATED_LANGUAGES
            }

        raise ValueError(
            f"Unsupported scip_backend type: '{scip_backend_type}'. "
            f"Valid options: {', '.join(ScipBackendFactory._VALID_BACKENDS)}"
        )
