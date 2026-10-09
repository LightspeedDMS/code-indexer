"""Bug #1763 code review, CRITICAL-2: `cidx watch`'s FTS index start
self-heals a stale-schema Tantivy index (Bug #1761's _PATH_EXACT_FIELD)
independently of SmartIndexer.smart_index() -- it does NOT always run
through smart_index() first (e.g. semantic indexing is not enabled for this
watch session, or this is not the initial sync). Before the fix, the watch
start wiped a stale index and never repopulated it -- every untouched
file's FTS entries were gone.

`cidx watch` is a long-running interactive command, so these tests drive
the exact function `watch()` calls to open its FTS index --
`fts_lifecycle.open_fts_index_for_watch()`, the same decision and rebuild
as `cidx index --fts` (Bug #2056) -- with real files, a real Tantivy index
and no test doubles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import tantivy

from code_indexer.config import Config
from code_indexer.services.fts_lifecycle import open_fts_index_for_watch
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


def _start_watch_fts(config: Config) -> None:
    fts_manager, _rebuilt_files = open_fts_index_for_watch(config)
    fts_manager.close()


def _search(fts_index_dir: Path, token: str) -> List[Dict[str, Any]]:
    verify_manager = TantivyIndexManager(fts_index_dir)
    verify_manager.open_for_search()
    return verify_manager.search(token, limit=5)


def test_watch_start_without_index_adds_every_file(tmp_path: Path) -> None:
    """A watch start with no FTS index builds it from every file on disk."""
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "alpha.py").write_text("ALPHA_MARKER_1763 = 1\n")
    (codebase / "beta.py").write_text("BETA_MARKER_1763 = 1\n")
    config = Config(codebase_dir=codebase)

    _start_watch_fts(config)

    fts_index_dir = codebase / ".code-indexer" / "tantivy_index"
    alpha_results = _search(fts_index_dir, "ALPHA_MARKER_1763")
    beta_results = _search(fts_index_dir, "BETA_MARKER_1763")
    assert [r["path"] for r in alpha_results] == ["alpha.py"]
    assert [r["path"] for r in beta_results] == ["beta.py"]


def test_watch_stale_schema_self_heal_keeps_untouched_files(
    tmp_path: Path,
) -> None:
    """Proves CRITICAL-2 is fixed: an untouched file indexed in a
    legacy-schema index is still searchable after the watch start's
    stale-schema rebuild."""
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
    assert TantivyIndexManager(fts_index_dir).schema_needs_rebuild()

    _start_watch_fts(Config(codebase_dir=codebase))

    existing_results = _search(fts_index_dir, "EXISTING_MARKER_1763")
    other_results = _search(fts_index_dir, "OTHER_MARKER_1763")
    assert [r["path"] for r in existing_results] == ["existing.py"], (
        "CRITICAL-2: existing.py's FTS entry was lost by the stale-schema "
        f"self-heal. Got: {existing_results}"
    )
    assert [r["path"] for r in other_results] == ["other.py"]


def test_watch_directory_present_meta_json_absent_gets_repopulated(
    tmp_path: Path,
) -> None:
    """MEDIUM-5 (#1763 code review): a previously failed/partial rmtree --
    or any other reason the tantivy_index directory exists without ever
    having been fully initialized -- leaves the directory PRESENT with no
    `meta.json`. That shape is "no index" (meta.json is the marker file),
    so the watch start builds it from disk instead of silently opening a
    brand-new EMPTY index."""
    codebase = tmp_path / "proj"
    codebase.mkdir()
    (codebase / "existing.py").write_text("EXISTING_MARKER_MEDIUM5 = 1\n")

    fts_index_dir = codebase / ".code-indexer" / "tantivy_index"
    fts_index_dir.mkdir(parents=True)
    # Leftover residue from a partial/failed rmtree (or any half-formed
    # directory): present on disk, contains a stray file, but crucially
    # has no meta.json.
    (fts_index_dir / "leftover.tmp").write_text("partial rmtree residue")
    assert not (fts_index_dir / "meta.json").exists()

    _start_watch_fts(Config(codebase_dir=codebase))

    results = _search(fts_index_dir, "EXISTING_MARKER_MEDIUM5")
    assert [r["path"] for r in results] == ["existing.py"], (
        "MEDIUM-5: a directory-present-but-meta.json-absent FTS index "
        f"must be repopulated, not left silently empty. Got: {results}"
    )
