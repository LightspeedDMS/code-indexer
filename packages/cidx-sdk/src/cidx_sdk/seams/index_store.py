"""The index store seam: where a repository's cidx index state lives.

Selected by ``index_store_backend``. ``local`` keeps the index inside the
repository (``<repo>/.code-indexer``); ``delegated`` publishes built indexes as
immutable versions to an artifact service, through the
``code_indexer.index_artifacts`` entry point (workspace-mcp's index store), and
checks versions out for readers.

Entry-point group ``code_indexer.index_artifacts``. The entry named ``local``
is an :class:`IndexStore` itself (``cidx-backend-workspace``); a delegate
registered there is the artifact service's CLIENT, which the core wraps in its
publishing store (``DelegatedIndexStore``): the version bookkeeping (aliases,
compare-and-swap bases) is the core's, the transport is the delegate's.

Moved from ``code_indexer.utils.index_store``, which re-exports it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, Union

#: The index directory's name inside a repository under the local backend.
INDEX_DIRNAME = ".code-indexer"


def index_root_for(repo_path: Union[str, Path]) -> Path:
    """Where *repo_path*'s index is BUILT and read on this pod: the same
    path under every implementation (a delegated store builds there too and
    publishes from it), so path questions need no implementation at all."""
    return Path(repo_path) / INDEX_DIRNAME


class IndexStoreDeleteUnsupportedError(RuntimeError):
    """The index store's artifact client cannot delete published versions."""


class IndexStore(Protocol):
    """Answers where a repository's cidx state is, for this process to use.

    An index built under ``index_root`` is ``publish``ed as an immutable
    version, and ``checkout`` puts a version where a reader can open it.
    """

    @property
    def is_local(self) -> bool:
        """True when indexes are read where they are built (no artifacts)."""
        ...

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        """The directory holding ``repo_path``'s indexes, config and metadata."""
        ...

    def publish(self, repo_path: Union[str, Path], built_dir: Path) -> Optional[str]:
        """Publish the index built in ``built_dir``; the new version, or None."""
        ...

    def checkout(
        self, repo_path: Union[str, Path], into: Path, version: Optional[str] = None
    ) -> Path:
        """Make a version readable under ``into``; the index root to read."""
        ...

    def delete(self, repo_path: Union[str, Path]) -> Optional[int]:
        """Delete every published version of ``repo_path``'s index.

        The number of versions deleted (0 when there were none: idempotent),
        or None when this store publishes nothing. Raises when the versions
        could not be deleted -- never reports a deletion that did not happen.
        """
        ...
