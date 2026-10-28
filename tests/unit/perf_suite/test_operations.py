import pytest

from perf_suite import client, operations


def test_rest_query_paths_semantic_and_fts():
    semantic = {"results": [{"file_path": "src/app/core.py", "line_number": 3}]}
    fts = {"search_mode": "fts", "fts_results": [{"path": "src/app/util.py"}]}
    assert operations.rest_query_paths(semantic, "semantic") == ["src/app/core.py"]
    assert operations.rest_query_paths(fts, "fts") == ["src/app/util.py"]


def test_rest_query_paths_rejects_unexpected_shape():
    with pytest.raises(client.ShapeError, match="fts_results"):
        operations.rest_query_paths({"results": []}, "fts")
    with pytest.raises(client.ShapeError, match="file_path"):
        operations.rest_query_paths({"results": [{"path": "x"}]}, "semantic")


def test_mcp_search_and_regex_paths():
    search = {"success": True, "results": {"results": [{"file_path": "a/b.py"}]}}
    regex = {"success": True, "matches": [{"file_path": "a/c.py", "line_number": 1}]}
    assert operations.mcp_search_paths(search) == ["a/b.py"]
    assert operations.mcp_regex_paths(regex) == ["a/c.py"]
    with pytest.raises(client.ShapeError):
        operations.mcp_search_paths({"success": True, "results": []})
    with pytest.raises(client.ShapeError):
        operations.mcp_regex_paths({"success": True})


def test_xray_job_paths_by_status():
    done = {"status": "completed", "result": {"matches": [{"file_path": "x/y.java"}]}}
    assert operations.xray_job_paths(done) == ["x/y.java"]
    assert operations.xray_job_paths({"status": "running", "result": None}) is None
    with pytest.raises(client.RequestFailedError, match="boom"):
        operations.xray_job_paths({"status": "failed", "error": "boom"})
    with pytest.raises(client.ShapeError):
        operations.xray_job_paths({"status": "completed", "result": {}})


def test_list_files_and_file_content():
    listing = {"success": True, "files": [{"path": "src/a.py"}, {"path": "src/b.py"}]}
    assert operations.list_files_paths(listing) == ["src/a.py", "src/b.py"]
    content = {
        "success": True,
        "file_content": [
            {"type": "text", "text": "line one\n"},
            {"type": "text", "text": "class X"},
        ],
    }
    assert operations.file_content_text(content) == "line one\nclass X"
    with pytest.raises(client.ShapeError):
        operations.file_content_text({"success": True, "file_content": "raw"})


def test_fanout_paths_rest_and_mcp():
    rest = {
        "results": {"one-global": [{"file_path": "a.py"}], "two-global": []},
        "errors": None,
    }
    mcp = {
        "success": True,
        "results": {
            "errors": {},
            "results_by_repo": {
                "one-global": {"count": 1, "results": [{"file_path": "a.py"}]}
            },
        },
    }
    assert operations.rest_multi_paths(rest) == {
        "one-global": ["a.py"],
        "two-global": [],
    }
    assert operations.mcp_multi_paths(mcp) == {"one-global": ["a.py"]}
    with pytest.raises(client.RequestFailedError, match="two-global"):
        operations.rest_multi_paths(
            {"results": {}, "errors": {"two-global": "Search failed"}}
        )
    with pytest.raises(client.RequestFailedError, match="two-global"):
        operations.mcp_multi_paths(
            {"results": {"errors": {"two-global": "x"}, "results_by_repo": {}}}
        )


def test_miss_detail_names_query_expected_and_top_paths():
    hits = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
    assert operations.miss_detail("click fts 'X'", "c.py", hits) is None
    detail = operations.miss_detail("click fts 'X'", "src/core.py", hits)
    assert detail is not None
    assert "click fts 'X'" in detail and "src/core.py" in detail
    assert "e.py" in detail and "f.py" not in detail


def test_fanout_missing_groups_is_a_wrong_result():
    requested = ["one-global", "two-global", "three-global"]
    grouped = {"one-global": ["pages/license.md"]}
    detail = operations.fanout_miss(
        "fanout fts 'license'", requested, grouped, {"one-global": "pages/license.md"}
    )
    assert detail is not None
    assert "two-global" in detail and "three-global" in detail
    complete = {alias: ["pages/license.md"] for alias in requested}
    assert (
        operations.fanout_miss("f", requested, complete, {"one-global": "license.md"})
        is None
    )
    wrong = operations.fanout_miss("f", requested, complete, {"two-global": "pom.xml"})
    assert wrong is not None and "pom.xml" in wrong


def test_xray_requests_follow_server_timeout_policy():
    rest = operations.xray_request("rest", "repo-global", "class X", ("*.py",), "fn")
    mcp = operations.xray_request("mcp", "repo-global", "class X", ("*.py",), "fn")
    assert "timeout_seconds" not in rest and "timeout_seconds" not in mcp
    assert rest["driver_regex"] == "class X" and mcp["pattern"] == "class X"
    assert mcp["await_seconds"] == 0


def test_failed_xray_poll_aborts_loudly():
    perf = client.PerfClient(
        "http://192.0.2.1:9", "example-user", "example-pass", http_timeout=0.5
    )
    perf._token = "example-token"
    perf._token_obtained_at = float("inf")
    with pytest.raises(client.PollFailedError, match="job-1"):
        operations.poll_xray(perf, "rest", "job-1")
    assert perf.tally() == {"GET /api/jobs/{job_id}": 1}


def test_regex_multi_groups_matches_by_source_repo():
    payload = {
        "errors": {},
        "matches": [
            {"file_path": "pyproject.toml", "source_repo": "one-global"},
            {"file_path": "LICENSE.txt", "source_repo": "two-global"},
            {"file_path": "pages/license.md", "source_repo": "one-global"},
        ],
    }
    assert operations.regex_multi_paths(payload) == {
        "one-global": ["pyproject.toml", "pages/license.md"],
        "two-global": ["LICENSE.txt"],
    }
    with pytest.raises(client.RequestFailedError, match="two-global"):
        operations.regex_multi_paths({"errors": {"two-global": "boom"}, "matches": []})
    with pytest.raises(client.ShapeError, match="source_repo"):
        operations.regex_multi_paths({"errors": {}, "matches": [{"file_path": "x"}]})


def test_rest_has_no_file_doors():
    assert operations.door_available("files_list", "rest") is False
    assert operations.door_available("file_read", "rest") is False
    assert operations.door_available("files_list", "mcp") is True
    for op in ("semantic", "fts", "regex", "xray", "fanout"):
        assert operations.door_available(op, "rest") is True
        assert operations.door_available(op, "mcp") is True
