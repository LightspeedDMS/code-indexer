"""Story #1787 S2 amendment AC14 (ADR-003 Decision 4): service_init.py
attaches ONE composite cache to the memory governor (its attach slot is
single), holding the core's HNSW cache, and publishes it so a feature add-on
puts its own cache under the governor when it starts (X-Ray's graph
wire-file cache: ``cidx_xray.server.lifecycle``, proven executably in
tests/unit/xray/test_xray_feature_lifecycle.py).

Source inspection, for the reason test_service_init_node_id_wiring_1400.py
documents: initialize_services() creates schemas, threads and connections.
"""

from __future__ import annotations

from pathlib import Path

_SERVICE_INIT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "service_init.py"
)


def _source() -> str:
    return _SERVICE_INIT_PATH.read_text()


def test_exactly_one_attach_call_with_the_composite() -> None:
    source = _source()
    assert source.count("_memory_governor.attach_cache(") == 1
    assert "_memory_governor.attach_cache(_governed_composite_cache)" in source


def test_composite_holds_hnsw_at_the_shared_floor_and_is_published() -> None:
    source = _source()
    start = source.find("_governed_composite_cache = CompositeLRUCache(")
    assert start != -1
    block = source[start : source.find("\n    )", start)]
    assert "_server_hnsw_cache" in block
    assert "floor_per_cache=YELLOW_LRU_FLOOR" in block
    published = source.find("set_governed_cache_composite(_governed_composite_cache)")
    attached = source.find("_memory_governor.attach_cache(_governed_composite_cache)")
    assert start < published < attached


def test_service_init_knows_no_feature() -> None:
    source = _source()
    assert "xray" not in source.lower()
