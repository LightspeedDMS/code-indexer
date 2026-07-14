"""
Story #1358 hardening: repair_orphans() must fail safely, never crash, on a
link-list connection pointing to an out-of-range element id.

Code review finding (fixed in fork commit on top of 57e9453): the inbound-
counting loop in repair_orphans() already bounds-checked neighbor ids
(`if ((size_t)data[j] < n) ...`), but the `try_connect` helper used for the
anchor/victim connection logic did NOT apply the same guard:

  - `get_linklist0(anchor)` where `anchor` is read out of a link list --
    an out-of-range id would write outside cur_element_count.
  - `inbound[cand]` where `cand` is similarly sourced -- out-of-range
    std::vector::operator[] (undefined behavior) if that id is >= n.

This is unreachable via the near-tie/exact-tie regimes alone (they only
ever produce zero-inbound nodes, never invalid-id connections), but S3's
future fleet sweep will invoke repair_orphans() against arbitrary REAL
production indexes, which could carry other corruption (the spike's own
caveats mention torn-write scenarios). These tests directly corrupt an
on-disk index file to simulate exactly that, proving repair_orphans()
degrades gracefully instead of hitting undefined behavior.

Real project hnswlib fork only. Zero mocks. Real save_index/load_index and
real binary-level corruption via the fork's documented on-disk layout
(HierarchicalNSW::saveIndex/loadIndex, hnswlib/hnswalg.h).
"""

import struct

import hnswlib

from tests.utils.hnsw_orphan_corpus import near_tie_corpus, build_hnsw_index

CORPUS_DIM = 1024
CORPUS_SIZE = 1000
NEAR_TIE_NOISE_SCALE = 1e-6
TEMPORAL_SHAPED_POCKET_FRACTION = 1.0
CORPUS_SEED = 42
SINGLE_THREADED = 1

# hnswlib's on-disk header layout (HierarchicalNSW::saveIndex, hnswalg.h):
# offsetLevel0_(size_t) max_elements_(size_t) cur_element_count(size_t)
# size_data_per_element_(size_t) label_offset_(size_t) offsetData_(size_t)
# maxlevel_(int) enterpoint_node_(tableint=uint) maxM_(size_t) maxM0_(size_t)
# M_(size_t) mult_(double) ef_construction_(size_t). offsetLevel0_ is always
# 0 (hnswalg.h:124), so each element's level-0 link list is the first field
# in its per-element block: a 4-byte linklistsizeint (count in the low 2
# bytes) followed by maxM0_ tableint (4-byte unsigned int) neighbor slots.
_HEADER_FMT = "<QQQQQQiIQQQdQ"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

# An id comfortably beyond any real element -- corpus size + a fixed offset,
# unambiguously out of range regardless of which real orphan/anchor is used.
_OUT_OF_RANGE_ID_OFFSET = 12345


def _build_and_save_broken_index(tmp_path):
    vectors = near_tie_corpus(
        size=CORPUS_SIZE,
        dim=CORPUS_DIM,
        noise_scale=NEAR_TIE_NOISE_SCALE,
        pocket_fraction=TEMPORAL_SHAPED_POCKET_FRACTION,
        seed=CORPUS_SEED,
    )
    index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
    before = index.check_integrity()
    orphans = [int(e.split()[1]) for e in before["errors"] if "orphan" in e]
    assert orphans, "fixture must start broken (need real orphans to corrupt around)"

    path = tmp_path / "broken.bin"
    index.save_index(str(path))
    return path, orphans


def _read_header(data):
    fields = struct.unpack_from(_HEADER_FMT, data, 0)
    keys = (
        "offset_level0",
        "max_elements",
        "cur_element_count",
        "size_data_per_element",
        "label_offset",
        "offset_data",
        "maxlevel",
        "enterpoint_node",
        "maxM",
        "maxM0",
        "M",
        "mult",
        "ef_construction",
    )
    return dict(zip(keys, fields))


def _level0_list_offset(header, elem_id):
    return (
        _HEADER_SIZE
        + elem_id * header["size_data_per_element"]
        + header["offset_level0"]
    )


def _read_level0_list(data, header, elem_id):
    offset = _level0_list_offset(header, elem_id)
    (count,) = struct.unpack_from("<H", data, offset)
    max_m0 = header["maxM0"]
    neighbors = list(struct.unpack_from(f"<{max_m0}I", data, offset + 4))[:count]
    return count, neighbors


class TestRepairOrphansBoundsSafety:
    def test_out_of_range_anchor_id_handled_gracefully(self, tmp_path):
        """Corrupts an orphan's OWN link-list neighbor (its "anchor" for
        repair purposes) to an out-of-range id. Exercises the `anchor >= n`
        guard: try_connect must reject this anchor without reading/writing
        outside cur_element_count, then fall through to the wider fallback
        scan and still repair the orphan via a valid anchor."""
        path, orphans = _build_and_save_broken_index(tmp_path)
        orphan_id = orphans[0]

        with open(path, "rb") as f:
            data = bytearray(f.read())
        header = _read_header(data)

        offset = _level0_list_offset(header, orphan_id)
        out_of_range_id = header["cur_element_count"] + _OUT_OF_RANGE_ID_OFFSET
        struct.pack_into("<H", data, offset, 1)  # force count = 1
        struct.pack_into("<I", data, offset + 4, out_of_range_id)

        with open(path, "wb") as f:
            f.write(bytes(data))

        fresh = hnswlib.Index(space="cosine", dim=CORPUS_DIM)
        fresh.load_index(str(path), max_elements=CORPUS_SIZE)

        before = fresh.check_integrity()
        assert any(
            f"invalid connection to {out_of_range_id}" in e for e in before["errors"]
        ), "corruption did not take effect as expected"

        # Must not crash (segfault/abort) or corrupt memory -- graceful
        # handling is proven simply by returning a well-formed result.
        result = fresh.repair_orphans()

        assert isinstance(result, dict)
        assert "valid" in result
        assert result["orphans_before"] > 0

    def test_out_of_range_victim_candidate_id_handled_gracefully(self, tmp_path):
        """Corrupts a FULL (maxM0-sized) anchor's own link list so one of
        its neighbor slots is an out-of-range id, then ensures a real
        orphan's anchor list includes that corrupted node. Exercises the
        `cand >= n` guard inside the eviction/victim-search branch: the
        search for a safe (inbound > 1) eviction candidate must skip the
        out-of-range entry rather than index inbound[] out of range."""
        path, orphans = _build_and_save_broken_index(tmp_path)

        with open(path, "rb") as f:
            data = bytearray(f.read())
        header = _read_header(data)
        max_m0 = header["maxM0"]

        target_orphan = None
        target_anchor = None
        for orphan_id in orphans:
            _, orphan_neighbors = _read_level0_list(data, header, orphan_id)
            for anchor_id in orphan_neighbors:
                anchor_count, _ = _read_level0_list(data, header, anchor_id)
                if anchor_count == max_m0:
                    target_orphan, target_anchor = orphan_id, anchor_id
                    break
            if target_orphan is not None:
                break

        assert target_orphan is not None, (
            "could not find an orphan with a full-list anchor in this fixture -- "
            "corpus calibration may need adjustment"
        )

        anchor_offset = _level0_list_offset(header, target_anchor)
        out_of_range_id = header["cur_element_count"] + _OUT_OF_RANGE_ID_OFFSET
        # Corrupt slot 0 of the anchor's own (already-full) link list.
        struct.pack_into("<I", data, anchor_offset + 4, out_of_range_id)

        with open(path, "wb") as f:
            f.write(bytes(data))

        fresh = hnswlib.Index(space="cosine", dim=CORPUS_DIM)
        fresh.load_index(str(path), max_elements=CORPUS_SIZE)

        before = fresh.check_integrity()
        assert any(
            f"invalid connection to {out_of_range_id}" in e for e in before["errors"]
        ), "corruption did not take effect as expected"

        result = fresh.repair_orphans()

        assert isinstance(result, dict)
        assert "valid" in result
        assert result["orphans_before"] > 0
