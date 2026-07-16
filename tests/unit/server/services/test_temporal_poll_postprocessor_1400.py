"""
Tests for Story #1400 Phase 5: shared per-read post-processor.

Canonical order (locked design): access-filter FIRST -> dedup -> terminal-
only rerank (over the full pool, top_k=requested_limit) -> requested_limit
truncation -> protocol wrap. Access-filter first = never rerank/return
unauthorized data.

Terminal-only RERANK: when `terminal=True`, `ctx.rerank_query` is present,
and a `config_service` is supplied, this post-processor actually invokes
the reranker (reranking._apply_reranking_sync) over the full deduped
candidate pool, with `deadline_monotonic` propagated through for CRITICAL
5's HTTP-timeout/backoff capping. `unranked` is then derived from the
ACTUAL reranker outcome via derive_unranked() -- never from mere
rerank_query presence. When rerank is not requested, not terminal, or no
config_service is supplied (partial reads never need one), this falls back
to truncate-only with unranked=True (conservative: never claims a ranking
guarantee not actually performed).

Partials (non-terminal reads) are ALWAYS unranked=True (rerank is
terminal-only, by design).
"""

import pytest

from code_indexer.server.services.temporal_poll_postprocessor import (
    postprocess_temporal_snapshot,
)


class _FakeAccessFilteringService:
    """Real-shape stand-in (anti-mock friendly): implements the exact
    filter_query_results(results, user_id) contract the real
    AccessFilteringService exposes, with a simple accessible-repos set."""

    def __init__(self, accessible_repos, admins=None):
        self._accessible = accessible_repos
        self._admins = admins or set()

    def is_admin_user(self, user_id):
        return user_id in self._admins

    def filter_query_results(self, results, user_id):
        if self.is_admin_user(user_id):
            return results
        return [r for r in results if r.get("repository_alias", "") in self._accessible]


def _make_snapshot(results, shards_completed=1, shards_total=1, requested_limit=10):
    return {
        "results": results,
        "shards_completed": shards_completed,
        "shards_total": shards_total,
        "ctx": {"requested_limit": requested_limit},
    }


class TestAccessFilterAppliedFirst:
    def test_unauthorized_results_excluded(self):
        snapshot = _make_snapshot(
            [
                {"file_path": "a.py", "repository_alias": "repo-a"},
                {"file_path": "b.py", "repository_alias": "repo-b"},
            ]
        )
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        results, k, n, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=False
        )

        paths = {r["file_path"] for r in results}
        assert paths == {"a.py"}

    def test_admin_sees_all_results(self):
        snapshot = _make_snapshot(
            [
                {"file_path": "a.py", "repository_alias": "repo-a"},
                {"file_path": "b.py", "repository_alias": "repo-b"},
            ]
        )
        access_svc = _FakeAccessFilteringService(
            accessible_repos=set(), admins={"admin"}
        )

        results, k, n, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="admin", is_admin=True, terminal=False
        )

        assert len(results) == 2


class TestDedup:
    def test_duplicate_file_path_commit_hash_deduped(self):
        snapshot = _make_snapshot(
            [
                {
                    "file_path": "a.py",
                    "repository_alias": "repo-a",
                    "metadata": {"commit_hash": "c1"},
                },
                {
                    "file_path": "a.py",
                    "repository_alias": "repo-a",
                    "metadata": {"commit_hash": "c1"},
                },
                {
                    "file_path": "a.py",
                    "repository_alias": "repo-a",
                    "metadata": {"commit_hash": "c2"},
                },
            ]
        )
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        results, k, n, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=False
        )

        assert len(results) == 2


class TestTruncationAndCounters:
    def test_truncated_to_requested_limit(self):
        snapshot = _make_snapshot(
            [
                {
                    "file_path": f"f{i}.py",
                    "repository_alias": "repo-a",
                    "metadata": {"commit_hash": f"c{i}"},
                }
                for i in range(5)
            ],
            requested_limit=2,
        )
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        results, k, n, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=False
        )

        assert len(results) == 2

    def test_shard_counters_passed_through(self):
        snapshot = _make_snapshot([], shards_completed=3, shards_total=7)
        access_svc = _FakeAccessFilteringService(accessible_repos=set())

        _, k, n, _ = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=False
        )

        assert k == 3
        assert n == 7


class TestUnrankedFlag:
    def test_non_terminal_read_always_unranked(self):
        snapshot = _make_snapshot([])
        access_svc = _FakeAccessFilteringService(accessible_repos=set())

        _, _, _, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=False
        )

        assert unranked is True

    def test_terminal_read_without_rerank_query_stays_unranked(self):
        """No rerank_query in ctx means no rerank was requested at all --
        even a terminal read stays conservatively unranked=True (never
        claims a ranking guarantee that was never requested)."""
        snapshot = _make_snapshot([])
        access_svc = _FakeAccessFilteringService(accessible_repos=set())

        _, _, _, unranked = postprocess_temporal_snapshot(
            snapshot, access_svc, username="alice", is_admin=False, terminal=True
        )

        assert unranked is True


def _make_rerank_result(index, score):
    from unittest.mock import MagicMock

    obj = MagicMock()
    obj.index = index
    obj.relevance_score = score
    return obj


def _make_config_service():
    from unittest.mock import MagicMock

    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    config.rerank_config = RerankConfig(
        voyage_reranker_model="rerank-2.5",
        cohere_reranker_model="rerank-v3.5",
        overfetch_multiplier=5,
    )
    config.claude_integration_config.voyageai_api_key = "voyage-key"
    config.claude_integration_config.cohere_api_key = "cohere-key"
    config.search_timeouts_config = None

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


class TestTerminalRerankWiring:
    def test_terminal_read_with_rerank_query_invokes_reranker_and_reports_success(
        self,
    ):
        from unittest.mock import patch

        snapshot = _make_snapshot(
            [
                {
                    "file_path": "a.py",
                    "repository_alias": "repo-a",
                    "content": "doc a",
                },
                {
                    "file_path": "b.py",
                    "repository_alias": "repo-a",
                    "content": "doc b",
                },
            ]
        )
        snapshot["ctx"]["rerank_query"] = "auth logic"
        snapshot["ctx"]["rerank_instruction"] = None
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.return_value = [
                _make_rerank_result(1, 0.95),
                _make_rerank_result(0, 0.6),
            ]
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            results, _k, _n, unranked = postprocess_temporal_snapshot(
                snapshot,
                access_svc,
                username="alice",
                is_admin=False,
                terminal=True,
                config_service=_make_config_service(),
            )

        assert unranked is False
        assert [r["file_path"] for r in results] == ["b.py", "a.py"]

    def test_terminal_read_without_config_service_stays_unranked(self):
        snapshot = _make_snapshot([{"file_path": "a.py", "repository_alias": "repo-a"}])
        snapshot["ctx"]["rerank_query"] = "auth logic"
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        _results, _k, _n, unranked = postprocess_temporal_snapshot(
            snapshot,
            access_svc,
            username="alice",
            is_admin=False,
            terminal=True,
            config_service=None,
        )

        assert unranked is True

    def test_non_terminal_read_never_invokes_reranker_even_with_rerank_query(self):
        from unittest.mock import patch

        snapshot = _make_snapshot([{"file_path": "a.py", "repository_alias": "repo-a"}])
        snapshot["ctx"]["rerank_query"] = "auth logic"
        access_svc = _FakeAccessFilteringService(accessible_repos={"repo-a"})

        with patch(
            "code_indexer.server.mcp.reranking.VoyageRerankerClient"
        ) as MockVoyage:
            _results, _k, _n, unranked = postprocess_temporal_snapshot(
                snapshot,
                access_svc,
                username="alice",
                is_admin=False,
                terminal=False,
                config_service=_make_config_service(),
            )

        assert unranked is True
        MockVoyage.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
