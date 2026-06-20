"""Tests for quarterly temporal index sharding (Story #1171).

AC7: Covers naming helpers, indexer commit routing, and query ordering.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


# ---------------------------------------------------------------------------
# Naming helpers (AC1)
# ---------------------------------------------------------------------------


class TestQuarterSuffix:
    """Test quarter_suffix(datetime) -> 'YYYYQn'."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            quarter_suffix,
        )

        self.quarter_suffix = quarter_suffix

    def test_quarter_suffix_q1(self):
        """Jan, Feb, Mar all return YYYYQ1."""
        assert self.quarter_suffix(datetime(2024, 1, 15)) == "2024Q1"
        assert self.quarter_suffix(datetime(2024, 2, 28)) == "2024Q1"
        assert self.quarter_suffix(datetime(2024, 3, 31)) == "2024Q1"

    def test_quarter_suffix_q2(self):
        """Apr, May, Jun all return YYYYQ2."""
        assert self.quarter_suffix(datetime(2024, 4, 1)) == "2024Q2"
        assert self.quarter_suffix(datetime(2024, 5, 15)) == "2024Q2"
        assert self.quarter_suffix(datetime(2024, 6, 30)) == "2024Q2"

    def test_quarter_suffix_q3(self):
        """Jul, Aug, Sep all return YYYYQ3."""
        assert self.quarter_suffix(datetime(2024, 7, 1)) == "2024Q3"
        assert self.quarter_suffix(datetime(2024, 8, 15)) == "2024Q3"
        assert self.quarter_suffix(datetime(2024, 9, 30)) == "2024Q3"

    def test_quarter_suffix_q4(self):
        """Oct, Nov, Dec all return YYYYQ4."""
        assert self.quarter_suffix(datetime(2024, 10, 1)) == "2024Q4"
        assert self.quarter_suffix(datetime(2024, 11, 15)) == "2024Q4"
        assert self.quarter_suffix(datetime(2024, 12, 31)) == "2024Q4"

    def test_quarter_suffix_boundary_jan1(self):
        """Jan 1 is Q1."""
        assert self.quarter_suffix(datetime(2025, 1, 1)) == "2025Q1"

    def test_quarter_suffix_boundary_dec31(self):
        """Dec 31 is Q4."""
        assert self.quarter_suffix(datetime(2023, 12, 31)) == "2023Q4"


class TestGetShardCollectionName:
    """Test get_shard_collection_name(model_name, datetime) -> collection string."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_shard_collection_name,
        )

        self.get_shard_collection_name = get_shard_collection_name

    def test_get_shard_collection_name_voyage(self):
        """Voyage model produces correct shard name."""
        result = self.get_shard_collection_name("voyage-code-3", datetime(2024, 8, 1))
        assert result == "code-indexer-temporal-voyage_code_3-2024Q3"

    def test_get_shard_collection_name_cohere(self):
        """Cohere model produces correct shard name."""
        result = self.get_shard_collection_name("embed-v4.0", datetime(2024, 4, 15))
        assert result == "code-indexer-temporal-embed_v4_0-2024Q2"


class TestIsShardedTemporalCollection:
    """Test is_sharded_temporal_collection(name) -> bool."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_sharded_temporal_collection,
            is_temporal_collection,
        )

        self.is_sharded = is_sharded_temporal_collection
        self.is_temporal = is_temporal_collection

    def test_is_sharded_temporal_collection_true(self):
        """Sharded name returns True."""
        assert self.is_sharded("code-indexer-temporal-voyage_code_3-2024Q3") is True

    def test_is_sharded_temporal_collection_false_legacy(self):
        """Legacy monolithic name returns False."""
        assert self.is_sharded("code-indexer-temporal") is False

    def test_is_sharded_temporal_collection_false_base(self):
        """Base provider name (without quarter) returns False."""
        assert self.is_sharded("code-indexer-temporal-voyage_code_3") is False

    def test_is_temporal_collection_still_true_for_shards(self):
        """is_temporal_collection() returns True for sharded names — regression guard."""
        assert self.is_temporal("code-indexer-temporal-voyage_code_3-2024Q3") is True
        assert self.is_temporal("code-indexer-temporal-embed_v4_0-2023Q1") is True


class TestGetQuarterRange:
    """Test get_quarter_range(year, quarter) -> (start_dt, end_dt) UTC."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_quarter_range,
        )

        self.get_quarter_range = get_quarter_range

    def test_get_quarter_range_q1(self):
        """Q1 range is Jan 1 to Apr 1 UTC."""
        start, end = self.get_quarter_range(2024, 1)
        assert start == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert end == datetime(2024, 4, 1, tzinfo=timezone.utc)

    def test_get_quarter_range_q4(self):
        """Q4 range is Oct 1 to (next year) Jan 1 UTC."""
        start, end = self.get_quarter_range(2024, 4)
        assert start == datetime(2024, 10, 1, tzinfo=timezone.utc)
        assert end == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_get_quarter_range_q2(self):
        """Q2 range is Apr 1 to Jul 1 UTC."""
        start, end = self.get_quarter_range(2024, 2)
        assert start == datetime(2024, 4, 1, tzinfo=timezone.utc)
        assert end == datetime(2024, 7, 1, tzinfo=timezone.utc)

    def test_get_quarter_range_q3(self):
        """Q3 range is Jul 1 to Oct 1 UTC."""
        start, end = self.get_quarter_range(2024, 3)
        assert start == datetime(2024, 7, 1, tzinfo=timezone.utc)
        assert end == datetime(2024, 10, 1, tzinfo=timezone.utc)


class TestGetOverlappingShards:
    """Test get_overlapping_shards(model_name, index_path, start, end) -> List[str]."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_overlapping_shards,
        )

        self.get_overlapping_shards = get_overlapping_shards

    def test_get_overlapping_shards_no_dirs(self, tmp_path):
        """Returns [] when index_path doesn't exist."""
        result = self.get_overlapping_shards(
            "voyage-code-3", tmp_path / "nonexistent", None, None
        )
        assert result == []

    def test_get_overlapping_shards_date_range(self, tmp_path):
        """Query [2024-04-01, 2024-09-30] returns Q2+Q3 not Q1 or Q4."""
        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        start = datetime(2024, 4, 1, tzinfo=timezone.utc)
        end = datetime(2024, 9, 30, tzinfo=timezone.utc)
        result = self.get_overlapping_shards("voyage-code-3", tmp_path, start, end)

        assert f"{base}-2024Q2" in result
        assert f"{base}-2024Q3" in result
        assert f"{base}-2024Q1" not in result
        assert f"{base}-2024Q4" not in result

    def test_get_overlapping_shards_all_time(self, tmp_path):
        """None start/end returns all shards."""
        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2024Q1", "2024Q2", "2024Q3"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        result = self.get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        assert len(result) == 3
        assert f"{base}-2024Q1" in result
        assert f"{base}-2024Q2" in result
        assert f"{base}-2024Q3" in result

    def test_get_overlapping_shards_includes_legacy(self, tmp_path):
        """Creates legacy dir + shards; all-time query includes legacy at end."""
        base = "code-indexer-temporal-voyage_code_3"
        (tmp_path / f"{base}-2024Q1").mkdir()
        (tmp_path / f"{base}-2024Q2").mkdir()
        # Legacy monolithic collection
        (tmp_path / base).mkdir()

        result = self.get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        assert result[-1] == base  # Legacy at end
        assert f"{base}-2024Q1" in result
        assert f"{base}-2024Q2" in result

    def test_get_overlapping_shards_ascending_order(self, tmp_path):
        """Returns shards sorted chronologically (ascending)."""
        base = "code-indexer-temporal-voyage_code_3"
        # Create in reverse order to verify sorting
        for suffix in ["2024Q4", "2024Q1", "2024Q3", "2024Q2"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        result = self.get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        assert result == [
            f"{base}-2024Q1",
            f"{base}-2024Q2",
            f"{base}-2024Q3",
            f"{base}-2024Q4",
        ]

    def test_get_overlapping_shards_legacy_only(self, tmp_path):
        """Only legacy dir on disk, all-time query returns legacy."""
        base = "code-indexer-temporal-voyage_code_3"
        (tmp_path / base).mkdir()

        result = self.get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        assert result == [base]

    def test_get_overlapping_shards_open_ended_start(self, tmp_path):
        """start=None, end=end-of-Q2: includes Q1, Q2, not Q3."""
        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2024Q1", "2024Q2", "2024Q3"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        # end just before Q3 starts (Q3 start = 2024-07-01)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)
        result = self.get_overlapping_shards("voyage-code-3", tmp_path, None, end)

        assert f"{base}-2024Q1" in result
        assert f"{base}-2024Q2" in result
        assert f"{base}-2024Q3" not in result

    def test_get_overlapping_shards_open_ended_end(self, tmp_path):
        """start=Q3 start, end=None: includes Q3, Q4, not Q1/Q2."""
        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        # Start at the beginning of Q3
        start = datetime(2024, 7, 1, tzinfo=timezone.utc)
        result = self.get_overlapping_shards("voyage-code-3", tmp_path, start, None)

        assert f"{base}-2024Q3" in result
        assert f"{base}-2024Q4" in result
        assert f"{base}-2024Q1" not in result
        assert f"{base}-2024Q2" not in result


# ---------------------------------------------------------------------------
# Indexer routing (AC2)
# ---------------------------------------------------------------------------


def _make_commit(hash_val: str, year: int, month: int, day: int):
    """Create a minimal CommitInfo-like object with a timestamp."""
    from code_indexer.services.temporal.models import CommitInfo

    ts = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    return CommitInfo(
        hash=hash_val,
        timestamp=ts,
        author_name="Test Author",
        author_email="test@example.com",
        message="test commit",
        parent_hashes=[],
    )


class TestIndexerShardRouting:
    """Verify that index_commits() routes commits to correct quarterly shards."""

    def _make_mocks(self, tmp_path: Path):
        """Build the mock config_manager and vector_store needed for TemporalIndexer."""
        mock_config = Mock()
        mock_config.voyage_ai = Mock()
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.voyage_ai.parallel_requests = 4
        mock_config.voyage_ai.temporal_parallel_requests = None
        mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
        mock_config.cohere = Mock()
        mock_config.cohere.parallel_requests = 4
        mock_config.cohere.temporal_parallel_requests = None
        mock_config.embedding_provider = "voyage-ai"
        mock_config.temporal = Mock()
        mock_config.temporal.diff_context_lines = 3
        mock_config.file_extensions = []
        mock_config.override_config = None
        mock_config.codebase_dir = tmp_path

        mock_config_manager = Mock()
        mock_config_manager.get_config.return_value = mock_config
        mock_config_manager.config_path = tmp_path / ".code-indexer" / "config.json"

        mock_vector_store = Mock()
        mock_vector_store.project_root = tmp_path
        mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
        mock_vector_store.collection_exists.return_value = True
        mock_vector_store.load_id_index.return_value = set()
        mock_vector_store.begin_indexing.return_value = None
        mock_vector_store.end_indexing.return_value = {"status": "ok"}
        mock_vector_store.upsert_points.return_value = None

        return mock_config_manager, mock_vector_store, mock_config

    def test_indexer_routes_commits_to_correct_shard(self, tmp_path):
        """Commits with timestamps in different quarters go to correct shard collections."""
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        mock_config_manager, mock_vector_store, mock_config = self._make_mocks(tmp_path)

        # Collection name is the provider base (pre-sharding)
        base_collection = "code-indexer-temporal-voyage_code_3"
        indexer = TemporalIndexer(
            mock_config_manager, mock_vector_store, collection_name=base_collection
        )

        # Create commits in Q1, Q2, Q3 of 2024
        q1_commit = _make_commit("aaa111", 2024, 2, 15)  # Feb 2024 -> Q1
        q2_commit = _make_commit("bbb222", 2024, 5, 20)  # May 2024 -> Q2
        q3_commit = _make_commit("ccc333", 2024, 9, 10)  # Sep 2024 -> Q3

        commits = [q1_commit, q2_commit, q3_commit]

        # Patch _process_commits_parallel to intercept collection_name at call time
        collection_names_at_call = []

        def fake_process(
            self_ref, shard_commits, emb_provider, vec_manager, prog_cb, reconcile
        ):
            collection_names_at_call.append(self_ref.collection_name)
            return len(shard_commits), len(shard_commits), len(shard_commits) * 3

        with patch.object(
            indexer, "_process_commits_parallel", fake_process.__get__(indexer)
        ):
            with patch.object(indexer, "_get_commit_history", return_value=commits):
                with patch.object(indexer, "_get_current_branch", return_value="main"):
                    with patch.object(indexer, "_save_temporal_metadata"):
                        with patch(
                            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                        ) as mock_factory:
                            mock_factory.create.return_value = Mock()
                            with patch(
                                "code_indexer.services.temporal.temporal_indexer.VectorCalculationManager"
                            ) as mock_vcm:
                                mock_vcm.return_value.__enter__ = Mock(
                                    return_value=Mock()
                                )
                                mock_vcm.return_value.__exit__ = Mock(
                                    return_value=False
                                )
                                indexer.index_commits()

        # Verify correct shards were used
        assert "code-indexer-temporal-voyage_code_3-2024Q1" in collection_names_at_call
        assert "code-indexer-temporal-voyage_code_3-2024Q2" in collection_names_at_call
        assert "code-indexer-temporal-voyage_code_3-2024Q3" in collection_names_at_call
        # Verify original collection_name is restored after
        assert indexer.collection_name == base_collection

    def test_indexer_single_shard_single_call(self, tmp_path):
        """All commits in same quarter result in only one shard being used."""
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        mock_config_manager, mock_vector_store, mock_config = self._make_mocks(tmp_path)

        base_collection = "code-indexer-temporal-voyage_code_3"
        indexer = TemporalIndexer(
            mock_config_manager, mock_vector_store, collection_name=base_collection
        )

        # All commits in Q2 2024
        commits = [
            _make_commit("aaa111", 2024, 4, 1),
            _make_commit("bbb222", 2024, 5, 15),
            _make_commit("ccc333", 2024, 6, 30),
        ]

        collection_names_at_call = []

        def fake_process(
            self_ref, shard_commits, emb_provider, vec_manager, prog_cb, reconcile
        ):
            collection_names_at_call.append(self_ref.collection_name)
            return len(shard_commits), len(shard_commits), len(shard_commits) * 3

        with patch.object(
            indexer, "_process_commits_parallel", fake_process.__get__(indexer)
        ):
            with patch.object(indexer, "_get_commit_history", return_value=commits):
                with patch.object(indexer, "_get_current_branch", return_value="main"):
                    with patch.object(indexer, "_save_temporal_metadata"):
                        with patch(
                            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                        ) as mock_factory:
                            mock_factory.create.return_value = Mock()
                            with patch(
                                "code_indexer.services.temporal.temporal_indexer.VectorCalculationManager"
                            ) as mock_vcm:
                                mock_vcm.return_value.__enter__ = Mock(
                                    return_value=Mock()
                                )
                                mock_vcm.return_value.__exit__ = Mock(
                                    return_value=False
                                )
                                indexer.index_commits()

        # Only one shard should have been used
        assert len(collection_names_at_call) == 1
        assert (
            collection_names_at_call[0] == "code-indexer-temporal-voyage_code_3-2024Q2"
        )


# ---------------------------------------------------------------------------
# Query ordering (AC3)
# ---------------------------------------------------------------------------


class TestQueryShardOrder:
    """Verify shards are queried in ascending chronological order."""

    def test_query_shards_sequential_order(self, tmp_path):
        """Shards queried in ascending chronological order (Q1 before Q2 before Q3)."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_overlapping_shards,
        )

        base = "code-indexer-temporal-voyage_code_3"
        # Create shards in reverse order to verify sort
        for suffix in ["2024Q3", "2024Q1", "2024Q2"]:
            (tmp_path / f"{base}-{suffix}").mkdir()

        result = get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        # Must be in ascending chronological order
        assert result == [
            f"{base}-2024Q1",
            f"{base}-2024Q2",
            f"{base}-2024Q3",
        ]

    def test_all_time_query_includes_all_shards(self, tmp_path):
        """All-time query (None start/end) includes all shards plus legacy."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_overlapping_shards,
        )

        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2023Q4", "2024Q1", "2024Q2"]:
            (tmp_path / f"{base}-{suffix}").mkdir()
        # Add legacy
        (tmp_path / base).mkdir()

        result = get_overlapping_shards("voyage-code-3", tmp_path, None, None)

        # All shards included, legacy last
        assert f"{base}-2023Q4" in result
        assert f"{base}-2024Q1" in result
        assert f"{base}-2024Q2" in result
        assert result[-1] == base

    def test_provider_slug_fix_in_embedding_provider_lookup(self, tmp_path):
        """_create_embedding_provider_for_collection correctly identifies provider for sharded name."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            _create_embedding_provider_for_collection,
        )

        # Build a minimal config mock
        config = Mock()
        config.voyage_ai = Mock()
        config.voyage_ai.model = "voyage-code-3"
        config.cohere = Mock()
        config.cohere.model = "embed-v4.0"
        config.embedding_provider = "voyage-ai"

        sharded_name = "code-indexer-temporal-voyage_code_3-2024Q3"

        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory:
            mock_factory.get_configured_providers.return_value = ["voyage-ai"]
            mock_factory.create.return_value = Mock()

            _create_embedding_provider_for_collection(config, sharded_name)

            # Should have called create with voyage-ai provider (not fallen back)
            mock_factory.create.assert_called_once()
            call_kwargs = mock_factory.create.call_args
            # Called with provider_name="voyage-ai"
            assert call_kwargs[1].get("provider_name") == "voyage-ai" or (
                len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "voyage-ai"
            )


# ---------------------------------------------------------------------------
# Sequential shard dispatch (AC3)
# ---------------------------------------------------------------------------


class TestSequentialShardDispatch:
    """Verify that same-provider shards are queried sequentially, not in parallel."""

    def test_same_provider_shards_queried_sequentially(self):
        """When multiple shards exist for one provider, query calls are sequential."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            _query_shards_raw,
        )
        from unittest.mock import patch, MagicMock
        import threading

        config = MagicMock()
        vector_store = MagicMock()
        vector_store.project_root = "/fake/root"

        shard_names = [
            "code-indexer-temporal-voyage_code_3-2024Q1",
            "code-indexer-temporal-voyage_code_3-2024Q2",
            "code-indexer-temporal-voyage_code_3-2024Q3",
        ]

        call_log: list = []
        active_calls = [0]
        lock = threading.Lock()
        max_concurrent = [0]

        def fake_single_provider(config, vs, coll_name, *args, **kwargs):
            with lock:
                active_calls[0] += 1
                if active_calls[0] > max_concurrent[0]:
                    max_concurrent[0] = active_calls[0]
            call_log.append(coll_name)
            # Simulate brief work
            from code_indexer.services.temporal.temporal_search_service import (
                TemporalSearchResults,
            )

            result = TemporalSearchResults(
                results=[], query="test", filter_type="none", filter_value=None
            )
            with lock:
                active_calls[0] -= 1
            return result

        with patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
            side_effect=fake_single_provider,
        ):
            _query_shards_raw(
                config,
                vector_store,
                shard_names,
                "test query",
                10,
                None,
                None,
            )

        # All three shards were queried
        assert call_log == shard_names, f"Expected sequential order, got {call_log}"
        # Never more than 1 active at once (sequential proof)
        assert max_concurrent[0] == 1, (
            f"Expected max 1 concurrent shard, got {max_concurrent[0]}"
        )


# ---------------------------------------------------------------------------
# Shard pruning in the live query path (AC7)
# ---------------------------------------------------------------------------


class TestShardPruningLiveQueryPath:
    """Verify execute_temporal_query_with_fusion prunes shards by time_range."""

    def test_bounded_time_range_queries_only_overlapping_shards(self, tmp_path):
        """A Q2-only time_range must NOT open Q1 or Q3 shards."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )
        from unittest.mock import patch, MagicMock

        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        base = "code-indexer-temporal-voyage_code_3"
        # Create Q1, Q2, Q3 shard directories on disk
        for suffix in ["2024Q1", "2024Q2", "2024Q3"]:
            (index_path / f"{base}-{suffix}").mkdir()

        config = MagicMock()
        config.voyage_ai = MagicMock()
        config.voyage_ai.model = "voyage-code-3"
        config.cohere = MagicMock()
        config.cohere.model = "embed-v4.0"
        config.embedding_provider = "voyage-ai"

        vector_store = MagicMock()
        vector_store.project_root = str(tmp_path)

        queried_shards: list = []

        def fake_query_single(cfg, vs, coll_name, *args, **kwargs):
            queried_shards.append(coll_name)
            from code_indexer.services.temporal.temporal_search_service import (
                TemporalSearchResults,
            )

            return TemporalSearchResults(
                results=[], query="test", filter_type="time_range", filter_value=None
            )

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
                side_effect=fake_query_single,
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ) as mock_factory,
        ):
            mock_factory.get_configured_providers.return_value = ["voyage-ai"]

            # Q2-only query: 2024-04-01 to 2024-06-30
            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=vector_store,
                query_text="test",
                limit=10,
                time_range=("2024-04-01", "2024-06-30"),
            )

        # Only Q2 shard should have been queried
        assert len(queried_shards) == 1, f"Expected 1 shard, got {queried_shards}"
        assert queried_shards[0] == f"{base}-2024Q2", (
            f"Expected Q2, got {queried_shards[0]}"
        )
        # Q1 and Q3 must NOT have been opened
        assert f"{base}-2024Q1" not in queried_shards
        assert f"{base}-2024Q3" not in queried_shards

    def test_all_time_query_includes_all_shards(self, tmp_path):
        """ALL_TIME_RANGE (None start/end) queries all available shards."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )
        from code_indexer.services.temporal.temporal_search_service import (
            ALL_TIME_RANGE,
        )
        from unittest.mock import patch, MagicMock

        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        base = "code-indexer-temporal-voyage_code_3"
        for suffix in ["2024Q1", "2024Q2", "2024Q3"]:
            (index_path / f"{base}-{suffix}").mkdir()

        config = MagicMock()
        config.voyage_ai = MagicMock()
        config.voyage_ai.model = "voyage-code-3"
        config.cohere = MagicMock()
        config.cohere.model = "embed-v4.0"
        config.embedding_provider = "voyage-ai"

        vector_store = MagicMock()
        vector_store.project_root = str(tmp_path)

        queried_shards: list = []

        def fake_query_single(cfg, vs, coll_name, *args, **kwargs):
            queried_shards.append(coll_name)
            from code_indexer.services.temporal.temporal_search_service import (
                TemporalSearchResults,
            )

            return TemporalSearchResults(
                results=[], query="test", filter_type="none", filter_value=None
            )

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
                side_effect=fake_query_single,
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ) as mock_factory,
        ):
            mock_factory.get_configured_providers.return_value = ["voyage-ai"]

            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=vector_store,
                query_text="test",
                limit=10,
                time_range=ALL_TIME_RANGE,
            )

        # All three shards queried
        assert len(queried_shards) == 3, f"Expected 3 shards, got {queried_shards}"
        assert f"{base}-2024Q1" in queried_shards
        assert f"{base}-2024Q2" in queried_shards
        assert f"{base}-2024Q3" in queried_shards


# ---------------------------------------------------------------------------
# Bug #1: New shard collections must be created before first write (Story #1171)
# ---------------------------------------------------------------------------

_FACTORY_PATCH = "code_indexer.services.embedding_factory.EmbeddingProviderFactory"


def _make_indexer_mocks(tmp_path: Path, collection_exists: bool = False):
    """Return (config_manager_mock, vector_store_mock) for TemporalIndexer tests.

    Only external collaborators are mocked:
    - vector_store: filesystem storage boundary
    - config_manager: configuration provider (no real config file required)
    Internal TemporalIndexer methods are NOT mocked.
    """
    mock_config = Mock()
    mock_config.voyage_ai = Mock()
    mock_config.voyage_ai.model = "voyage-code-3"
    mock_config.voyage_ai.parallel_requests = 4
    mock_config.voyage_ai.temporal_parallel_requests = None
    mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
    mock_config.cohere = Mock()
    mock_config.cohere.parallel_requests = 4
    mock_config.cohere.temporal_parallel_requests = None
    mock_config.embedding_provider = "voyage-ai"
    mock_config.temporal = Mock()
    mock_config.temporal.diff_context_lines = 3
    mock_config.file_extensions = []
    mock_config.override_config = None
    mock_config.codebase_dir = tmp_path

    mock_config_manager = Mock()
    mock_config_manager.get_config.return_value = mock_config
    cfg_dir = tmp_path / ".code-indexer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    mock_config_manager.config_path = cfg_dir / "config.json"

    index_dir = tmp_path / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    mock_vector_store = Mock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = index_dir
    mock_vector_store.collection_exists.return_value = collection_exists
    mock_vector_store.create_collection.return_value = True
    mock_vector_store.load_id_index.return_value = set()
    mock_vector_store.begin_indexing.return_value = None
    mock_vector_store.end_indexing.return_value = {"status": "ok"}
    mock_vector_store.upsert_points.return_value = None

    return mock_config_manager, mock_vector_store


def _run_index_commits(indexer, commits):
    """Drive indexer.index_commits() mocking only external subprocess/service boundaries.

    Mocked external boundaries:
    - _get_commit_history: calls subprocess.run(git log) — external process
    - _get_current_branch: calls subprocess.run(git branch) — external process
    - EmbeddingProviderFactory: external API service
    - VectorCalculationManager: external embedding computation service

    NOT mocked: any internal TemporalIndexer logic including _save_temporal_metadata,
    which writes to indexer.temporal_dir (under tmp_path via the mock vector_store).

    cancellation_event returns True so workers exit immediately without real API calls.
    """
    mock_vcm_instance = Mock()
    mock_vcm_instance.cancellation_event = Mock()
    mock_vcm_instance.cancellation_event.is_set.return_value = True

    with patch.object(indexer, "_get_commit_history", return_value=commits):
        with patch.object(indexer, "_get_current_branch", return_value="main"):
            with patch(_FACTORY_PATCH) as mock_factory:
                mock_factory.create.return_value = Mock()
                mock_factory.get_provider_model_info.return_value = {"dimensions": 1024}
                with patch(
                    "code_indexer.services.temporal.temporal_indexer.VectorCalculationManager"
                ) as mock_vcm:
                    mock_vcm.return_value.__enter__ = Mock(
                        return_value=mock_vcm_instance
                    )
                    mock_vcm.return_value.__exit__ = Mock(return_value=False)
                    indexer.index_commits()


class TestShardCollectionCreation:
    """Verify index_commits() creates each new shard collection before begin_indexing().

    Bug #1: upsert_points() raises ValueError('Collection does not exist') on the
    first write to a new quarterly shard because begin_indexing() does NOT create
    the collection. The fix must call create_collection() before begin_indexing()
    whenever collection_exists() returns False for a shard.
    """

    def test_new_shard_gets_create_collection_before_begin_indexing(self, tmp_path):
        """create_collection() must appear BEFORE begin_indexing() for a new shard."""
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        mock_config_manager, mock_vector_store = _make_indexer_mocks(
            tmp_path, collection_exists=False
        )
        indexer = TemporalIndexer(
            mock_config_manager,
            mock_vector_store,
            collection_name="code-indexer-temporal-voyage_code_3",
        )

        call_order: list = []

        def _track_create(n, *a, **k):
            call_order.append(("create_collection", n))
            return True

        def _track_begin(n, *a, **k):
            call_order.append(("begin_indexing", n))

        mock_vector_store.create_collection.side_effect = _track_create
        mock_vector_store.begin_indexing.side_effect = _track_begin

        shard = "code-indexer-temporal-voyage_code_3-2024Q2"
        _run_index_commits(indexer, [_make_commit("aaa111", 2024, 5, 15)])

        creates = [
            i for i, op in enumerate(call_order) if op == ("create_collection", shard)
        ]
        begins = [
            i for i, op in enumerate(call_order) if op == ("begin_indexing", shard)
        ]

        assert creates, f"create_collection not called for {shard}. order={call_order}"
        assert begins, f"begin_indexing not called for {shard}. order={call_order}"
        assert creates[0] < begins[0], (
            f"create_collection must precede begin_indexing for {shard}. "
            f"create@{creates[0]}, begin@{begins[0]}. order={call_order}"
        )

    def test_existing_shard_skips_create_collection(self, tmp_path):
        """When the shard already exists, create_collection must NOT be called for it."""
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

        mock_config_manager, mock_vector_store = _make_indexer_mocks(
            tmp_path, collection_exists=True
        )
        indexer = TemporalIndexer(
            mock_config_manager,
            mock_vector_store,
            collection_name="code-indexer-temporal-voyage_code_3",
        )
        mock_vector_store.create_collection.reset_mock()

        _run_index_commits(indexer, [_make_commit("bbb222", 2024, 5, 15)])

        mock_vector_store.create_collection.assert_not_called()


# ---------------------------------------------------------------------------
# Bug #2: HNSW cache must be evicted after each shard query (Story #1171)
# ---------------------------------------------------------------------------


def _run_query_shards(shard_names, vector_store):
    """Drive _query_shards_raw() with faked shard results.

    _query_single_provider is the external network/embedding boundary and is mocked.
    The hnsw_index_cache attribute on vector_store is the cache object under test.
    No internal _query_shards_raw logic is mocked.
    """
    from code_indexer.services.temporal.temporal_fusion_dispatch import (
        _query_shards_raw,
    )
    from unittest.mock import MagicMock

    config = MagicMock()

    def fake_single(cfg, vs, coll_name, *args, **kwargs):
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        return TemporalSearchResults(
            results=[], query="test", filter_type="none", filter_value=None
        )

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=fake_single,
    ):
        return _query_shards_raw(config, vector_store, shard_names, "q", 10, None, None)


class TestHNSWCacheEvictionAfterShard:
    """Verify _query_shards_raw() evicts the HNSW cache entry after each shard.

    Bug #2: hnsw_index_cache.get_or_load() keeps every shard HNSW resident in RAM
    as subsequent shards load. The fix must call
    vector_store.hnsw_index_cache.invalidate() after each shard so peak RAM is
    bounded to one HNSW index at a time (server mode only — CLI has no cache).
    """

    def test_cache_invalidated_once_per_shard(self, tmp_path):
        """N shard queries must produce exactly N hnsw_index_cache.invalidate() calls."""
        base_path = tmp_path / ".code-indexer" / "index"
        base_path.mkdir(parents=True)

        vector_store = Mock()
        vector_store.base_path = base_path
        vector_store.hnsw_index_cache = Mock()
        invalidated: list = []
        vector_store.hnsw_index_cache.invalidate.side_effect = (
            lambda k: invalidated.append(k)
        )

        shard_names = [
            "code-indexer-temporal-voyage_code_3-2024Q1",
            "code-indexer-temporal-voyage_code_3-2024Q2",
            "code-indexer-temporal-voyage_code_3-2024Q3",
        ]
        _run_query_shards(shard_names, vector_store)

        assert len(invalidated) == 3, (
            f"Expected 3 cache invalidations (one per shard), got {len(invalidated)}: "
            f"{invalidated}"
        )

    def test_no_error_when_cache_is_none(self, tmp_path):
        """CLI mode: hnsw_index_cache=None must not raise during shard queries."""
        from unittest.mock import MagicMock

        base_path = tmp_path / ".code-indexer" / "index"
        base_path.mkdir(parents=True)

        vector_store = MagicMock()
        vector_store.base_path = base_path
        vector_store.hnsw_index_cache = None  # CLI/standalone — no cache

        # Must not raise AttributeError or TypeError
        _run_query_shards(["code-indexer-temporal-voyage_code_3-2024Q1"], vector_store)
