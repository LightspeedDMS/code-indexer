"""The alias / write-mode marker FILES through the content seam (issue-64616).

The pointers themselves live in the registry (content seam step 5a). Their
file representation -- ``golden-repos/aliases/<name>.json`` and
``golden-repos/.write_mode/<name>.json`` -- is still read in three places:
the one-time startup import, the read-through on a registry miss before that
import has finished, and the ``metadata_read_source=files`` rollback, which
also writes them. :class:`MetadataFiles` does all of that on this pod's disk.

Under ``content_backend='delegated'`` the pod does not mount the golden-repos
volume, so that disk holds nothing: an import would "finish" having imported
nothing, a read-through would answer "no such alias", and a rollback would
write files nobody reads. :class:`SeamMetadataFiles` is the same file
representation where the content service holds it, at the same absolute paths: reads
and listings through the content backend, writes and deletes through the
content writer -- the shared-directory accessor cidx-meta already uses.

Failure rule, as for cidx-meta: a file that is absent is absent (None), and
a directory that is absent lists as empty; a backend that cannot be reached, or is not built yet, RAISES
:class:`MetadataFilesUnavailableError` (an ``OSError``, which the import
already reports and retries). An unreachable the content service is never "no
aliases".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from code_indexer.global_repos.control_plane_metadata import (
    MetadataFiles,
    MetadataFilesNotReadyError,
    MetadataFilesUnavailableError,
)
from code_indexer.server.services.cidx_meta_content import (
    CidxMetaUnavailableError,
    SharedCidxMeta,
    shared_cidx_meta,
)
from code_indexer.server.storage.shared.content_backend import (
    NOT_FOUND_FETCH_ERRORS,
    UNREADABLE_FETCH_ERRORS,
    is_absent_root_error,
)

logger = logging.getLogger(__name__)

_SUFFIX = ".json"

__all__ = ["MetadataFilesUnavailableError", "SeamMetadataFiles"]


class SeamMetadataFiles(MetadataFiles):
    """:class:`MetadataFiles` with the content service, not this pod's disk, as the store.

    Paths are unchanged (``dir_for`` / ``path_for``); only where the bytes are
    is different. The backend and writer are resolved on every call -- the
    startup import can run before the server has built them, and must then fail
    (and be retried), never fall back to the local disk.
    """

    def _shared(self, kind: str) -> SharedCidxMeta:
        directory = self.dir_for(kind)
        try:
            shared = shared_cidx_meta(directory)
        except CidxMetaUnavailableError as exc:
            raise MetadataFilesUnavailableError(str(exc)) from exc
        if shared is None:
            raise MetadataFilesNotReadyError(
                f"control-plane metadata files under {directory} are delegated "
                "(content_backend='delegated') but no delegated content backend "
                "is available yet; refusing to read this pod's disk"
            )
        return shared

    @staticmethod
    def _decode(name: str, data: bytes) -> Optional[Dict[str, Any]]:
        try:
            document = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Unreadable metadata file %s: %s", name, exc)
            return None
        return document if isinstance(document, dict) else None

    def _names(self, shared: SharedCidxMeta) -> List[str]:
        """The ``*.json`` names in the directory. A directory not created yet
        (``.write_mode`` exists only after the first write session) lists as
        empty, exactly as :class:`MetadataFiles` lists a missing directory --
        otherwise the startup import could never complete on a fresh pod. Any
        OTHER failure raises: an unreachable the content service is never "empty"."""
        try:
            return shared.list_names("", _SUFFIX)
        except CidxMetaUnavailableError as exc:
            if is_absent_root_error(exc):
                return []
            raise MetadataFilesUnavailableError(str(exc)) from exc

    # ── reads ───────────────────────────────────────────────────────────

    def read(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        shared = self._shared(kind)
        try:
            data = shared.read_bytes(f"{name}{_SUFFIX}")
        except FileNotFoundError:
            return None
        except CidxMetaUnavailableError as exc:
            raise MetadataFilesUnavailableError(str(exc)) from exc
        return self._decode(f"{name}{_SUFFIX}", data)

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        """Every readable document whose name starts with *prefix*, fetched in
        one batch. A file that vanished or is unreadable is simply absent; a
        backend that cannot list or fetch raises."""
        shared = self._shared(kind)
        files = [n for n in self._names(shared) if n.startswith(prefix)]
        documents: Dict[str, Dict[str, Any]] = {}
        if not files:
            return documents
        try:
            fetched = list(shared.backend.fetch(shared.root, files))
        except Exception as exc:
            raise MetadataFilesUnavailableError(
                f"fetching metadata files under {shared.root} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        for item in fetched:
            if item.error is not None:
                if item.error in NOT_FOUND_FETCH_ERRORS:
                    continue
                if item.error in UNREADABLE_FETCH_ERRORS:
                    logger.warning(
                        "Unreadable metadata file %s/%s: %s",
                        shared.root,
                        item.path,
                        item.error,
                    )
                    continue
                raise MetadataFilesUnavailableError(
                    f"unexpected error fetching {shared.root}/{item.path}: {item.error}"
                )
            document = self._decode(item.path, item.data)
            if document is not None:
                documents[item.path[: -len(_SUFFIX)]] = document
        return documents

    def names(self, kind: str) -> List[str]:
        return [n[: -len(_SUFFIX)] for n in self._names(self._shared(kind))]

    # ── writes ──────────────────────────────────────────────────────────

    def write(self, kind: str, name: str, document: Dict[str, Any]) -> None:
        try:
            self._shared(kind).put(f"{name}{_SUFFIX}", self.encode(document))
        except CidxMetaUnavailableError as exc:
            raise MetadataFilesUnavailableError(str(exc)) from exc

    def delete(self, kind: str, name: str) -> None:
        try:
            self._shared(kind).delete(f"{name}{_SUFFIX}")
        except CidxMetaUnavailableError as exc:
            raise MetadataFilesUnavailableError(str(exc)) from exc
