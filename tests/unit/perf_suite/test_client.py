import pytest

from perf_suite import client

SECRET = "s3cret-Pa55-example"
# ^ why RFC 5737 TEST-NET-1 port 9, not a real host; a sent request fails => tally proves the send
UNROUTABLE_URL = "http://192.0.2.1:9"


def _client(http_timeout: float = 0.5) -> client.PerfClient:
    return client.PerfClient(
        UNROUTABLE_URL, "example-user", SECRET, http_timeout=http_timeout
    )


def test_rest_call_outside_allow_list_is_refused_before_sending():
    perf = _client()
    with pytest.raises(client.NotAllowedError, match="POST /api/admin/golden-repos"):
        perf.rest("POST", "/api/admin/golden-repos", json={"repo_url": "x"})
    with pytest.raises(client.NotAllowedError, match="DELETE /api/jobs/"):
        perf.rest("DELETE", "/api/jobs/{job_id}", path_params={"job_id": "abc"})
    assert perf.tally() == {}


def test_mcp_tool_outside_allow_list_is_refused_before_sending():
    perf = _client()
    with pytest.raises(client.NotAllowedError, match="add_golden_repo"):
        perf.mcp("add_golden_repo", {"repo_url": "x"})
    assert perf.tally() == {}


def test_allow_list_holds_only_read_only_doors():
    assert client.ALLOWED_REST == frozenset(
        {
            ("POST", "/auth/login"),
            ("GET", "/health"),
            ("POST", "/api/query"),
            ("POST", "/api/query/multi"),
            ("POST", "/api/regex/search"),
            ("POST", "/api/xray/search"),
            ("GET", "/api/jobs/{job_id}"),
        }
    )
    assert client.ALLOWED_MCP_TOOLS == frozenset(
        {
            "list_global_repos",
            "search_code",
            "regex_search",
            "xray_search",
            "get_job_details",
            "list_files",
            "get_file_content",
        }
    )


def test_redact_removes_password_everywhere():
    perf = _client()
    text = f'{{"input": {{"password": "{SECRET}"}}, "again": "{SECRET}"}}'
    redacted = perf.redact(text)
    assert SECRET not in redacted
    assert redacted.count(client.REDACTED) == 2


def test_failed_login_error_never_carries_password_and_is_sent_once():
    perf = _client()
    with pytest.raises(client.LoginRejectedError) as raised:
        perf.login()
    assert SECRET not in str(raised.value)
    assert perf.tally() == {"POST /auth/login": 1}


def test_password_is_not_exposed_by_repr_or_vars():
    perf = _client()
    assert SECRET not in repr(perf)
    assert SECRET not in str(vars(perf))


def test_tally_keys_templated_path_by_template():
    perf = _client()
    perf._token = "example-token"
    perf._token_obtained_at = float("inf")
    with pytest.raises(client.TransportError):
        perf.rest("GET", "/api/jobs/{job_id}", path_params={"job_id": "abc-123"})
    assert perf.tally() == {"GET /api/jobs/{job_id}": 1}


def test_redact_skips_values_shorter_than_three_chars():
    short = client.PerfClient(UNROUTABLE_URL, "ab", SECRET)
    longer = client.PerfClient(UNROUTABLE_URL, "abc", SECRET)
    try:
        assert short.redact("tab abc") == "tab abc"
        assert longer.redact("tab abc") == f"tab {client.REDACTED}"
    finally:
        short.close()
        longer.close()
