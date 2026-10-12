"""Where the outbox sweeper writes the alias / write-mode marker files.

A sink is the file-write path, and nothing else: ``put`` an absolute path's
bytes, or ``delete`` it. ANY exception means "not written" -- the sweeper then
keeps the outbox row and retries on a later sweep. That is the whole contract,
shaped so the workspace-mcp sink is a thin adapter over the content writer the
workspace-mcp write path will expose (``put(path, data)`` / ``delete(path)``,
raising an error whose ``.code`` is ECONFLICT/EINVAL/EFORBIDDEN/EUNAVAILABLE/
ETRANSPORT).

* ``local-files``: this pod's own disk -- correct for today's fleet, where the
  golden-repos volume is mounted on every pod.
* ``workspace-mcp``: the mount-less fleet. Its file-write path does not exist
  yet, so :func:`build_sink` returns an :class:`UnavailableSink` that fails
  every call LOUDLY (an ERROR at build time, and a raise per replay): rows are
  kept, ``cidx_metadata_outbox_depth`` stays visible, nothing is dropped.
  :class:`ContentWriterSink` is the adapter that will replace it once the
  write path publishes; ``content_writer_plugin`` resolves its client.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from code_indexer.global_repos.control_plane_metadata import MetadataFiles
from code_indexer.server.utils.config_manager import (
    METADATA_OUTBOX_SINK_LOCAL_FILES,
    METADATA_OUTBOX_SINK_WORKSPACE_MCP,
)

logger = logging.getLogger(__name__)

WORKSPACE_MCP_UNAVAILABLE = (
    "no workspace-mcp write path yet: metadata_outbox_sink=workspace-mcp keeps "
    "every outbox row (cidx_metadata_outbox_depth grows) until the workspace-mcp "
    "file-write path ships; set metadata_outbox_sink=local-files on a fleet with "
    "the golden-repos volume mounted"
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
    """Adapter over a workspace-mcp content writer (``put``/``delete``).

    Unconditional writes (no ``expected_blob_sha``): the sweeper always writes
    the registry's current value, so last-writer-wins is the intent. Any error
    the writer raises propagates, which keeps the outbox row.
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


def build_sink(name: str) -> MetadataSink:
    """The sink for a ``metadata_outbox_sink`` value.

    ``workspace-mcp`` fails loud: logged at ERROR here, and every replay raises.
    """
    if name == METADATA_OUTBOX_SINK_LOCAL_FILES:
        return LocalFilesSink()
    if name == METADATA_OUTBOX_SINK_WORKSPACE_MCP:
        logger.error("Metadata outbox sink %s: %s", name, WORKSPACE_MCP_UNAVAILABLE)
        return UnavailableSink(name, WORKSPACE_MCP_UNAVAILABLE)
    reason = f"unknown metadata_outbox_sink {name!r}"
    logger.error("Metadata outbox: %s; keeping every row", reason)
    return UnavailableSink(name, reason)
