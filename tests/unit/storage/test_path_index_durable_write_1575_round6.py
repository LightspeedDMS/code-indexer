"""Bug #1575 round 6, item 3 (Codex + opus dual review of round 5's diff):
``PathIndex.save_snapshot()`` did a bare ``open(path, "wb")`` +
``msgpack.dump()`` -- no temp file, no fsync, no atomic ``os.replace``, no
parent-directory fsync. This violates this project's own documented
durable-write convention (Bug #1407 -- CLAUDE.md mandates
temp-file+fsync+os.replace+directory-fsync for ALL persisted metadata;
this exact class already has ``HNSWIndexManager._atomic_write_metadata_
durable`` demonstrating the pattern for JSON). A crash mid-write left a
truncated/corrupt ``path_index.bin`` that ``PathIndex.load()`` then raised
UNCAUGHT on (msgpack ``ValueError``/``UnpackException``), bricking
``begin_indexing()`` -- opus reproduced this live.

This module covers the fix in two parts: AST-based durability-ordering
checks against the real ``PathIndex._durable_msgpack_write`` implementation
(mirroring ``test_hnsw_durable_writes_1529.py``'s established methodology),
plus real behavioral tests proving ``PathIndex.load()`` fails safe (never
raises) on a corrupt/truncated/missing bin.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any, List

from code_indexer.storage.filesystem_vector_store import PathIndex


def _called_symbol(node: ast.Call):
    return (
        node.func.id
        if isinstance(node.func, ast.Name)
        else getattr(node.func, "attr", None)
    )


def _calls(func: Any, name: str) -> List[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_symbol(node) == name
    ]


def test_durable_msgpack_write_renames_the_temp_var_onto_the_real_path() -> None:
    """The rename must publish the distinct ``tmp_path_str`` temp variable
    onto the real destination -- renaming the bare 'path' parameter onto
    itself (a no-op disguised as a rename) would defeat the fix entirely."""
    replace_calls = _calls(PathIndex._durable_msgpack_write, "replace")
    assert len(replace_calls) == 1, (
        f"expected exactly one os.replace() rename in "
        f"_durable_msgpack_write, found {len(replace_calls)}"
    )
    replace_call = replace_calls[0]
    assert len(replace_call.args) == 2
    source_expr = ast.unparse(replace_call.args[0])
    dest_expr = ast.unparse(replace_call.args[1])

    assert dest_expr == "str(path)", (
        f"expected the rename destination to be the real target path "
        f"('str(path)'), got {dest_expr!r}"
    )
    assert source_expr == "tmp_path_str", (
        f"expected the rename source to be the distinct temp variable "
        f"'tmp_path_str', got {source_expr!r} -- renaming the destination "
        f"onto itself is a no-op, not an atomic publish"
    )


def test_durable_msgpack_write_fsyncs_tmp_before_and_dir_after_rename() -> None:
    """Presence alone is insufficient -- the ORDER around the rename is
    what buys durability: the temp file's contents must be flushed BEFORE
    the rename publishes them, and the containing directory must be
    flushed AFTER the rename so the rename itself survives a crash."""
    func = PathIndex._durable_msgpack_write
    replace_calls = _calls(func, "replace")
    assert replace_calls
    rename_line = min(call.lineno for call in replace_calls)

    file_fsync_calls = _calls(func, "nfs_safe_fsync")
    assert file_fsync_calls, "no nfs_safe_fsync() call found"
    assert all(
        ast.unparse(call.args[0]) == "tmp_f.fileno()" for call in file_fsync_calls
    ), "nfs_safe_fsync() must be called on the TEMP file's own fileno()"
    assert any(call.lineno < rename_line for call in file_fsync_calls), (
        "nfs_safe_fsync() must run BEFORE the rename -- otherwise the "
        "rename can publish unflushed contents (the truncated-bin hazard "
        "this fix closes)"
    )

    dir_fsync_calls = _calls(func, "fsync_directory")
    assert dir_fsync_calls, "no fsync_directory() call found"
    assert all(
        ast.unparse(call.args[0]) == "path.parent" for call in dir_fsync_calls
    ), "fsync_directory() must be called on the destination's PARENT directory"
    assert any(call.lineno > rename_line for call in dir_fsync_calls), (
        "fsync_directory() must run AFTER the rename -- otherwise the "
        "rename itself can be lost on crash/power-loss"
    )


def test_load_returns_empty_on_corrupt_garbage_bin(tmp_path: Path) -> None:
    """A bin file containing bytes that are not valid msgpack at all must
    never raise uncaught out of PathIndex.load() -- opus reproduced this
    bricking begin_indexing() with an unhandled exception."""
    bin_path = tmp_path / "path_index.bin"
    bin_path.write_bytes(b"\xff\xff\xff\xff not valid msgpack at all \x00\x01")

    loaded = PathIndex.load(bin_path)

    assert loaded.all_paths() == set(), (
        "PathIndex.load() must fail SAFE on a corrupt bin (treat as absent, "
        "return an empty PathIndex) rather than raising uncaught -- a "
        "corrupt path_index.bin must never brick begin_indexing()"
    )


def test_load_returns_empty_on_truncated_bin(tmp_path: Path) -> None:
    """A bin file truncated mid-write (the exact pre-fix crash scenario)
    must also fail safe, never raise uncaught."""
    import msgpack

    full = msgpack.packb({"src/a.py": ["pt1", "pt2"], "src/b.py": ["pt3"]})
    truncated = full[: len(full) // 2]

    bin_path = tmp_path / "path_index.bin"
    bin_path.write_bytes(truncated)

    loaded = PathIndex.load(bin_path)

    assert loaded.all_paths() == set(), (
        "PathIndex.load() must fail SAFE on a truncated bin (treat as "
        "absent, return an empty PathIndex) rather than raising uncaught"
    )


def test_load_still_returns_empty_for_missing_file_no_regression(
    tmp_path: Path,
) -> None:
    """Regression guard: the pre-existing 'file does not exist' behavior
    (returns an empty PathIndex, no exception) must be byte-identical after
    adding corrupt-file handling."""
    bin_path = tmp_path / "does_not_exist.bin"

    loaded = PathIndex.load(bin_path)

    assert loaded.all_paths() == set()
