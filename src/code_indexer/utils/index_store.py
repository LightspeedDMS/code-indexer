"""Where a repository's cidx index state lives.

cidx keeps a repository's indexes -- vectors, the FTS and trigram indexes, SCIP
databases, metadata and per-repo config -- under ``<repo>/.code-indexer/``. That
couples cidx's own state to the repository volume: removing the repo mount
(the content seam) would delete the index with the checkout, and every pod that
reads an index needs the volume the repository lives on.

The decision (content seam plan, phase 5) is to abstract the state out rather
than move it to another volume. The index engines -- hnswlib graphs, SQLite
stores, tantivy segments -- are embedded libraries that need a local
filesystem, so the abstraction is over WHERE an index lives and, later, how it
is versioned:

1. (this module) callers ask an ``IndexStore`` for a repository's index root
   instead of joining ``.code-indexer`` onto the repo path. The only backend is
   ``LocalIndexStore``, which answers exactly that join -- no behaviour change.
   tests/unit/utils/test_index_store.py fails the build on any new
   direct derivation, so the remaining ones can only migrate behind this seam.
2. indexes as versioned artifacts: built in local scratch, published as an
   immutable snapshot, checked out to a node-local cache by readers.
3. an object-store backend, with the alias -> version pointer in Postgres.

Kept free of server imports: plain CLI indexing uses it too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Union

#: The index directory's name inside a repository under the local backend.
#: Exported for the code that must SKIP it while walking or searching a
#: repository (ripgrep globs, tree walkers): while indexes live in the
#: repository, every reader of the repository has to step around them.
INDEX_DIRNAME = ".code-indexer"


class IndexStore(Protocol):
    """Answers where a repository's cidx state is, for this process to use."""

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        """The directory holding ``repo_path``'s indexes, config and metadata."""
        ...


class LocalIndexStore:
    """Today's layout: the index lives inside the repository it describes."""

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        return Path(repo_path) / INDEX_DIRNAME


_DEFAULT: IndexStore = LocalIndexStore()


def get_index_store() -> IndexStore:
    """The index store this process uses. Only the local backend exists yet."""
    return _DEFAULT


def index_root(repo_path: Union[str, Path]) -> Path:
    """Shorthand for ``get_index_store().index_root(repo_path)``."""
    return get_index_store().index_root(repo_path)
