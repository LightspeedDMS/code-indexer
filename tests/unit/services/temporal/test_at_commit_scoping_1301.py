"""Tests for Bug #1301: at_commit point-in-time scoping on the per-commit
temporal index.

Bug #1301 root cause: `at_commit` was advertised end-to-end (REST model,
MCP tool docs, execute_temporal_query_with_fusion signature) but never
applied as a filter and never validated -- a bogus commit hash was silently
accepted and the full unfiltered result set came back.

Approved fix: `at_commit` resolves to a commit hash + UNIX timestamp via
git, then that timestamp becomes an upper bound on `commit_timestamp` --
the exact same mechanism `time_range`'s upper bound already uses. An
unresolvable ref/hash raises ValueError (surfaced as HTTP 400 by the
existing ValueError->400 handling already in inline_query.py /
semantic_query_manager.py).
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchService,
    resolve_commit_timestamp,
)


def _init_real_git_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


def _commit(repo_path: Path, filename: str, content: str, message: str) -> str:
    (repo_path / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class TestResolveCommitTimestamp:
    """resolve_commit_timestamp() -- the at_commit ref resolution helper."""

    def test_resolve_valid_commit_hash_returns_correct_timestamp(self, tmp_path):
        """A real commit hash resolves to its actual commit UNIX timestamp."""
        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        commit_hash = _commit(repo_path, "a.txt", "hello", "first commit")

        ts_proc = subprocess.run(
            ["git", "show", "-s", "--format=%ct", commit_hash],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        expected_ts = int(ts_proc.stdout.strip())

        resolved_ts = resolve_commit_timestamp(repo_path, commit_hash)

        assert resolved_ts == expected_ts

    def test_resolve_short_hash_resolves_same_as_full_hash(self, tmp_path):
        """A 7-char short hash resolves to the same timestamp as the full hash."""
        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        commit_hash = _commit(repo_path, "a.txt", "hello", "first commit")

        full_ts = resolve_commit_timestamp(repo_path, commit_hash)
        short_ts = resolve_commit_timestamp(repo_path, commit_hash[:7])

        assert full_ts == short_ts

    def test_bogus_commit_hash_raises_value_error(self, tmp_path):
        """A non-existent commit hash must raise ValueError -- NOT be silently
        accepted (Bug #1301: previously HTTP 200 with unfiltered results)."""
        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        _commit(repo_path, "a.txt", "hello", "first commit")

        with pytest.raises(ValueError, match="at_commit"):
            resolve_commit_timestamp(repo_path, "deadbeefdeadbeef")

    def test_ref_pointing_at_non_commit_raises_value_error(self, tmp_path):
        """An unresolvable ref name (never existed) raises ValueError too."""
        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        _commit(repo_path, "a.txt", "hello", "first commit")

        with pytest.raises(ValueError, match="at_commit"):
            resolve_commit_timestamp(repo_path, "refs/heads/does-not-exist")


class TestQueryTemporalAtCommitScoping:
    """TemporalSearchService.query_temporal(at_commit_ts=...) upper-bounds
    commit_timestamp using the SAME mechanism as time_range's upper bound."""

    def _make_service(self):
        config_manager = MagicMock()
        vector_store = MagicMock()
        embedding_provider = MagicMock()

        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        vector_store.__class__ = FilesystemVectorStore  # type: ignore[assignment]
        vector_store.search.return_value = ([], {})

        service = TemporalSearchService(
            config_manager=config_manager,
            project_root=Path("/tmp/test_project"),
            vector_store_client=vector_store,
            embedding_provider=embedding_provider,
            collection_name="code-indexer-temporal",
        )
        return service, vector_store

    def test_at_commit_ts_tightens_upper_bound_below_time_range_end(self):
        """When at_commit_ts is earlier than the time_range end, the
        vector-store filter's lte bound must be the at_commit_ts, not the
        (later) time_range end."""
        service, vector_store = self._make_service()

        # time_range end is 2100-12-31 (ALL_TIME_RANGE upper bound); at_commit_ts
        # is a real, much earlier timestamp.
        at_commit_ts = 1700000000  # 2023-11-14

        service.query_temporal(
            query="token cost analyzer",
            time_range=("1970-01-01", "2100-12-31"),
            limit=15,
            at_commit_ts=at_commit_ts,
        )

        call_kwargs = vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs["filter_conditions"]
        commit_ts_condition = next(
            c for c in filter_conditions["must"] if c.get("key") == "commit_timestamp"
        )
        assert commit_ts_condition["range"]["lte"] == at_commit_ts, (
            "at_commit_ts must tighten the commit_timestamp upper bound to "
            f"exactly {at_commit_ts}, got {commit_ts_condition['range']}"
        )

    def test_at_commit_ts_does_not_widen_a_tighter_time_range_end(self):
        """When time_range's own end is EARLIER than at_commit_ts, the
        tighter time_range bound must win (min() semantics, never widen)."""
        service, vector_store = self._make_service()

        # time_range end = 2020-01-01 23:59:59 -> earlier than at_commit_ts below.
        at_commit_ts = 1700000000  # 2023-11-14, LATER than the time_range end

        service.query_temporal(
            query="x",
            time_range=("1970-01-01", "2020-01-01"),
            limit=5,
            at_commit_ts=at_commit_ts,
        )

        call_kwargs = vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs["filter_conditions"]
        commit_ts_condition = next(
            c for c in filter_conditions["must"] if c.get("key") == "commit_timestamp"
        )
        # 2020-01-01 23:59:59 UTC/local as computed by the existing time_range
        # logic must remain the (tighter) bound -- must be < at_commit_ts.
        assert commit_ts_condition["range"]["lte"] < at_commit_ts

    def test_no_at_commit_ts_leaves_time_range_bound_untouched(self):
        """Byte-identical behavior when at_commit_ts is omitted (None)."""
        service, vector_store = self._make_service()

        service.query_temporal(
            query="x",
            time_range=("1970-01-01", "2100-12-31"),
            limit=5,
        )

        call_kwargs = vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs["filter_conditions"]
        commit_ts_condition = next(
            c for c in filter_conditions["must"] if c.get("key") == "commit_timestamp"
        )
        import datetime

        expected_end_ts = int(
            datetime.datetime.strptime("2100-12-31", "%Y-%m-%d")
            .replace(hour=23, minute=59, second=59)
            .timestamp()
        )
        assert commit_ts_condition["range"]["lte"] == expected_end_ts


class TestFilterByTimeRangeAtCommitPostFilter:
    """_filter_by_time_range() post-filter safety layer must also honor
    at_commit_ts (mirrors the vector-store-layer filter_conditions bound)."""

    def _make_service(self):
        config_manager = MagicMock()
        vector_store = MagicMock()
        embedding_provider = MagicMock()
        return TemporalSearchService(
            config_manager=config_manager,
            project_root=Path("/tmp/test_project"),
            vector_store_client=vector_store,
            embedding_provider=embedding_provider,
            collection_name="code-indexer-temporal",
        )

    def _raw_result(self, commit_hash: str, commit_timestamp: int) -> dict:
        return {
            "score": 0.9,
            "chunk_text": "some content",
            "payload": {
                "path": "foo.py",
                "chunk_index": 0,
                "commit_hash": commit_hash,
                "commit_date": "2024-01-01",
                "commit_message": "msg",
                "author_name": "alice",
                "commit_timestamp": commit_timestamp,
                "is_head": True,
            },
        }

    def test_result_after_at_commit_ts_is_excluded(self):
        """A commit whose timestamp is AFTER at_commit_ts must be dropped,
        even though it falls inside the (wider) time_range window."""
        service = self._make_service()

        earlier_ts = 1600000000  # before at_commit_ts
        later_ts = 1800000000  # after at_commit_ts
        at_commit_ts = 1700000000

        semantic_results = [
            self._raw_result("earliercommit", earlier_ts),
            self._raw_result("latercommit", later_ts),
        ]

        filtered, _ = service._filter_by_time_range(
            semantic_results=semantic_results,
            start_date="1970-01-01",
            end_date="2100-12-31",
            at_commit_ts=at_commit_ts,
        )

        commit_hashes = {r.metadata["commit_hash"] for r in filtered}
        assert commit_hashes == {"earliercommit"}, (
            f"at_commit_ts={at_commit_ts} must exclude the later commit; "
            f"got hashes={commit_hashes}"
        )


class TestExecuteTemporalQueryWithFusionAtCommitWiring:
    """execute_temporal_query_with_fusion() must resolve+validate at_commit
    and forward the resolved timestamp to service.query_temporal()."""

    def test_bogus_at_commit_raises_value_error_before_querying_shards(self, tmp_path):
        """A bogus at_commit must raise ValueError immediately -- NOT be
        swallowed by the per-shard try/except in _query_shards_raw (which
        would silently return empty results, HTTP 200, no error)."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )

        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        _commit(repo_path, "a.txt", "hello", "first commit")

        config = MagicMock()
        vector_store = MagicMock()
        vector_store.project_root = repo_path

        with pytest.raises(ValueError, match="at_commit"):
            execute_temporal_query_with_fusion(
                config=config,
                index_path=repo_path / ".code-indexer" / "index",
                vector_store=vector_store,
                query_text="x",
                limit=5,
                at_commit="deadbeefdeadbeef",
            )

    def test_valid_at_commit_forwarded_as_at_commit_ts_to_query_temporal(
        self, tmp_path
    ):
        """A resolvable at_commit reaches service.query_temporal() as the
        resolved at_commit_ts (not the raw ref string)."""
        from contextlib import ExitStack
        from unittest.mock import patch

        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        repo_path = tmp_path / "repo"
        _init_real_git_repo(repo_path)
        commit_hash = _commit(repo_path, "a.txt", "hello", "first commit")
        expected_ts = resolve_commit_timestamp(repo_path, commit_hash)

        config = MagicMock()
        vector_store = MagicMock()
        vector_store.project_root = repo_path

        mock_service = MagicMock()
        mock_service.query_temporal.return_value = TemporalSearchResults(
            results=[],
            query="x",
            filter_type="time_range",
            filter_value=None,
        )

        collections = [("code-indexer-temporal-voyage_code_3", repo_path)]
        provider_groups = [(name, [name]) for name, _ in collections]

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_migration"
                    ".migrate_legacy_temporal_collection"
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_fusion_dispatch"
                    "._discover_provider_shards_with_pruning",
                    return_value=provider_groups,
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_fusion_dispatch"
                    ".filter_healthy_temporal_providers",
                    side_effect=lambda cols: (cols, []),
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_fusion_dispatch"
                    "._create_embedding_provider_for_collection",
                    return_value=MagicMock(),
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_search_service"
                    ".TemporalSearchService",
                    return_value=mock_service,
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.temporal.temporal_fusion_dispatch"
                    "._make_config_manager",
                    return_value=MagicMock(),
                )
            )

            execute_temporal_query_with_fusion(
                config=config,
                index_path=repo_path / ".code-indexer" / "index",
                vector_store=vector_store,
                query_text="x",
                limit=5,
                at_commit=commit_hash,
            )

        mock_service.query_temporal.assert_called_once()
        _, kwargs = mock_service.query_temporal.call_args
        assert kwargs.get("at_commit_ts") == expected_ts, (
            f"at_commit={commit_hash!r} must resolve to at_commit_ts={expected_ts} "
            f"forwarded to service.query_temporal(); got kwargs={kwargs}"
        )
