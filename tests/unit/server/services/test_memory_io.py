"""TDD tests for memory_io.py — Story #877 Phase 1b.

All tests written BEFORE implementation (RED phase).
"""

import hashlib
import stat
from collections import OrderedDict

import pytest

from code_indexer.server.services.memory_io import (
    MemoryFileCorruptError,
    MemoryFileNotFoundError,
    atomic_delete_memory_file,
    atomic_write_memory_file,
    compute_content_hash,
    deserialize_memory,
    read_memory_file,
    serialize_memory,
)

SHA256_HEX_LENGTH = 64


# ---------------------------------------------------------------------------
# serialize / deserialize round-trip
# ---------------------------------------------------------------------------


def test_serialize_round_trip():
    """serialize then deserialize returns equivalent dict and body."""
    fm = {"id": "abc", "type": "gotcha", "tags": ["a", "b"]}
    body = "body text"
    raw = serialize_memory(fm, body)
    recovered_fm, recovered_body = deserialize_memory(raw)
    assert recovered_fm == fm
    assert recovered_body.strip() == body


def test_serialize_empty_body():
    """body='' produces valid frontmatter with an empty body."""
    fm = {"id": "xyz", "type": "insight"}
    raw = serialize_memory(fm, "")
    recovered_fm, recovered_body = deserialize_memory(raw)
    assert recovered_fm == fm
    assert recovered_body.strip() == ""


def test_serialize_always_ends_with_newline():
    """serialize_memory always ends with a trailing newline."""
    raw = serialize_memory({"id": "n"}, "hello")
    assert raw.endswith("\n")


# ---------------------------------------------------------------------------
# deserialize error cases
# ---------------------------------------------------------------------------


def test_deserialize_missing_frontmatter_raises():
    """Plain string without --- delimiters raises MemoryFileCorruptError."""
    with pytest.raises(MemoryFileCorruptError):
        deserialize_memory("hello world")


def test_deserialize_malformed_yaml_raises():
    """Frontmatter with bad YAML raises MemoryFileCorruptError."""
    bad = "---\n:bad yaml: [\n---\nbody"
    with pytest.raises(MemoryFileCorruptError):
        deserialize_memory(bad)


# ---------------------------------------------------------------------------
# atomic_write_memory_file
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_with_parent_dirs(tmp_path):
    """atomic_write_memory_file creates parent directories as needed."""
    target = tmp_path / "subdir" / "deep" / "m.md"
    assert not target.parent.exists()
    atomic_write_memory_file(target, {"id": "1"}, "body")
    assert target.exists()


def test_atomic_write_returns_content_hash_matches_read(tmp_path):
    """Hash returned by atomic_write equals hash returned by read_memory_file."""
    target = tmp_path / "mem.md"
    returned_hash = atomic_write_memory_file(target, {"id": "2", "type": "t"}, "body")
    _, _, read_hash = read_memory_file(target)
    assert returned_hash == read_hash


def test_atomic_write_does_not_leave_temp_on_success(tmp_path):
    """No extra files remain in the directory after a successful write."""
    target = tmp_path / "mem.md"
    atomic_write_memory_file(target, {"id": "3"}, "clean")
    # Only the target file should exist; no temp artifacts
    all_files = list(tmp_path.iterdir())
    assert all_files == [target], f"Unexpected extra files: {all_files}"


def test_atomic_write_cleans_up_temp_on_failure(tmp_path):
    """On OSError write failure: no extra files linger AND OSError propagates."""
    target = tmp_path / "mem.md"

    # Make tmp_path read-only so any write attempt raises OSError (PermissionError).
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(OSError):
            atomic_write_memory_file(target, {"id": "4"}, "fail")
        # Directory should still contain no files (target was never created)
        all_files = list(tmp_path.iterdir())
        assert all_files == [], f"Leftover files after failure: {all_files}"
    finally:
        # Restore permissions so tmp_path cleanup can proceed
        tmp_path.chmod(stat.S_IRWXU)


def test_atomic_write_uses_utf8(tmp_path):
    """Non-ASCII characters survive a write/read round-trip identically."""
    target = tmp_path / "mem.md"
    body = "café au lait"
    atomic_write_memory_file(target, {"id": "5"}, body)
    _, recovered_body, _ = read_memory_file(target)
    assert recovered_body.strip() == body


# ---------------------------------------------------------------------------
# read_memory_file
# ---------------------------------------------------------------------------


def test_read_memory_file_returns_frontmatter_body_and_hash(tmp_path):
    """Round-trip via write then read returns correct fm, body, hash."""
    target = tmp_path / "mem.md"
    fm = {"id": "r1", "type": "gotcha"}
    body = "round-trip body"
    atomic_write_memory_file(target, fm, body)
    recovered_fm, recovered_body, content_hash = read_memory_file(target)
    assert recovered_fm == fm
    assert recovered_body.strip() == body
    assert len(content_hash) == SHA256_HEX_LENGTH


def test_read_missing_file_raises_memory_file_not_found_error(tmp_path):
    """Reading a non-existent path raises MemoryFileNotFoundError."""
    missing = tmp_path / "ghost.md"
    with pytest.raises(MemoryFileNotFoundError):
        read_memory_file(missing)


def test_read_corrupt_file_raises_corrupt_error(tmp_path):
    """Writing garbage bytes then reading raises MemoryFileCorruptError."""
    target = tmp_path / "corrupt.md"
    target.write_bytes(b"this is not frontmatter at all")
    with pytest.raises(MemoryFileCorruptError):
        read_memory_file(target)


# ---------------------------------------------------------------------------
# atomic_delete_memory_file
# ---------------------------------------------------------------------------


def test_atomic_delete_removes_file(tmp_path):
    """Write then delete; subsequent read raises MemoryFileNotFoundError."""
    target = tmp_path / "mem.md"
    atomic_write_memory_file(target, {"id": "d1"}, "")
    assert target.exists()
    atomic_delete_memory_file(target)
    assert not target.exists()
    with pytest.raises(MemoryFileNotFoundError):
        read_memory_file(target)


def test_atomic_delete_missing_file_raises(tmp_path):
    """Deleting a non-existent path raises MemoryFileNotFoundError."""
    missing = tmp_path / "nope.md"
    with pytest.raises(MemoryFileNotFoundError):
        atomic_delete_memory_file(missing)


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


def test_compute_content_hash_deterministic():
    """Same bytes produce the same hex digest every time."""
    data = b"hello world"
    assert compute_content_hash(data) == compute_content_hash(data)
    assert compute_content_hash(data) == hashlib.sha256(data).hexdigest()


def test_content_hash_changes_when_body_changes(tmp_path):
    """Two writes with different bodies produce different hashes."""
    t1 = tmp_path / "a.md"
    t2 = tmp_path / "b.md"
    h1 = atomic_write_memory_file(t1, {"id": "c1"}, "body one")
    h2 = atomic_write_memory_file(t2, {"id": "c1"}, "body two")
    assert h1 != h2


# ---------------------------------------------------------------------------
# key order stability (deterministic content_hash across round-trips)
# ---------------------------------------------------------------------------


def test_frontmatter_key_order_preserved_in_serialize():
    """Keys appear in insertion order in the serialized YAML.

    Stable key order is required so that content_hash is deterministic
    across round-trips: the same logical memory always produces the same hash.
    """
    keys = ["zebra", "alpha", "mango", "beta"]
    fm = OrderedDict((k, k + "_val") for k in keys)
    raw = serialize_memory(fm, "")
    # Extract the YAML block between the two --- lines
    parts = raw.split("---")
    yaml_block = parts[1]
    positions = [yaml_block.index(k) for k in keys]
    assert positions == sorted(positions), (
        f"Key order not preserved. Positions: {dict(zip(keys, positions))}"
    )


def test_content_hash_stable_across_round_trip(tmp_path):
    """Write, read back, rewrite: content_hash must be identical each time.

    This proves that serialize_memory uses stable key ordering so the same
    logical memory always hashes to the same value regardless of how many
    times it is read and rewritten.
    """
    target = tmp_path / "stable.md"
    keys = ["zebra", "alpha", "mango", "beta"]
    fm = OrderedDict((k, k + "_val") for k in keys)
    body = "stable body"

    # First write
    h1 = atomic_write_memory_file(target, fm, body)

    # Read back, rewrite with identical content
    recovered_fm, recovered_body, h_read = read_memory_file(target)
    assert h1 == h_read, "Hash changed between write and read"

    # Second write using the recovered data
    h2 = atomic_write_memory_file(target, recovered_fm, recovered_body.strip())
    assert h1 == h2, (
        f"Hash changed across round-trip: first={h1}, second={h2}. "
        "Key ordering is not stable."
    )
