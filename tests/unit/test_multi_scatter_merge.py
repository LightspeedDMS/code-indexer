"""Unit tests for C6 Phase 2 multi-repo scatter-gather merge."""

from code_indexer.server.routes.multi_query_routes import _merge_multi_responses


def test_merge_unions_results_sums_repos_maxes_time():
    parts = [
        {
            "results": {"r1": [{"x": 1}]},
            "errors": None,
            "metadata": {
                "total_results": 1,
                "total_repos_searched": 1,
                "execution_time_ms": 50,
            },
        },
        {
            "results": {"r2": [{"y": 1}, {"z": 2}]},
            "errors": {"r3": "not found"},
            "metadata": {
                "total_results": 2,
                "total_repos_searched": 1,
                "execution_time_ms": 80,
            },
        },
    ]
    resp = _merge_multi_responses(parts)
    assert set(resp.results.keys()) == {"r1", "r2"}
    assert resp.results["r2"] == [{"y": 1}, {"z": 2}]
    # total_results recomputed from the merged results (1 + 2)
    assert resp.metadata.total_results == 3
    assert resp.metadata.total_repos_searched == 2  # summed across parts
    assert resp.metadata.execution_time_ms == 80  # max across parts
    assert resp.errors == {"r3": "not found"}


def test_merge_empty_parts():
    resp = _merge_multi_responses([])
    assert resp.results == {}
    assert resp.metadata.total_results == 0
    assert resp.metadata.total_repos_searched == 0
    assert resp.errors is None


def test_merge_tolerates_missing_fields():
    # A part with only results (no metadata/errors) must not blow up.
    resp = _merge_multi_responses([{"results": {"r": [{"a": 1}]}}])
    assert resp.metadata.total_results == 1
    assert resp.errors is None
