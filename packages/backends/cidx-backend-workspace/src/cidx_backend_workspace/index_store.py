"""The local index store: a repository's index lives inside it.

Registered on ``code_indexer.index_artifacts`` as ``local``: an
:class:`~cidx_sdk.seams.index_store.IndexStore` that publishes and checks out
nothing, because readers read the index where it was built. (A delegate on the
same group is an artifact-service client the core wraps; see
:mod:`cidx_sdk.seams.index_store`.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from cidx_sdk.seams.index_store import index_root_for


class LocalIndexStore:
    """Today's layout: the index lives inside the repository it describes."""

    is_local = True

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        return index_root_for(repo_path)

    def publish(self, repo_path: Union[str, Path], built_dir: Path) -> Optional[str]:
        """Nothing to publish: readers read the index where it was built."""
        return None

    def checkout(
        self, repo_path: Union[str, Path], into: Path, version: Optional[str] = None
    ) -> Path:
        """Nothing to fetch: the built index IS the readable one."""
        return self.index_root(repo_path)

    def delete(self, repo_path: Union[str, Path]) -> Optional[int]:
        """Nothing published: the index is inside the repository directory,
        so deleting that directory (golden-repo removal does) deletes it."""
        return None


def make_index_store() -> LocalIndexStore:
    """Entry point ``local`` on ``code_indexer.index_artifacts``."""
    return LocalIndexStore()
