"""Epic #1454 / Story #1461 salvage item 5.

_search_single_repository's composite branch (proxy_mode=true repos) returns
BEFORE the temporal branch is ever reached. The composite CLI path
(_execute_cli_query / _build_cli_args) has no wiring for
time_range/time_range_all/at_commit, so a temporal query against a
composite repo silently drops the time filter and returns non-temporal
results with HTTP 200 -- no error, no warning.

Fix: reject the whole temporal request explicitly (SemanticQueryError)
when the repo is composite AND the request carries temporal intent (any
of time_range, time_range_all, at_commit) -- never partially honor other
filters while dropping the time filter (anti-fallback / anti-silent-
failure). A non-temporal composite query must still succeed unchanged.

Real infra: a genuine on-disk composite (proxy_mode=true) repo config, no
mocking of _is_composite_repository or the guard under test.
"""

from __future__ import annotations

import json

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryError,
    SemanticQueryManager,
)


def _make_composite_repo(tmp_path):
    repo_dir = tmp_path / "composite-repo"
    (repo_dir / ".code-indexer").mkdir(parents=True)
    (repo_dir / ".code-indexer" / "config.json").write_text(
        json.dumps({"proxy_mode": True})
    )
    return repo_dir


def _manager(tmp_path):
    return SemanticQueryManager(data_dir=str(tmp_path / "server-data"))


class TestCompositeTemporalRejection:
    @pytest.mark.parametrize(
        "temporal_kwargs",
        [
            {"time_range": "2024-01-01..2024-12-31"},
            {"time_range_all": True},
            {"at_commit": "abc1234"},
        ],
        ids=["time_range", "time_range_all", "at_commit"],
    )
    def test_temporal_intent_on_composite_repo_raises_semantic_query_error(
        self, tmp_path, temporal_kwargs
    ):
        repo_dir = _make_composite_repo(tmp_path)
        manager = _manager(tmp_path)

        with pytest.raises(SemanticQueryError, match="composite"):
            manager._search_single_repository(
                str(repo_dir),
                "composite-repo",
                "auth logic",
                10,
                None,
                None,
                query_strategy="primary_only",
                **temporal_kwargs,
            )


class TestNonTemporalCompositeQueryUnaffected:
    def test_non_temporal_composite_query_still_reaches_cli_integration(
        self, tmp_path, monkeypatch
    ):
        """A non-temporal composite query must NOT be rejected -- it must
        still be routed through the real composite CLI integration
        unchanged. Only the external _execute_query CLI boundary (not any
        SemanticQueryManager method) is faked, since there are no real
        component repos to search here."""
        repo_dir = _make_composite_repo(tmp_path)
        manager = _manager(tmp_path)

        captured = {}

        def _fake_execute_query(args, repo_paths):
            captured["args"] = list(args)
            captured["repo_paths"] = repo_paths
            print("")  # no output -> _parse_cli_output returns []

        monkeypatch.setattr(
            "code_indexer.server.query.semantic_query_manager._execute_query",
            _fake_execute_query,
        )

        result = manager._search_single_repository(
            str(repo_dir),
            "composite-repo",
            "auth logic",
            10,
            None,
            None,
            query_strategy="primary_only",
        )

        assert result == []
        assert "auth logic" in captured.get("args", [])
