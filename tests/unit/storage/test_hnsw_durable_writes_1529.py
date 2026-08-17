"""Bug #1529 review finding #7(a): HNSW/metadata writes must be DURABLE.

A fixed temporal path means refreshes rewrite a shard IN PLACE, so the three
independently-visible artifacts of one logical update (`chunks.db`,
`hnsw_index.bin`, `collection_meta.json`) are what a concurrent reader sees.
SQLite gives `chunks.db` real transactional durability; the other two were
only ATOMIC (temp file + `os.replace`), never DURABLE:

  - The HNSW publishers saved the index to a temp path and renamed it with no
    fsync of the temp file and no fsync of the containing directory, so a
    crash/power-loss could publish unflushed contents or lose the rename.
  - TWO separate `collection_meta.json` writers duplicated the same
    temp-file+rename block WITHOUT any fsync -- even though
    `_atomic_write_metadata_durable` (added by Bug #1407 for exactly this
    reason) already existed in the same class and did it correctly. That file
    holds the load-bearing `hnsw_index.id_mapping`, so a torn write there
    destroys the integer-label -> point_id bridge.

Three copies of one write pattern, one correct and two not, is also the
three-strike duplication Messi #4 forbids: the fix routes every writer
through a single durable helper rather than adding fsync calls three times.

Round-4 correction: the first pass hardened `build_index`, which has ZERO
production callers. The publishers that actually run are
`save_incremental_update` and `BackgroundIndexRebuilder.atomic_swap` -- the
latter being what `end_indexing()` -> `rebuild_from_vectors()` goes through
for EVERY collection kind, and therefore the exact path an admin temporal
reindex now takes once `--clear` is suppressed. Both were left as bare atomic
renames. This guard is consequently enumerated over all three publishers, so
hardening a dead path again cannot pass for the real one.

Durability itself cannot be asserted without crashing the machine, so it is
verified STRUCTURALLY -- including the ORDERING that makes fsync meaningful
(source flushed before the rename; directory flushed after it) -- plus real
round-trips through the production load/publish paths.

The metadata guard keys on PUBLICATION (which method renames something onto
collection_meta.json) rather than on one serializer name, so a future writer
using json.dumps, write_text or anything else cannot slip past it.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path
from typing import Any, List

import numpy as np

from code_indexer.storage.background_index_rebuilder import BackgroundIndexRebuilder
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

DURABLE_METADATA_WRITER = "_atomic_write_metadata_durable"

#: Shared durability helpers (`utils/file_locking.py`). `fsync_path` flushes a
#: file OR a directory by path -- `atomic_swap` needs both shapes.
DIRECTORY_FSYNC = "fsync_directory"
SWAP_SOURCE_FSYNC = "fsync_path"

#: Flush of an already-open file descriptor, used where the writer holds one.
FILE_FSYNC = "nfs_safe_fsync"

#: Substrings that identify a rename TARGET as the metadata file.
METADATA_TARGET_MARKERS = ("meta_file", "collection_meta")

#: Deterministic fixture vectors -- the value itself carries no meaning beyond
#: reproducibility (it is this bug's number).
RANDOM_SEED = 1529

VECTOR_DIM = 8
NUM_VECTORS = 12


def _class_tree() -> ast.AST:
    # getsource() of a module-level class is already at column 0. cleandoc()
    # must NOT be used here: it strips the body's common indentation and turns
    # the class into an IndentationError.
    return ast.parse(inspect.getsource(HNSWIndexManager))


def _called_symbol(node: ast.Call):
    return (
        node.func.id
        if isinstance(node.func, ast.Name)
        else getattr(node.func, "attr", None)
    )


def _call_lines(func: Any, name: str) -> List[int]:
    """Source line numbers of every call to `name` inside func's own body.

    Args:
        func: A Python function/method object whose source is retrievable.
        name: The called symbol to look for (plain name or attribute).
    """
    if not callable(func):
        raise TypeError(f"_call_lines: expected a callable, got {type(func)!r}")
    if not name:
        raise ValueError("_call_lines: name is required")

    # A method's source is indented; dedent (never cleandoc, which also
    # rewrites the docstring's leading whitespace and shifts line numbers).
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_symbol(node) == name
    )


def _methods_publishing_metadata() -> List[str]:
    """Methods that rename something ONTO collection_meta.json.

    Publication is the durability-critical act, so this is the property worth
    constraining -- independent of which serializer produced the bytes.
    """
    publishers = []
    for node in ast.walk(_class_tree()):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or _called_symbol(inner) != "replace":
                continue
            rendered = " ".join(ast.unparse(arg) for arg in inner.args)
            if any(marker in rendered for marker in METADATA_TARGET_MARKERS):
                publishers.append(node.name)
                break
    return publishers


def _methods_calling(name: str) -> List[str]:
    return [
        node.name
        for node in ast.walk(_class_tree())
        if isinstance(node, ast.FunctionDef)
        and node.name != name
        and any(
            _called_symbol(inner) == name
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
        )
    ]


def _assert_durable_rename_ordering(
    func: Any,
    *,
    rename_symbols: List[str],
    file_fsync_symbol: str,
    dir_fsync_symbol: str,
) -> None:
    """Assert func flushes the source BEFORE and the directory AFTER its rename.

    Presence alone is not enough: an fsync of the source AFTER the rename, or
    of the directory BEFORE it, would satisfy a naive "is it called?" check
    while providing no durability whatsoever.
    """
    rename_lines: List[int] = []
    for symbol in rename_symbols:
        rename_lines.extend(_call_lines(func, symbol))
    assert rename_lines, (
        f"{func.__qualname__} no longer renames anything into place "
        f"(looked for {rename_symbols})"
    )
    rename_line = min(rename_lines)

    file_fsyncs = _call_lines(func, file_fsync_symbol)
    dir_fsyncs = _call_lines(func, dir_fsync_symbol)

    assert any(line < rename_line for line in file_fsyncs), (
        f"{func.__qualname__} does not fsync the source BEFORE the rename "
        f"({file_fsync_symbol} at {file_fsyncs}, rename at line "
        f"{rename_line}); the rename can publish unflushed contents"
    )
    assert any(line > rename_line for line in dir_fsyncs), (
        f"{func.__qualname__} does not fsync the containing directory AFTER "
        f"the rename ({dir_fsync_symbol} at {dir_fsyncs}, rename at line "
        f"{rename_line}); the rename itself can be lost on power-loss"
    )


def test_only_the_durable_helper_publishes_collection_meta_json() -> None:
    """Every metadata write must go through the ONE durable writer.

    Covers all three halves of the invariant: the helper exists, production
    code actually routes through it, and nothing else publishes the file
    (a helper nothing calls would leave every writer unprotected).
    """
    assert hasattr(HNSWIndexManager, DURABLE_METADATA_WRITER), (
        f"{DURABLE_METADATA_WRITER} is gone; there is no durable metadata "
        "writer left to route through"
    )

    publishers = _methods_publishing_metadata()
    assert publishers == [DURABLE_METADATA_WRITER], (
        f"collection_meta.json is published by {publishers}, but only "
        f"{DURABLE_METADATA_WRITER} may do so. Any other publisher renames "
        "without fsync, and a torn write there destroys "
        "hnsw_index.id_mapping."
    )

    assert _methods_calling(DURABLE_METADATA_WRITER), (
        f"nothing calls {DURABLE_METADATA_WRITER}; the metadata-writing "
        "paths are not routed through it"
    )


def test_build_index_fsyncs_in_the_order_that_makes_it_durable() -> None:
    """Presence is not enough -- the ORDER around os.replace is the point."""
    _assert_durable_rename_ordering(
        HNSWIndexManager.build_index,
        rename_symbols=["replace"],
        file_fsync_symbol=FILE_FSYNC,
        dir_fsync_symbol=DIRECTORY_FSYNC,
    )


def test_save_incremental_update_fsyncs_in_the_order_that_makes_it_durable() -> None:
    """The incremental publisher is a REAL production path, unlike build_index.

    `add_or_update_vector`/`_apply_visibility_aware_incremental_update` finalize
    through here, so a torn `hnsw_index.bin` published from this method is a
    live hazard on a fixed temporal path that is rewritten in place.
    """
    _assert_durable_rename_ordering(
        HNSWIndexManager.save_incremental_update,
        rename_symbols=["replace"],
        file_fsync_symbol=FILE_FSYNC,
        dir_fsync_symbol=DIRECTORY_FSYNC,
    )


def test_atomic_swap_fsyncs_in_the_order_that_makes_it_durable() -> None:
    """`atomic_swap` is THE publisher `rebuild_from_vectors` actually uses.

    `end_indexing()` -> `rebuild_from_vectors()` -> `rebuild_with_lock()` ->
    `atomic_swap()` is the path every collection kind takes, and the one an
    admin temporal reindex now routes through once `--clear` is suppressed.
    Hardening `build_index` (zero production callers) left it untouched.
    """
    _assert_durable_rename_ordering(
        BackgroundIndexRebuilder.atomic_swap,
        rename_symbols=["rename", "replace"],
        file_fsync_symbol=SWAP_SOURCE_FSYNC,
        dir_fsync_symbol=DIRECTORY_FSYNC,
    )


def test_atomic_swap_still_publishes_files_and_directories(tmp_path: Path) -> None:
    """Durability must not break EITHER shape `atomic_swap` serves.

    Its callers publish a plain file (HNSW, id_index) and a directory (the
    Tantivy FTS index), so a source-flush that only understands files would
    break FTS rebuilds outright.
    """
    collection_path = tmp_path / "collection"
    rebuilder = BackgroundIndexRebuilder(collection_path)
    # The constructor creates the collection directory; assert rather than
    # assume, so a future change there fails HERE instead of mid-test.
    assert collection_path.is_dir()

    file_temp = collection_path / "index.bin.tmp"
    file_temp.write_bytes(b"payload-1529")
    file_target = collection_path / "index.bin"
    rebuilder.atomic_swap(file_temp, file_target)
    assert not file_temp.exists()
    assert file_target.read_bytes() == b"payload-1529"

    dir_temp = collection_path / "fts.tmp"
    dir_temp.mkdir()
    (dir_temp / "segment").write_bytes(b"segment-1529")
    dir_target = collection_path / "fts"
    rebuilder.atomic_swap(dir_temp, dir_target)
    assert not dir_temp.exists()
    assert (dir_target / "segment").read_bytes() == b"segment-1529"


def test_built_index_reloads_through_the_production_path(tmp_path: Path) -> None:
    """Real round-trip: durability changes must not alter correctness.

    Proven by an INDEPENDENT reload via the production `load_index()` path,
    asserting the persisted element count -- never merely "no exception".
    """
    collection_path = tmp_path / "collection"
    collection_path.mkdir()
    (collection_path / "collection_meta.json").write_text(
        json.dumps({"vector_dim": VECTOR_DIM})
    )

    rng = np.random.default_rng(RANDOM_SEED)
    vectors = rng.standard_normal((NUM_VECTORS, VECTOR_DIM)).astype(np.float32)
    ids = [f"proj:commit:{i:08x}:0" for i in range(NUM_VECTORS)]

    manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
    manager.build_index(collection_path=collection_path, vectors=vectors, ids=ids)

    # A FRESH manager, so nothing in-process can mask a bad on-disk file.
    reloaded = HNSWIndexManager(vector_dim=VECTOR_DIM).load_index(collection_path)
    assert reloaded is not None, "the persisted index could not be reloaded"
    assert reloaded.get_current_count() == NUM_VECTORS

    meta = json.loads((collection_path / "collection_meta.json").read_text())
    assert meta["hnsw_index"]["vector_count"] == NUM_VECTORS
    # The id_mapping bridge must survive the durable write intact.
    assert len(meta["hnsw_index"]["id_mapping"]) == NUM_VECTORS
