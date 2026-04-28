"""Tests for active reranking: success ordering, field preservation, round-trips,
and hybrid mode (two independent sublist calls).

Story #693 -- Epic #689.
"""

from typing import Dict, Tuple

from .conftest import ServiceStack, make_semantic, make_fts


def _apply():
    from code_indexer.cli_search_funnel import _apply_cli_rerank_and_filter

    return _apply_cli_rerank_and_filter


# ---------------------------------------------------------------------------
# Rerank success
# ---------------------------------------------------------------------------


class TestRerankSuccess:
    def test_rerank_reverses_order(self, voyage_reversed_patched: ServiceStack) -> None:
        apply = _apply()
        results = [
            make_semantic(score=float(i), content=f"content {i}") for i in range(4)
        ]
        out = apply(
            results=results,
            rerank_query="authentication logic",
            rerank_instruction=None,
            config=voyage_reversed_patched.config,
            user_limit=3,
            health_monitor=voyage_reversed_patched.monitor,
        )
        assert len(out) == 3
        # Reversed: doc[3] -> doc[2] -> doc[1]
        assert out[0]["score"] == results[3]["score"]
        assert out[1]["score"] == results[2]["score"]
        assert out[2]["score"] == results[1]["score"]

    def test_truncates_to_user_limit_after_rerank(
        self, voyage_identity_patched: ServiceStack
    ) -> None:
        apply = _apply()
        results = [make_semantic(score=float(i)) for i in range(6)]
        out = apply(
            results=results,
            rerank_query="auth",
            rerank_instruction=None,
            config=voyage_identity_patched.config,
            user_limit=3,
            health_monitor=voyage_identity_patched.monitor,
        )
        assert len(out) == 3

    def test_extra_fields_preserved_through_rerank(
        self, voyage_identity_patched: ServiceStack
    ) -> None:
        apply = _apply()
        results = [
            make_semantic(
                path="src/special.py",
                staleness={"stale": True},
                custom_tag="important",
            )
        ]
        out = apply(
            results=results,
            rerank_query="query",
            rerank_instruction=None,
            config=voyage_identity_patched.config,
            user_limit=1,
            health_monitor=voyage_identity_patched.monitor,
        )
        assert len(out) == 1
        assert out[0]["payload"]["path"] == "src/special.py"
        assert out[0].get("staleness") == {"stale": True}
        assert out[0].get("custom_tag") == "important"


# ---------------------------------------------------------------------------
# Normalization round-trips through active rerank path
# ---------------------------------------------------------------------------


class TestNormalizationRoundTrips:
    def test_semantic_all_fields_survive_active_rerank(
        self, voyage_identity_patched: ServiceStack
    ) -> None:
        apply = _apply()
        r = make_semantic(
            path="src/auth.py",
            line_start=10,
            line_end=20,
            score=0.95,
            content="class Auth: pass",
            language="python",
            file_last_modified="2024-01-01",
        )
        out = apply(
            results=[r],
            rerank_query="auth",
            rerank_instruction=None,
            config=voyage_identity_patched.config,
            user_limit=1,
            health_monitor=voyage_identity_patched.monitor,
        )
        assert len(out) == 1
        o = out[0]
        assert o["payload"]["path"] == "src/auth.py"
        assert o["payload"]["line_start"] == 10
        assert o["payload"]["line_end"] == 20
        assert o["payload"]["content"] == "class Auth: pass"
        assert o["payload"]["language"] == "python"
        assert o["payload"]["file_last_modified"] == "2024-01-01"

    def test_fts_all_fields_survive_active_rerank(
        self, voyage_identity_patched: ServiceStack
    ) -> None:
        apply = _apply()
        r = make_fts(
            path="src/bar.py",
            line=42,
            column=7,
            match_text="bar_func",
            snippet="def bar_func(): ...",
            language="python",
            snippet_start_line=40,
        )
        out = apply(
            results=[r],
            rerank_query="bar",
            rerank_instruction=None,
            config=voyage_identity_patched.config,
            user_limit=1,
            health_monitor=voyage_identity_patched.monitor,
        )
        assert len(out) == 1
        o = out[0]
        assert o["path"] == "src/bar.py"
        assert o["line"] == 42
        assert o["column"] == 7
        assert o["match_text"] == "bar_func"
        assert o["snippet"] == "def bar_func(): ..."
        assert o["language"] == "python"
        assert o["snippet_start_line"] == 40


# ---------------------------------------------------------------------------
# Hybrid mode: funnel called independently per sublist
# ---------------------------------------------------------------------------


class TestHybridMode:
    def test_two_independent_calls_each_truncated_voyage_called_twice(
        self,
        voyage_counting_patched: Tuple[ServiceStack, Dict[str, int]],
    ) -> None:
        """Hybrid mode: funnel called once for semantic, once for FTS.
        Voyage must be invoked exactly once per call (total: 2)."""
        stack, call_count = voyage_counting_patched
        apply = _apply()

        semantic_results = [make_semantic(score=float(i)) for i in range(4)]
        fts_results = [make_fts(path=f"src/{i}.py") for i in range(4)]

        sem_out = apply(
            results=semantic_results,
            rerank_query="auth",
            rerank_instruction=None,
            config=stack.config,
            user_limit=2,
            health_monitor=stack.monitor,
        )
        fts_out = apply(
            results=fts_results,
            rerank_query="auth",
            rerank_instruction=None,
            config=stack.config,
            user_limit=2,
            health_monitor=stack.monitor,
        )

        assert len(sem_out) == 2
        assert len(fts_out) == 2
        assert call_count["n"] == 2
