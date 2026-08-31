"""Bug #1763 code review, CRITICAL-2: `cidx watch`'s FTS handler
initialization block self-heals a stale-schema Tantivy index (Bug #1761's
_PATH_EXACT_FIELD) independently of SmartIndexer.smart_index() -- it does
NOT always run through smart_index() first (e.g. semantic indexing is not
enabled for this watch session, or this is not the initial sync), so it
cannot rely on smart_index()'s own eager repopulation (CRITICAL-1's fix)
having already rebuilt the index. Before this fix, the watch handler's
stale-schema rmtree() wiped the index and never repopulated it -- every
untouched file's FTS entries were gone with no recovery path short of a
full manual reindex.

`cidx watch` itself is a long-running interactive command (an unbounded
`while not handler.interrupted: time.sleep(1)` loop) that cannot be driven
end-to-end without either refactoring its interrupt-handling into an
injectable seam (out of scope for this fix) or replacing a collaborator
under test. Both are undesirable, so this file instead tests the CRITICAL-2
fix at the level it actually lives at: `_populate_fts_index_from_disk()`,
the standalone helper `watch()`'s FTS handler init block calls (see
`src/code_indexer/cli.py`, right after `tantivy_manager.initialize_index()`
in the FTS handler init section), driven directly with real files, a real
TantivyIndexManager, and no test doubles at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import tantivy

from code_indexer.cli import _populate_fts_index_from_disk
from code_indexer.config import Config
from code_indexer.services.tantivy_index_manager import TantivyIndexManager

# Tantivy IndexWriter heap size for the raw-tantivy legacy-index builder
# below -- mirrors the identical named constant used elsewhere (e.g.
# test_fts_schema_rebuild_1763.py's _build_legacy_index()).
_TEST_WRITER_HEAP_BYTES = 50_000_000


def _build_legacy_fts_index(index_dir: Path, documents: List[Dict[str, Any]]) -> None:
    """Build a real on-disk Tantivy index using the PRE-#1761 schema shape
    (no path_exact_raw / content_raw_verbatim fields), pre-populated with
    `documents`. `documents` entries are duck-typed dicts with
    heterogeneous value types (str path/content, List[str] identifiers,
    int line numbers) -- Any is the correct annotation for that shape,
    same convention as the established _build_legacy_index in
    test_fts_schema_rebuild_1763.py / test_tantivy_path_exact_delete_1761.py.
    """
    from tantivy import Facet

    index_dir.mkdir(parents=True, exist_ok=True)

    schema_builder = tantivy.SchemaBuilder()
    schema_builder.add_text_field("path", stored=True)
    schema_builder.add_text_field("content", stored=False)
    schema_builder.add_text_field("content_raw", stored=True)
    schema_builder.add_text_field("identifiers", stored=True)
    schema_builder.add_unsigned_field("line_start", indexed=True, stored=True)
    schema_builder.add_unsigned_field("line_end", indexed=True, stored=True)
    schema_builder.add_text_field("language", stored=True)
    schema_builder.add_facet_field("language_facet")
    schema = schema_builder.build()

    index = tantivy.Index(schema, str(index_dir))
    writer = index.writer(_TEST_WRITER_HEAP_BYTES)

    for doc_dict in documents:
        doc = tantivy.Document()
        doc.add_text("path", doc_dict["path"])
        doc.add_text("content", doc_dict["content"])
        doc.add_text("content_raw", doc_dict["content_raw"])
        doc.add_text("identifiers", " ".join(doc_dict["identifiers"]))
        doc.add_unsigned("line_start", doc_dict["line_start"])
        doc.add_unsigned("line_end", doc_dict["line_end"])
        doc.add_text("language", doc_dict["language"])
        doc.add_facet("language_facet", Facet.from_string(f"/{doc_dict['language']}"))
        writer.add_document(doc)
    writer.commit()
    writer.wait_merging_threads()


def test_populate_fts_index_from_disk_adds_every_file(tmp_path: Path) -> None:
    """Direct unit coverage: _populate_fts_index_from_disk() must discover
    and add every file on disk, matching SmartIndexer.
    _populate_fts_from_all_files()'s contract."""
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "alpha.py").write_text("ALPHA_MARKER_1763 = 1\n")
    (codebase / "beta.py").write_text("BETA_MARKER_1763 = 1\n")

    config = Config(codebase_dir=codebase)
    fts_index_dir = codebase / ".code-indexer" / "tantivy_index"
    fts_manager = TantivyIndexManager(fts_index_dir)
    fts_manager.initialize_index(create_new=True)

    count = _populate_fts_index_from_disk(config, fts_manager)
    assert count == 2

    verify_manager = TantivyIndexManager(fts_index_dir)
    verify_manager.open_for_search()
    alpha_results = verify_manager.search("ALPHA_MARKER_1763", limit=5)
    beta_results = verify_manager.search("BETA_MARKER_1763", limit=5)
    assert len(alpha_results) == 1
    assert alpha_results[0]["path"] == "alpha.py"
    assert len(beta_results) == 1
    assert beta_results[0]["path"] == "beta.py"


def _run_watch_style_self_heal(config: Config, fts_index_dir: Path) -> None:
    """Replicates `cidx watch`'s FTS handler init block sequence EXACTLY,
    one-for-one, with the real production functions it calls
    (schema_needs_rebuild(), initialize_index(), and the real
    _populate_fts_index_from_disk() -- no hand-reimplementation of the
    repopulation logic itself):

        fts_index_exists = fts_index_dir.exists()
        fts_schema_stale = fts_index_exists and tantivy_manager.schema_needs_rebuild()
        fts_needs_full_population = fts_schema_stale or not fts_index_exists
        if fts_schema_stale:
            shutil.rmtree(fts_index_dir)
        tantivy_manager.initialize_index(create_new=(not fts_index_exists) or fts_schema_stale)
        if fts_needs_full_population:
            _populate_fts_index_from_disk(config, tantivy_manager)
    """
    tantivy_manager = TantivyIndexManager(fts_index_dir)
    fts_index_exists = fts_index_dir.exists()
    fts_schema_stale = fts_index_exists and tantivy_manager.schema_needs_rebuild()
    assert fts_schema_stale is True, "setup sanity check: legacy schema must be stale"
    fts_needs_full_population = fts_schema_stale or not fts_index_exists

    if fts_schema_stale:
        shutil.rmtree(fts_index_dir)
    tantivy_manager.initialize_index(
        create_new=(not fts_index_exists) or fts_schema_stale
    )
    if fts_needs_full_population:
        _populate_fts_index_from_disk(config, tantivy_manager)


def test_watch_style_stale_schema_self_heal_survives_untouched_doc(
    tmp_path: Path,
) -> None:
    """Proves CRITICAL-2 is fixed: a pre-existing document for an
    untouched file, seeded in a legacy-schema on-disk index, survives
    `cidx watch`'s exact stale-schema-detect -> wipe -> repopulate
    sequence (see _run_watch_style_self_heal above)."""
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "existing.py").write_text("EXISTING_MARKER_1763 = 1\n")
    (codebase / "other.py").write_text("OTHER_MARKER_1763 = 1\n")

    fts_index_dir = codebase / ".code-indexer" / "tantivy_index"
    _build_legacy_fts_index(
        fts_index_dir,
        [
            {
                "path": "existing.py",
                "content": "EXISTING_MARKER_1763",
                "content_raw": "EXISTING_MARKER_1763",
                "identifiers": ["EXISTING_MARKER_1763"],
                "line_start": 1,
                "line_end": 1,
                "language": "py",
            }
        ],
    )

    config = Config(codebase_dir=codebase)
    _run_watch_style_self_heal(config, fts_index_dir)

    verify_manager = TantivyIndexManager(fts_index_dir)
    verify_manager.open_for_search()
    existing_results = verify_manager.search("EXISTING_MARKER_1763", limit=5)
    other_results = verify_manager.search("OTHER_MARKER_1763", limit=5)

    assert len(existing_results) == 1, (
        "CRITICAL-2: existing.py's FTS entry was lost by the stale-schema "
        f"self-heal. Got: {existing_results}"
    )
    assert existing_results[0]["path"] == "existing.py"
    assert len(other_results) == 1
    assert other_results[0]["path"] == "other.py"


def _run_watch_style_self_heal_marker_check(
    config: Config, fts_index_dir: Path
) -> None:
    """Replicates `cidx watch`'s FTS handler init block sequence EXACTLY,
    one-for-one, with the real production functions it calls, POST Bug
    #1763 MEDIUM-5 fix -- `fts_index_exists` is now a meta.json MARKER-FILE
    check, matching smart_indexer.py:405 exactly, instead of a bare
    directory-existence check:

        fts_index_exists = (fts_index_dir / "meta.json").exists()
        fts_schema_stale = fts_index_exists and tantivy_manager.schema_needs_rebuild()
        fts_needs_full_population = fts_schema_stale or not fts_index_exists
        if fts_schema_stale:
            shutil.rmtree(fts_index_dir)
        tantivy_manager.initialize_index(create_new=(not fts_index_exists) or fts_schema_stale)
        if fts_needs_full_population:
            _populate_fts_index_from_disk(config, tantivy_manager)
    """
    tantivy_manager = TantivyIndexManager(fts_index_dir)
    fts_index_exists = (fts_index_dir / "meta.json").exists()
    fts_schema_stale = fts_index_exists and tantivy_manager.schema_needs_rebuild()
    fts_needs_full_population = fts_schema_stale or not fts_index_exists

    if fts_schema_stale:
        shutil.rmtree(fts_index_dir)
    tantivy_manager.initialize_index(
        create_new=(not fts_index_exists) or fts_schema_stale
    )
    if fts_needs_full_population:
        _populate_fts_index_from_disk(config, tantivy_manager)


def test_watch_style_directory_present_meta_json_absent_gets_repopulated(
    tmp_path: Path,
) -> None:
    """MEDIUM-5 (#1763 code review): a previously failed/partial rmtree
    (e.g. mid-delete OSError) -- or any other reason the tantivy_index
    directory exists on disk without ever having been fully
    initialized -- can leave the FTS index directory PRESENT but with no
    `meta.json` marker file. The pre-fix `fts_index_exists =
    fts_index_dir.exists()` directory-only check treated this shape as
    "index exists, not stale" -> fts_schema_stale=False (schema_needs_
    rebuild() also returns False with no meta.json to read) ->
    fts_needs_full_population=False -> ZERO repopulation. But
    initialize_index(create_new=False) still builds a brand-new EMPTY
    index underneath (TantivyIndexManager.initialize_index()'s own
    `create_new or not (index_dir / "meta.json").exists()` fallback at
    tantivy_index_manager.py:403) -- so the repo's FTS data would be
    silently lost, with no warning. The fixed marker-file check
    (`(fts_index_dir / "meta.json").exists()`, matching
    smart_indexer.py:405 exactly) correctly reports
    fts_index_exists=False for this directory shape, driving
    fts_needs_full_population=True and a real repopulation from disk.
    """
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "existing.py").write_text("EXISTING_MARKER_MEDIUM5 = 1\n")

    fts_index_dir = codebase / ".code-indexer" / "tantivy_index"
    fts_index_dir.mkdir(parents=True)
    # Leftover residue from a partial/failed rmtree (or any half-formed
    # directory): present on disk, contains a stray file, but crucially
    # has no meta.json.
    (fts_index_dir / "leftover.tmp").write_text("partial rmtree residue")
    assert fts_index_dir.exists()
    assert not (fts_index_dir / "meta.json").exists()

    config = Config(codebase_dir=codebase)
    _run_watch_style_self_heal_marker_check(config, fts_index_dir)

    verify_manager = TantivyIndexManager(fts_index_dir)
    verify_manager.open_for_search()
    results = verify_manager.search("EXISTING_MARKER_MEDIUM5", limit=5)
    assert len(results) == 1, (
        "MEDIUM-5: a directory-present-but-meta.json-absent FTS index "
        f"must be repopulated, not left silently empty. Got: {results}"
    )
    assert results[0]["path"] == "existing.py"
