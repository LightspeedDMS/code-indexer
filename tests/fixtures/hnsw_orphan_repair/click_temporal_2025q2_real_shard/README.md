Real-world regression fixture: staging HNSW shard with known orphans

This is a REAL (not synthetic) HNSW index shard pulled from the staging
cidx-server (golden repo `click`, collection
`code-indexer-temporal-voyage_context_4-2025Q2`) via MCP SSH on 2026-07-11
for Story #1359 (Epic #1333) AC6 validation. No modifications were made to
the staging server; this is an unmodified copy of the on-disk artifact.

Contents:
- `hnsw_index.bin` -- the real HNSW binary index (484 elements, dim=1024,
  M=16, ef_construction=200, cosine space)
- `collection_meta.json` -- the matching collection metadata (id_mapping
  references public git commit hashes from the pallets/click open-source
  project, CIDX's standard test golden repo -- no secrets, no PII, no
  internal hostnames)

Known regression (recorded on issue #1359): elements 270-273 have zero
inbound HNSW connections (orphans) pre-repair. These four elements are
bit-identical to each other and to a fifth element (274, which retained
connectivity) -- i.e. the EXACT-TIE (race) regime (S1's regime 2: a
multi-threaded `add_items` back-link race on genuinely duplicate content),
not the near-tie regime. This is the exact trailing-ID fingerprint the
synthetic corpus generator (`tests/utils/hnsw_orphan_corpus.py`) could not
reproduce.

Used by: `tests/unit/hnsw_orphan_repair/test_repair_orphans_real_staging_shard_1359.py`
-- confirms `check_integrity()` reports the known 4 orphans pre-repair,
runs `repair_orphans()`, and confirms 0 orphans + restored recall
post-repair, codifying the exact real-world regression this story fixed
(as distinct from the synthetic corpus classes S1's own tests already
cover).
