"""Bug #1529 review item 4: the temporal-never-writes-legacy-JSON invariant
must be self-enforcing at the STORAGE layer, not only at the CLI front door.

Bug #1528's binding rule is that temporal indexing never writes another
legacy ``vector_*.json`` file. `FilesystemVectorStore.create_collection`
still honors an explicit ``use_chunks_db_for_new_collections=False`` for a
temporal collection, so the storage layer itself does not enforce the rule --
it trusts its callers. That invariant has now been violated and re-violated
across the whole #1528/#1529 saga, so trusting callers is not good enough.

DELIBERATE DEVIATION, flagged for review: the review asked for a hard raise
inside ``create_collection``. That was investigated and rejected on evidence,
because a raise is strictly WORSE here:

  1. It would contradict a shipped, deliberately-tested Bug #1528 decision
     (``test_temporal_chunks_db_layout_1528.py::
     test_explicit_sharded_json_still_honored_for_temporal``): temporal is
     tri-state on purpose -- CHUNKS_DB by default, legacy ONLY on an explicit
     request.
  2. SEVENTEEN test files build a REAL legacy temporal shard through the
     production writer, precisely to prove the legacy READ and MIGRATE paths
     still work -- and they must, because real fleet data is still
     SHARDED_JSON until it is migrated. A raise would force every one of them
     to hand-fabricate files instead, replacing writer-produced fixtures with
     invented ones and weakening exactly the migration coverage Epic #1454
     depends on.
  3. Keeping them working would require a test-only escape parameter on
     production code -- a knob no production caller ever sets (Messi #12).

What the storage layer CAN enforce, and what the actual regression vector is,
is that no PRODUCTION code path ever requests the legacy layout. That is
enforced here by enumeration over the real source tree, plus a loud WARNING
so any real occurrence is discoverable in logs instead of silent (the same
"fix the silence, not the behavior" treatment finding #8 received).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import List, Tuple

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

LAYOUT_KWARG = "use_chunks_db_for_new_collections"

TEMPORAL_COLLECTION = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "code_indexer"


def _literal_false_layout_call_sites() -> List[Tuple[str, int]]:
    """Every production call passing a LITERAL False for the layout kwarg.

    A forwarded variable (``layout=some_value``) is not a violation -- those
    thread a caller's choice through and are the normal wiring. A hardcoded
    ``False`` is a production site deliberately asking for the legacy layout,
    which is what must never exist.
    """
    offenders: List[Tuple[str, int]] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != LAYOUT_KWARG:
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and value.value is False:
                    offenders.append((str(path.relative_to(SRC_ROOT)), node.lineno))
    return offenders


def _warning_records_naming(caplog, collection_name: str) -> List[str]:
    """Messages logged at exactly WARNING that name the given collection."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and collection_name in record.getMessage()
    ]


def test_no_production_call_site_requests_the_legacy_layout() -> None:
    """The real invariant: production never asks for SHARDED_JSON.

    The CLI's `--new-collection-layout=sharded_json` flag is the only
    production route to an explicit False, and
    `reject_sharded_json_for_temporal` already refuses it alongside
    `--index-commits` (before daemon delegation, so both the direct and
    daemon routes are covered). This guard closes the other direction: a
    future edit hardcoding the legacy layout somewhere in `src/` fails CI
    here.
    """
    offenders = _literal_false_layout_call_sites()

    assert offenders == [], (
        f"production code requests the legacy chunk layout at {offenders}. "
        "Temporal indexing must never write legacy vector_*.json files "
        "(Bug #1528); the legacy layout is reachable only by explicit "
        "operator request, never hardcoded."
    )


def test_legacy_temporal_collection_creation_is_logged_loudly(
    tmp_path: Path, caplog
) -> None:
    """A deliberately-legacy temporal build must not be silent.

    The storage layer cannot distinguish a test fabricating pre-#1528 data
    from a production mistake, so it must not refuse -- but an occurrence in
    a real deployment log is a five-alarm signal, and today it leaves no
    trace at all.
    """
    store = FilesystemVectorStore(
        base_path=tmp_path / "index", use_chunks_db_for_new_collections=False
    )

    with caplog.at_level(logging.WARNING):
        store.create_collection(TEMPORAL_COLLECTION, vector_size=VECTOR_SIZE)

    assert _warning_records_naming(caplog, TEMPORAL_COLLECTION), (
        "creating a temporal collection in the legacy SHARDED_JSON layout "
        "logged no WARNING naming it; records="
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_default_temporal_creation_stays_quiet(tmp_path: Path, caplog) -> None:
    """Discriminating control for the test above.

    Without this, a blanket warning on EVERY create_collection would satisfy
    the positive test while being pure noise -- and noise on every normal
    temporal shard trains operators to ignore the one occurrence that matters.
    """
    store = FilesystemVectorStore(base_path=tmp_path / "index")

    with caplog.at_level(logging.WARNING):
        store.create_collection(TEMPORAL_COLLECTION, vector_size=VECTOR_SIZE)

    assert not _warning_records_naming(caplog, TEMPORAL_COLLECTION), (
        "the default consolidated temporal build must not warn"
    )
