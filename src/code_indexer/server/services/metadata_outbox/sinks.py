"""Where the outbox sweeper writes the alias / write-mode marker files.

A sink is the file-write path, and nothing else: ``put`` an absolute path's
bytes, or ``delete`` it. ANY exception means "not written" -- the sweeper then
keeps the outbox row and retries on a later sweep. That is the whole contract.

* ``local-files``: this pod's own disk -- correct for a fleet where the
  golden-repos volume is mounted on every pod.
* ``workspace-mcp``: the mount-less fleet. :class:`ContentWriterSink` writes
  through the server's content writer (the ``code_indexer.content_writer``
  seam, workspace-mcp's ``POST /content/write``) at the same absolute path,
  so workspace-mcp's ``WSM_CONTENT_WRITE_ROOTS`` must cover the golden-repos
  directory (``aliases/`` and the write-mode marker directory). With no
  writer configured (a local content backend) :func:`build_sink` returns an
  :class:`UnavailableSink` that fails every call LOUDLY (an ERROR at build
  time, and a raise per replay): rows are kept, ``cidx_metadata_outbox_depth``
  stays visible, nothing is dropped.

``metadata_outbox_sink=auto`` never reaches this module: the settings provider
resolves it to one of the two above (see ``resolve_metadata_outbox_sink``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from code_indexer.global_repos.control_plane_metadata import MetadataFiles
from code_indexer.server.utils.config_manager import (
    METADATA_OUTBOX_SINK_LOCAL_FILES,
    METADATA_OUTBOX_SINK_WORKSPACE_MCP,
)

logger = logging.getLogger(__name__)

WORKSPACE_MCP_UNAVAILABLE = (
    "no content writer configured: metadata_outbox_sink=workspace-mcp keeps "
    "every outbox row (cidx_metadata_outbox_depth grows) until one is. The "
    "writer exists only under content_backend=delegated; on a fleet with the "
    "golden-repos volume mounted set metadata_outbox_sink=local-files (or auto)"
)


class MetadataSinkUnavailableError(RuntimeError):
    """The configured sink cannot write at all."""


@runtime_checkable
class MetadataSink(Protocol):
    """The file-write path the sweeper replays through."""

    name: str

    def put(self, path: str, data: bytes) -> None:
        """Write *data* to the absolute *path*; raise on any failure."""
        ...

    def delete(self, path: str) -> None:
        """Remove the absolute *path* (missing is fine); raise on failure."""
        ...


class LocalFilesSink:
    """Writes straight to this pod's disk (durable temp-file + rename)."""

    name = METADATA_OUTBOX_SINK_LOCAL_FILES

    def put(self, path: str, data: bytes) -> None:
        MetadataFiles.write_bytes(Path(path), data)

    def delete(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)


class ContentWriterSink:
    """Adapter over the server's content writer (workspace-mcp).

    Unconditional writes (no ``expected_blob_sha`` is sent): the sweeper always
    writes the registry's CURRENT document for a key, so last-writer-wins is
    the intent. The writer takes absolute paths, as the sweeper produces them.
    A delete of an absent file is success (the writer returns False rather
    than raising). Any error the writer raises propagates, which keeps the
    outbox row.
    """

    name = METADATA_OUTBOX_SINK_WORKSPACE_MCP

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def put(self, path: str, data: bytes) -> None:
        self._writer.put(path, data)

    def delete(self, path: str) -> None:
        self._writer.delete(path)


class UnavailableSink:
    """A sink that cannot write: every call raises, so every row is kept."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def put(self, path: str, data: bytes) -> None:
        raise MetadataSinkUnavailableError(self.reason)

    def delete(self, path: str) -> None:
        raise MetadataSinkUnavailableError(self.reason)


def _server_content_writer() -> Optional[Any]:
    from code_indexer.server.startup.content_writer_wiring import (
        server_content_writer,
    )

    return server_content_writer()


def build_sink(
    name: str,
    *,
    content_writer: Callable[[], Optional[Any]] = _server_content_writer,
) -> MetadataSink:
    """The sink for a resolved ``metadata_outbox_sink`` value.

    ``workspace-mcp`` adapts the writer *content_writer* returns (the one the
    running server parked at startup); with none it fails loud: logged at
    ERROR here, and every replay raises.
    """
    if name == METADATA_OUTBOX_SINK_LOCAL_FILES:
        return LocalFilesSink()
    if name == METADATA_OUTBOX_SINK_WORKSPACE_MCP:
        writer = content_writer()
        if writer is not None:
            logger.info(
                "Metadata outbox sink %s: content writer %s",
                name,
                type(writer).__name__,
            )
            return ContentWriterSink(writer)
        logger.error("Metadata outbox sink %s: %s", name, WORKSPACE_MCP_UNAVAILABLE)
        return UnavailableSink(name, WORKSPACE_MCP_UNAVAILABLE)
    reason = f"unknown metadata_outbox_sink {name!r}"
    logger.error("Metadata outbox: %s; keeping every row", reason)
    return UnavailableSink(name, reason)
