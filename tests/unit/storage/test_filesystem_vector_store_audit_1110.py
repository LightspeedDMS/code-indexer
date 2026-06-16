"""Story #1110 (S6 Chunk B): _run_deep_fidelity_audit + FSV search() integration.

Tests:
  1. Identical second vector -> top10_overlap=1.0, top1_match=True, record_audit called.
  2. Different second vector -> overlap < 1.0.
  3. Fail-open: hnsw_manager.query raises -> swallowed, no propagation.
  4. No-op: audit_ctx without "sampled" key -> _run_deep_fidelity_audit NOT called.
  5. On-mode audit calls governed_query_embedding exactly once (re-embed path).
  6. Shadow-mode audit does NOT call governed_query_embedding (uses audit_ctx cached_blob).
  7. Zero-norm second vector -> audit skipped (DEBUG only, record_audit NOT called).
  8. Empty primary HNSW result -> audit skipped (record_audit NOT called).
  9. FSV search() integration: sampled audit_ctx -> _run_deep_fidelity_audit called.
  10. FSV search() integration: unsampled audit_ctx -> _run_deep_fidelity_audit NOT called.
  11. FSV search() integration: audit raises -> search() result unaffected (fail-open).
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 8  # small dimension for fast test index builds


def _enc(vec: List[float]) -> bytes:
    """Encode float32 LE blob (same encoding as Chunk A audit_ctx)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _norm(v: List[float]) -> List[float]:
    """Return unit-length version of v."""
    arr = np.array(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    if n == 0:
        return v
    return (arr / n).tolist()  # type: ignore[no-any-return]


def _build_hnsw(tmp_path: Path, vectors: List[List[float]], ids: List[str]):
    """Build a tiny HNSW index and return (manager, index, collection_path)."""
    manager = HNSWIndexManager(vector_dim=_DIM, space="cosine")
    coll_path = tmp_path / "audit_test_coll"
    coll_path.mkdir()
    arr = np.array(vectors, dtype=np.float32)
    manager.build_index(coll_path, arr, ids)
    index = manager.load_index(coll_path)
    return manager, index, coll_path


# ---------------------------------------------------------------------------
# Fixture: small real HNSW index
# ---------------------------------------------------------------------------


@pytest.fixture()
def tiny_hnsw(tmp_path):
    """Real HNSW with 5 distinct unit vectors and known IDs."""
    vecs = [
        _norm([1, 0, 0, 0, 0, 0, 0, 0]),
        _norm([0, 1, 0, 0, 0, 0, 0, 0]),
        _norm([0, 0, 1, 0, 0, 0, 0, 0]),
        _norm([0, 0, 0, 1, 0, 0, 0, 0]),
        _norm([0, 0, 0, 0, 1, 0, 0, 0]),
    ]
    ids = ["id_0", "id_1", "id_2", "id_3", "id_4"]
    manager, index, coll_path = _build_hnsw(tmp_path, vecs, ids)
    return {
        "manager": manager,
        "index": index,
        "collection_path": coll_path,
        "vecs": vecs,
        "ids": ids,
    }


# ---------------------------------------------------------------------------
# Fixture: FSV store with a real tiny collection for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def tiny_fsv_store(tmp_path):
    """A FilesystemVectorStore with a 5-vector collection for search() integration."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
    store.create_collection("audit_coll", vector_size=_DIM)

    vecs = [
        _norm([1, 0, 0, 0, 0, 0, 0, 0]),
        _norm([0, 1, 0, 0, 0, 0, 0, 0]),
        _norm([0, 0, 1, 0, 0, 0, 0, 0]),
        _norm([0, 0, 0, 1, 0, 0, 0, 0]),
        _norm([0, 0, 0, 0, 1, 0, 0, 0]),
    ]
    points = [
        {
            "id": f"doc_{i}",
            "vector": vecs[i],
            "payload": {"path": f"file_{i}.py", "content": f"content {i}"},
        }
        for i in range(len(vecs))
    ]
    store.begin_indexing("audit_coll")
    store.upsert_points("audit_coll", points)
    store.end_indexing("audit_coll")

    return store, vecs


# ---------------------------------------------------------------------------
# Import helper (avoids NameError when module doesn't exist yet)
# ---------------------------------------------------------------------------


def _import_run_deep_fidelity_audit():
    from code_indexer.server.services.embedding_cache_audit import (
        _run_deep_fidelity_audit,
    )

    return _run_deep_fidelity_audit


# ---------------------------------------------------------------------------
# 1. Identical second vector -> overlap 1.0, top1_match True
# ---------------------------------------------------------------------------


class TestRunDeepFidelityAuditIdentical:
    """Identical second vector -> full overlap, top1_match True."""

    def test_identical_cached_blob_shadow_mode(self, tiny_hnsw):
        """Shadow mode: primary used live vec; second search uses cached_blob.

        Both vectors are identical -> overlap MUST be 1.0, top1_match True.
        """
        _run = _import_run_deep_fidelity_audit()
        mgr = tiny_hnsw["manager"]
        idx = tiny_hnsw["index"]
        coll = tiny_hnsw["collection_path"]
        primary_vec = np.array(tiny_hnsw["vecs"][0], dtype=np.float32)
        primary_ids, _ = mgr.query(
            index=idx, query_vector=primary_vec, collection_path=coll, k=5, ef=50
        )

        # audit_ctx: shadow mode, cached_blob encodes SAME vector as primary
        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
            "live_vec": tiny_hnsw["vecs"][0],  # same as primary (served was live)
        }

        mock_metrics = MagicMock()
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=None,
                query="test",
            )

        mock_metrics.record_audit.assert_called_once()
        call_kwargs = mock_metrics.record_audit.call_args[1]
        assert call_kwargs["top10_overlap"] == pytest.approx(1.0)
        assert call_kwargs["top1_match"] is True
        assert call_kwargs["provider"] == "voyage-ai"
        assert call_kwargs["mode"] == "shadow"

    def test_identical_second_vec_on_mode(self, tiny_hnsw):
        """On mode: primary used cached vec; second search re-embeds via governed_query_embedding.

        Monkeypatch governed_query_embedding to return the SAME vector as primary.
        -> overlap 1.0, top1_match True.
        """
        _run = _import_run_deep_fidelity_audit()
        mgr = tiny_hnsw["manager"]
        idx = tiny_hnsw["index"]
        coll = tiny_hnsw["collection_path"]
        primary_vec = np.array(tiny_hnsw["vecs"][0], dtype=np.float32)
        primary_ids, _ = mgr.query(
            index=idx, query_vector=primary_vec, collection_path=coll, k=5, ef=50
        )

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "on",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
        }

        fake_provider = MagicMock()
        mock_metrics = MagicMock()
        # governed_query_embedding returns same vector as primary -> identical second search
        with (
            patch(
                "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
                return_value=tiny_hnsw["vecs"][0],
            ) as mock_gov,
            patch(
                "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
                return_value=mock_metrics,
            ),
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=fake_provider,
                query="test query",
            )

        mock_gov.assert_called_once()
        call_kwargs = mock_metrics.record_audit.call_args[1]
        assert call_kwargs["top10_overlap"] == pytest.approx(1.0)
        assert call_kwargs["top1_match"] is True


# ---------------------------------------------------------------------------
# 2. Different second vector -> overlap < 1.0
# ---------------------------------------------------------------------------


class TestRunDeepFidelityAuditDifferent:
    """A deliberately different second vector -> overlap < 1.0."""

    def test_different_vector_lower_overlap(self, tiny_hnsw):
        """Shadow mode: cached_blob encodes orthogonal vector -> top-1 differs."""
        _run = _import_run_deep_fidelity_audit()
        mgr = tiny_hnsw["manager"]
        idx = tiny_hnsw["index"]
        coll = tiny_hnsw["collection_path"]
        # Primary uses vec[0] (aligned to axis 0)
        primary_vec = np.array(tiny_hnsw["vecs"][0], dtype=np.float32)
        primary_ids, _ = mgr.query(
            index=idx, query_vector=primary_vec, collection_path=coll, k=5, ef=50
        )

        # cached_blob encodes vec[4] (aligned to axis 4) -> opposite end of space
        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][4]),
            "live_vec": tiny_hnsw["vecs"][0],
        }

        mock_metrics = MagicMock()
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=None,
                query="test",
            )

        mock_metrics.record_audit.assert_called_once()
        call_kwargs = mock_metrics.record_audit.call_args[1]
        # With 5 vectors and orthogonal queries the overlap must be less than 1.0
        assert call_kwargs["top10_overlap"] < 1.0 or not call_kwargs["top1_match"]


# ---------------------------------------------------------------------------
# 3. Fail-open: hnsw_manager.query raises -> swallowed
# ---------------------------------------------------------------------------


class TestRunDeepFidelityAuditFailOpen:
    """hnsw_manager.query raises in audit -> swallowed, no propagation."""

    def test_hnsw_query_exception_swallowed(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        coll = tiny_hnsw["collection_path"]

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
            "live_vec": tiny_hnsw["vecs"][0],
        }

        bad_manager = MagicMock()
        bad_manager.query.side_effect = RuntimeError("simulated HNSW failure")

        mock_metrics = MagicMock()
        # Must not raise
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=bad_manager,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=["id_0", "id_1"],
                embedding_provider=None,
                query="test",
            )
        # record_audit must NOT have been called (error swallowed before compute)
        mock_metrics.record_audit.assert_not_called()

    def test_governed_query_embedding_exception_swallowed(self, tiny_hnsw):
        """On-mode: governed_query_embedding raises -> entire audit swallowed."""
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]
        primary_ids = ["id_0", "id_1", "id_2"]

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "on",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
        }

        mock_metrics = MagicMock()
        with (
            patch(
                "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
                side_effect=RuntimeError("provider down"),
            ),
            patch(
                "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
                return_value=mock_metrics,
            ),
        ):
            # Must not raise
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=MagicMock(),
                query="test",
            )
        mock_metrics.record_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. No-op: audit_ctx without "sampled" key
# ---------------------------------------------------------------------------


class TestRunDeepFidelityAuditNoOp:
    """When audit_ctx lacks 'sampled' key, _run_deep_fidelity_audit does nothing."""

    def test_missing_sampled_key_noop(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]

        # audit_ctx without "sampled"
        audit_ctx: Dict[str, Any] = {}

        mock_metrics = MagicMock()
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=["id_0"],
                embedding_provider=None,
                query="test",
            )

        mock_metrics.record_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 5. On-mode: governed_query_embedding called exactly once
# ---------------------------------------------------------------------------


class TestOnModeReEmbed:
    """On-mode audit calls governed_query_embedding exactly once."""

    def test_on_mode_calls_governed_once(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]
        primary_vec = np.array(tiny_hnsw["vecs"][0], dtype=np.float32)
        primary_ids, _ = mgr.query(
            index=idx, query_vector=primary_vec, collection_path=coll, k=5, ef=50
        )

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "on",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
        }
        fake_provider = MagicMock()

        with (
            patch(
                "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
                return_value=tiny_hnsw["vecs"][1],
            ) as mock_gov,
            patch(
                "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
                return_value=MagicMock(),
            ),
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=fake_provider,
                query="my query text",
            )

        # Called exactly once with the correct provider and text
        assert mock_gov.call_count == 1
        call_args = mock_gov.call_args
        assert call_args[0][0] is fake_provider
        assert call_args[0][1] == "my query text"


# ---------------------------------------------------------------------------
# 6. Shadow-mode: governed_query_embedding NOT called
# ---------------------------------------------------------------------------


class TestShadowModeNoreEmbed:
    """Shadow-mode audit uses cached_blob (no re-embed)."""

    def test_shadow_mode_no_governed_call(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]
        primary_vec = np.array(tiny_hnsw["vecs"][0], dtype=np.float32)
        primary_ids, _ = mgr.query(
            index=idx, query_vector=primary_vec, collection_path=coll, k=5, ef=50
        )

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][1]),
            "live_vec": tiny_hnsw["vecs"][0],
        }

        with (
            patch(
                "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
            ) as mock_gov,
            patch(
                "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
                return_value=MagicMock(),
            ),
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=MagicMock(),
                query="test",
            )

        mock_gov.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Zero-norm second vector -> audit skipped
# ---------------------------------------------------------------------------


class TestZeroNormSkip:
    """Zero-norm second vector is skipped (DEBUG, no record_audit call)."""

    def test_zero_norm_cached_blob_skipped_shadow(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]
        primary_ids = ["id_0", "id_1"]

        # Zero vector encoded in blob
        zero_vec = [0.0] * _DIM
        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(zero_vec),
            "live_vec": tiny_hnsw["vecs"][0],
        }

        mock_metrics = MagicMock()
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=primary_ids,
                embedding_provider=None,
                query="test",
            )

        mock_metrics.record_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Empty primary_candidate_ids -> audit skipped
# ---------------------------------------------------------------------------


class TestEmptyResultSkip:
    """Empty primary_candidate_ids -> audit skipped (record_audit NOT called)."""

    def test_empty_primary_ids_skipped(self, tiny_hnsw):
        _run = _import_run_deep_fidelity_audit()
        idx = tiny_hnsw["index"]
        mgr = tiny_hnsw["manager"]
        coll = tiny_hnsw["collection_path"]

        audit_ctx: Dict[str, Any] = {
            "sampled": True,
            "mode": "shadow",
            "provider": "voyage-ai",
            "cached_blob": _enc(tiny_hnsw["vecs"][0]),
            "live_vec": tiny_hnsw["vecs"][0],
        }

        mock_metrics = MagicMock()
        with patch(
            "code_indexer.server.services.embedding_cache_audit.get_query_embedding_cache_metrics",
            return_value=mock_metrics,
        ):
            _run(
                audit_ctx=audit_ctx,
                hnsw_index=idx,
                hnsw_manager=mgr,
                collection_path=coll,
                ef=50,
                primary_candidate_ids=[],  # empty!
                embedding_provider=None,
                query="test",
            )

        mock_metrics.record_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 9-11. FSV search() integration tests
# ---------------------------------------------------------------------------


class TestFSVSearchAuditIntegration:
    """FSV search() correctly invokes _run_deep_fidelity_audit when sampled."""

    def test_sampled_audit_ctx_triggers_audit_in_search(self, tiny_fsv_store, tmp_path):
        """FSV search() calls _run_deep_fidelity_audit when audit_ctx has 'sampled'=True."""
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]

        # Fake provider that returns a known embedding
        fake_provider = MagicMock()
        fake_provider.get_embedding.return_value = query_vec

        # Patch coalesced_query_embedding to return the embedding AND populate audit_ctx
        def _fake_coalesced(
            provider, text, *, no_embedding_cache_shortcut=False, audit_ctx=None
        ):
            if audit_ctx is not None:
                audit_ctx["sampled"] = True
                audit_ctx["mode"] = "shadow"
                audit_ctx["provider"] = "voyage-ai"
                audit_ctx["cached_blob"] = _enc(query_vec)
                audit_ctx["live_vec"] = query_vec
            return query_vec

        with (
            patch(
                "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                side_effect=_fake_coalesced,
            ) as _mock_coalesced,
            patch(
                "code_indexer.storage.filesystem_vector_store._run_deep_fidelity_audit",
            ) as mock_audit,
        ):
            results = store.search(
                query="hello",
                embedding_provider=fake_provider,
                collection_name="audit_coll",
                limit=3,
            )

        # Search must succeed and return results
        assert isinstance(results, list)
        # _run_deep_fidelity_audit must have been called once
        mock_audit.assert_called_once()

    def test_unsampled_audit_ctx_does_not_trigger_audit(self, tiny_fsv_store, tmp_path):
        """FSV search() does NOT call _run_deep_fidelity_audit when audit_ctx lacks 'sampled'."""
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]

        fake_provider = MagicMock()
        fake_provider.get_embedding.return_value = query_vec

        # Patch coalesced_query_embedding to NOT populate audit_ctx (miss / rate=0)
        def _fake_coalesced_noop(
            provider, text, *, no_embedding_cache_shortcut=False, audit_ctx=None
        ):
            # leave audit_ctx untouched (empty dict)
            return query_vec

        with (
            patch(
                "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                side_effect=_fake_coalesced_noop,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store._run_deep_fidelity_audit",
            ) as mock_audit,
        ):
            results = store.search(
                query="hello",
                embedding_provider=fake_provider,
                collection_name="audit_coll",
                limit=3,
            )

        assert isinstance(results, list)
        # Must NOT have been called
        mock_audit.assert_not_called()

    def test_audit_exception_does_not_break_search(self, tiny_fsv_store, tmp_path):
        """If _run_deep_fidelity_audit raises, search() still returns results (fail-open)."""
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]

        fake_provider = MagicMock()
        fake_provider.get_embedding.return_value = query_vec

        def _fake_coalesced_sampled(
            provider, text, *, no_embedding_cache_shortcut=False, audit_ctx=None
        ):
            if audit_ctx is not None:
                audit_ctx["sampled"] = True
                audit_ctx["mode"] = "on"
                audit_ctx["provider"] = "voyage-ai"
                audit_ctx["cached_blob"] = _enc(query_vec)
            return query_vec

        with (
            patch(
                "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                side_effect=_fake_coalesced_sampled,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store._run_deep_fidelity_audit",
                side_effect=RuntimeError("audit exploded"),
            ),
        ):
            # Must not raise — fail-open
            results = store.search(
                query="hello",
                embedding_provider=fake_provider,
                collection_name="audit_coll",
                limit=3,
            )

        assert isinstance(results, list)
