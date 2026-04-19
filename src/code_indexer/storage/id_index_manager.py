"""ID index manager for fast point_id to file_path mapping.

Maintains a persistent binary file mapping vector IDs to their file paths
using mmap for fast loading and minimal memory overhead.
"""

import logging
import os
import struct
from pathlib import Path
from typing import Dict
import threading

logger = logging.getLogger(__name__)

_MAX_INDEX_ENTRIES = 10_000_000
_HEADER_SIZE = 4  # bytes occupied by the uint32 entry-count field


class CorruptIDIndexError(Exception):
    """Raised when id_index.bin is detected to be corrupt or truncated.

    Callers that catch this error may trigger rebuild_from_vectors() to
    auto-repair the index from the intact vector JSON files on disk.
    """


class IDIndexManager:
    """Manages persistent ID index for fast lookups using binary format.

    Binary Format Specification:
    [num_entries: 4 bytes (uint32, little-endian)]
    For each entry:
      [id_length: 2 bytes (uint16, little-endian)]
      [id_string: variable UTF-8 bytes]
      [path_length: 2 bytes (uint16, little-endian)]
      [path_string: variable UTF-8 bytes, relative to collection]
    """

    INDEX_FILENAME = "id_index.bin"

    def __init__(self):
        """Initialize IDIndexManager."""
        self._lock = threading.RLock()  # Reentrant lock to allow nested locking

    @staticmethod
    def _read_exact(f, size: int, context: str) -> bytes:
        """Read exactly `size` bytes or raise CorruptIDIndexError."""
        data = bytes(f.read(size))
        if len(data) < size:
            raise CorruptIDIndexError(f"id_index.bin truncated: EOF reading {context}")
        return data

    @staticmethod
    def _read_u16(f, context: str) -> int:
        """Read a little-endian uint16 or raise CorruptIDIndexError."""
        return int(struct.unpack("<H", IDIndexManager._read_exact(f, 2, context))[0])

    @staticmethod
    def _read_u32(f, context: str) -> int:
        """Read a little-endian uint32 or raise CorruptIDIndexError."""
        return int(struct.unpack("<I", IDIndexManager._read_exact(f, 4, context))[0])

    @staticmethod
    def _read_utf8_string(f, length: int, context: str) -> str:
        """Read `length` UTF-8 bytes and decode them or raise CorruptIDIndexError."""
        raw = IDIndexManager._read_exact(f, length, context)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: invalid UTF-8 in {context}"
            ) from exc

    @staticmethod
    def _safe_relative_path(path_str: str, context: str) -> Path:
        """Validate that path_str is a safe relative path.

        Rejects absolute paths and paths that escape the collection directory
        via ``..`` traversal, raising CorruptIDIndexError on invalid input.
        """
        p = Path(path_str)
        if p.is_absolute():
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} is an absolute path: {path_str!r}"
            )
        # Normalise and check for escaping parent components
        try:
            normalised = Path(*p.parts)  # reconstructs without redundant separators
        except Exception:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} is not a valid path: {path_str!r}"
            )
        if ".." in normalised.parts:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} escapes collection directory: {path_str!r}"
            )
        return normalised

    def load_index(self, collection_path: Path) -> Dict[str, Path]:
        """Load ID index from disk.

        Returns:
            Dictionary mapping point IDs to absolute file paths

        Raises:
            CorruptIDIndexError: File is zero bytes, too small for the header,
                has an unreasonable entry count, or is truncated mid-entry.
                Callers should catch this and call rebuild_from_vectors().
        """
        index_file = collection_path / self.INDEX_FILENAME
        if not index_file.exists():
            return {}

        with open(index_file, "rb") as f:
            file_size = f.seek(0, 2)
            f.seek(0)

            if file_size == 0:
                raise CorruptIDIndexError(
                    "id_index.bin is zero bytes (interrupted write)"
                )
            if file_size < _HEADER_SIZE:
                raise CorruptIDIndexError(
                    f"id_index.bin too small for entry-count header ({file_size} bytes)"
                )

            num_entries = self._read_u32(f, "entry-count header")
            if num_entries > _MAX_INDEX_ENTRIES:
                raise CorruptIDIndexError(
                    f"id_index.bin has unreasonable entry count: {num_entries} "
                    f"(max {_MAX_INDEX_ENTRIES})"
                )

            id_index: Dict[str, Path] = {}
            for _ in range(num_entries):
                id_len = self._read_u16(f, "ID length")
                point_id = self._read_utf8_string(f, id_len, "ID string")
                path_len = self._read_u16(f, "path length")
                path_str = self._read_utf8_string(f, path_len, "path string")
                safe_path = self._safe_relative_path(path_str, "path string")
                id_index[point_id] = collection_path / safe_path

            return id_index

    def save_index(self, collection_path: Path, id_index: Dict[str, Path]) -> None:
        """Save ID index to disk using an atomic temp-file + os.replace pattern.

        Writes to a .bin.tmp side-car, fsyncs it, then uses os.replace() to
        atomically swap it into place.  A directory fsync follows so the rename
        survives a crash.  The original id_index.bin is never truncated until
        the new file is fully written and fsynced.

        Args:
            collection_path: Path to collection directory
            id_index: Dictionary mapping point IDs to file paths
        """
        index_file = collection_path / self.INDEX_FILENAME
        temp_file = index_file.with_suffix(".bin.tmp")

        with self._lock:
            with open(temp_file, "wb") as f:
                f.write(struct.pack("<I", len(id_index)))

                for point_id, file_path in id_index.items():
                    try:
                        relative_path = file_path.relative_to(collection_path)
                        path_str = str(relative_path)
                    except ValueError:
                        path_str = str(file_path)

                    id_bytes = point_id.encode("utf-8")
                    path_bytes = path_str.encode("utf-8")

                    f.write(struct.pack("<H", len(id_bytes)))
                    f.write(id_bytes)
                    f.write(struct.pack("<H", len(path_bytes)))
                    f.write(path_bytes)

                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, index_file)

            dir_fd = os.open(str(collection_path), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def update_batch(self, collection_path: Path, updates: Dict[str, Path]) -> None:
        """Update ID index with new entries (incremental update).

        Args:
            collection_path: Path to collection directory
            updates: Dictionary of point IDs to file paths to add/update
        """
        with self._lock:
            # Load existing index
            existing_index = self.load_index(collection_path)

            # Merge updates
            existing_index.update(updates)

            # Save back to disk
            self.save_index(collection_path, existing_index)

    def remove_ids(self, collection_path: Path, point_ids: list) -> None:
        """Remove entries from ID index.

        Args:
            collection_path: Path to collection directory
            point_ids: List of point IDs to remove
        """
        with self._lock:
            # Load existing index
            existing_index = self.load_index(collection_path)

            # Remove specified IDs
            for point_id in point_ids:
                existing_index.pop(point_id, None)

            # Save back to disk
            self.save_index(collection_path, existing_index)

    def rebuild_from_vectors(self, collection_path: Path) -> Dict[str, Path]:
        """Rebuild ID index by scanning all vector JSON files.

        Uses BackgroundIndexRebuilder for atomic file swapping with exclusive
        locking. Index loads can continue using old index during rebuild.

        Args:
            collection_path: Path to collection directory

        Returns:
            Dictionary mapping point IDs to file paths
        """
        import json
        from .background_index_rebuilder import BackgroundIndexRebuilder

        id_index = {}

        # Scan all vector JSON files
        scanned_count = 0
        for json_file in collection_path.rglob("*.json"):
            if "collection_meta" in json_file.name:
                continue
            if json_file.name == self.INDEX_FILENAME:
                continue

            scanned_count += 1
            try:
                with open(json_file) as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "rebuild_from_vectors: skipping %s — JSON parse error: %s",
                    json_file,
                    exc,
                )
                continue

            if not isinstance(data, dict):
                logger.warning(
                    "rebuild_from_vectors: skipping %s — expected JSON object, got %s",
                    json_file,
                    type(data).__name__,
                )
                continue

            if "id" not in data:
                logger.warning(
                    "rebuild_from_vectors: skipping %s — missing 'id' field",
                    json_file,
                )
                continue

            id_index[data["id"]] = json_file

        if not id_index and scanned_count > 0:
            logger.error(
                "rebuild_from_vectors: suspicious zero-entry rebuild — "
                "scanned %d vector files but produced no valid entries in %s",
                scanned_count,
                collection_path,
            )

        # Use BackgroundIndexRebuilder for atomic swap with locking
        rebuilder = BackgroundIndexRebuilder(collection_path)
        index_file = collection_path / self.INDEX_FILENAME

        def build_id_index_to_temp(temp_file: Path) -> None:
            """Build ID index to temp file."""
            with open(temp_file, "wb") as f:
                # Write number of entries (4 bytes, uint32)
                f.write(struct.pack("<I", len(id_index)))

                # Write each entry
                for point_id, file_path in id_index.items():
                    # Make path relative to collection_path
                    try:
                        relative_path = file_path.relative_to(collection_path)
                        path_str = str(relative_path)
                    except ValueError:
                        # If path is not relative to collection_path, store as-is
                        path_str = str(file_path)

                    # Encode strings to UTF-8
                    id_bytes = point_id.encode("utf-8")
                    path_bytes = path_str.encode("utf-8")

                    # Write ID length (2 bytes, uint16) and ID string
                    f.write(struct.pack("<H", len(id_bytes)))
                    f.write(id_bytes)

                    # Write path length (2 bytes, uint16) and path string
                    f.write(struct.pack("<H", len(path_bytes)))
                    f.write(path_bytes)

        # Rebuild with lock (entire rebuild duration)
        rebuilder.rebuild_with_lock(build_id_index_to_temp, index_file)

        return id_index
