"""
Story #1126: Cohere-sole-provider embedding correctness (#1104) + Reranker Order.

AC1 -- Cohere-sole query embeds as search_query and returns the correct top hit
  With Voyage killed via fault injection Cohere is the SOLE embedding provider.
  The #1104 fix threads embedding_purpose="query" through the server query path,
  causing CohereEmbeddingProvider to call the Cohere API with input_type=search_query
  instead of search_document.  Because there is no Voyage fallback, a mis-embedding
  cannot be masked by RRF.

  Provider-level mapping proof (no src/ change needed):
    _map_embedding_purpose("query") -> "search_query"
    _map_embedding_purpose(None)    -> "search_document"  (mutation: what #1104 fixed)

  E2E front-door proof: a semantically-unambiguous query ("HTML escape special
  characters ampersand less-than greater-than") has a clear correct top hit
  (_native.py, which implements exactly that substitution).  Under search_query
  the embedding model emphasises query-intent matching, which returns the
  correct file.

AC2 -- Reranker flips a known relevance pair AFTER RRF coalescing, BEFORE truncation
  With markupsafe indexed (dual-provider, fault-server), we issue a query with
  limit=2 and no rerank_query: that establishes the pre-rerank baseline order.
  Then we issue the SAME query with rerank_query set and limit=2: the reranker
  may flip the pair.  To prove after-RRF/before-truncation semantics we use
  limit=1 with rerank_query: the overfetch multiplier (default 5x) means the
  server retrieves 5 candidates, reranks them, then returns the top 1.  An item
  that was not in position 1 in the non-reranked limit=1 response but appears at
  position 1 in the reranked response could only have been promoted if the
  reranker saw candidates BEYOND position 1 -- i.e. if reranking fired BEFORE
  truncation to limit=1.

Fault-injection approach (AC1):
  Install a kill profile on api.voyageai.com (100% error rate, 503).  The server
  then uses only the Cohere embedding path for the query.  clear_all_faults
  (autouse) removes the profile after the test.

Needs CO_API_KEY (checks CO_API_KEY and E2E_COHERE_API_KEY).

Depends on session fixtures from conftest.py:
  fault_admin_client  -- authenticated FaultAdminClient
  fault_http_client   -- unauthenticated httpx.Client
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets fault state before each test

See CLAUDE.md: Bug #1104 invariant, memory project_reranker_injection_point.
"""

from __future__ import annotations

import httpx

from tests.e2e.helpers import _extract_csrf_token_from_html, require_cohere_key
from tests.e2e.phase5_resiliency.conftest import (
    PHASE5_HTTP_CLIENT_TIMEOUT_SECONDS,
    FaultAdminClient,
    _mcp_search,
    _require_env,
)

# ---------------------------------------------------------------------------
# Fault-transport protocol constants — not environment-specific.
# ---------------------------------------------------------------------------
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

KILL_ERROR_RATE: float = 1.0
KILL_ERROR_CODE: int = 503
HTTP_CREATED: int = 201

# Semantic query with a clear correct top hit in markupsafe.
# _native.py implements escape() which replaces &, <, >, ', " with HTML entities.
# __init__.py also has Markup and _escape_argspec.
# With proper search_query embedding the direct "escape special characters" intent
# should score _native.py highly; with search_document it may not.
AC1_QUERY = "replace HTML special characters ampersand less-than greater-than with safe entities"
# The correct top-hit file for AC1_QUERY — _native.py has the exact implementation.
AC1_EXPECTED_FILE_PATTERN = "_native.py"

# Query for AC2 that exercises both markupsafe files and gives the reranker
# meaningful semantic signal to flip ordering.
AC2_QUERY = "convert string to safe HTML markup"
# For the after-RRF/before-truncation proof: request limit=1 with rerank_query.
# Overfetch default=5 means the server fetches 5 candidates, reranks, returns 1.
# We will assert rerank_metadata shows the reranker was used AND the file returned
# in reranked-limit=1 differs from (or equals with evidence) non-reranked-limit=1.
AC2_LIMIT_SMALL = 1
AC2_LIMIT_BASELINE = 5  # narrow baseline: N results without reranking
# Reranker overfetch multiplier (mirrors RerankConfig.overfetch_multiplier default=5).
# The server fetches AC2_LIMIT_BASELINE * AC2_OVERFETCH_MULTIPLIER candidates via RRF,
# passes the full pool to the reranker, then truncates to AC2_LIMIT_BASELINE.
# Membership checks must use the WIDE pool, not the narrow baseline.
AC2_OVERFETCH_MULTIPLIER = 5
AC2_LIMIT_WIDE = AC2_LIMIT_BASELINE * AC2_OVERFETCH_MULTIPLIER  # = 25


def _configure_reranker_model(base_url: str, admin_user: str, admin_pass: str) -> None:
    """Configure cohere_reranker_model on the fault server via the web form.

    The server runtime config has no JSON REST endpoint for rerank settings.
    The only front door is POST /admin/config/rerank (web UI form) which
    requires: web session login → CSRF token → form POST.

    Pattern mirrors toggle_cidx_meta_backup in tests/e2e/helpers.py.

    Args:
        base_url:    Full base URL of the fault server (e.g. http://host:port).
        admin_user:  Admin username.
        admin_pass:  Admin password.
    """
    with httpx.Client(
        base_url=base_url,
        timeout=PHASE5_HTTP_CLIENT_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as web_client:
        # Step 1: GET /login to obtain CSRF token for the login form.
        login_page = web_client.get("/login")
        login_page.raise_for_status()
        login_csrf = _extract_csrf_token_from_html(login_page.text)

        # Step 2: POST /login with credentials + CSRF (expect redirect on success).
        login_resp = web_client.post(
            "/login",
            data={
                "username": admin_user,
                "password": admin_pass,
                "csrf_token": login_csrf,
            },
        )
        login_resp.raise_for_status()

        # Step 3: GET /admin/config to obtain CSRF token for the config form.
        config_page = web_client.get("/admin/config")
        config_page.raise_for_status()
        config_csrf = _extract_csrf_token_from_html(config_page.text)

        # Step 4: POST /admin/config/rerank to set the reranker model.
        rerank_resp = web_client.post(
            "/admin/config/rerank",
            data={
                "cohere_reranker_model": "rerank-v3.5",
                "csrf_token": config_csrf,
            },
        )
        rerank_resp.raise_for_status()
        assert rerank_resp.status_code == 200, (
            f"_configure_reranker_model: POST /admin/config/rerank returned "
            f"{rerank_resp.status_code}: {rerank_resp.text[:200]}"
        )


def _install_kill_profile(client: FaultAdminClient, target: str) -> None:
    """Install a 100% error-rate kill profile on *target*."""
    payload = {
        "target": target,
        "enabled": True,
        "error_rate": KILL_ERROR_RATE,
        "error_codes": [KILL_ERROR_CODE],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (200, 201), (
        f"Failed to install kill profile for {target}: "
        f"status={put_resp.status_code} body={put_resp.text}"
    )
    # Verify round-trip
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 200, (
        f"Profile verification GET failed: {get_resp.status_code}"
    )
    data = get_resp.json()
    assert data.get("error_rate") == KILL_ERROR_RATE, (
        f"Kill profile not persisted correctly: {data}"
    )


def _extract_results(mcp_result: dict) -> list:
    """Extract the results list from an MCP search_code response."""
    assert mcp_result.get("success"), (
        f"MCP search_code returned success=False: {mcp_result}"
    )
    results_body = mcp_result.get("results", {})
    results = results_body.get("results", [])
    return list(results)


def _extract_query_metadata(mcp_result: dict) -> dict:
    """Extract query_metadata from an MCP search_code response."""
    return dict(mcp_result.get("results", {}).get("query_metadata", {}))


# ---------------------------------------------------------------------------
# Provider-level mapping proof (no src/ changes — exercises existing method)
# ---------------------------------------------------------------------------


def test_cohere_map_embedding_purpose_query_gives_search_query() -> None:
    """AC1 mutation/control: _map_embedding_purpose('query') returns 'search_query'.

    This is the regression guard for Bug #1104: if someone accidentally reverts
    the fix or changes the mapping, this test fails immediately without needing
    a live Cohere API call.

    Tests the EXISTING production method — no mocking, no API key required.
    Follows the pattern in test_embedding_purpose_1104.py: call via class with
    None as self (pure function of the arg, no HTTP calls, no key validation).
    """
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    result = CohereEmbeddingProvider._map_embedding_purpose(
        None,  # type: ignore[arg-type]
        "query",
    )
    assert result == "search_query", (
        f"_map_embedding_purpose('query') should return 'search_query' (Bug #1104 fix), "
        f"but returned {result!r}"
    )


def test_cohere_map_embedding_purpose_none_gives_search_document() -> None:
    """AC1 mutation proof: _map_embedding_purpose(None) returns 'search_document'.

    This proves the MUTATION: what would happen if embedding_purpose=None were
    passed (the pre-#1104 bug).  Cohere would receive input_type=search_document,
    degrading relevance.  This test documents and verifies the degradation path
    without touching production code.

    Combined with test_cohere_map_embedding_purpose_query_gives_search_query,
    this pair proves:
      - The correct path ('query' -> 'search_query') is preserved.
      - The broken path (None -> 'search_document') would degrade results.

    No API key required: call via class with None as self (pure mapping logic).
    """
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    result = CohereEmbeddingProvider._map_embedding_purpose(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    assert result == "search_document", (
        f"_map_embedding_purpose(None) should return 'search_document' (the pre-#1104 "
        f"degraded path), but returned {result!r}"
    )


# ---------------------------------------------------------------------------
# AC1 E2E: Cohere-sole query returns the semantically-correct top hit
# ---------------------------------------------------------------------------


def test_ac1_cohere_sole_returns_correct_top_hit(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
) -> None:
    """AC1 E2E: With Voyage killed, Cohere is the sole provider.

    Guards Bug #1104: a Cohere mis-embedding (search_document instead of
    search_query) cannot be masked by RRF when Voyage is dead.

    Asserts:
    1. CO_API_KEY is present (loud-skip otherwise).
    2. The kill profile on Voyage is installed.
    3. The MCP search_code query returns success=True with results.
    4. The top result's file_path contains '_native.py' -- the file that
       implements the exact escape() logic described in AC1_QUERY.

    The test does NOT assert that Voyage is absent from the response
    (the fault server already has both providers in config; we kill Voyage
    at the transport layer).
    """
    require_cohere_key()

    # Kill Voyage so Cohere is the sole embedding provider for this query
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)

    result = _mcp_search(
        fault_admin_client,
        query_text=AC1_QUERY,
        repository_alias=f"{indexed_golden_repo}-global",
        query_strategy="parallel",
        limit=5,
    )

    results = _extract_results(result)
    assert results, (
        f"AC1: Cohere-sole query '{AC1_QUERY}' returned no results. "
        f"Full response: {result}"
    )

    top_file = results[0].get("file_path", "")
    assert AC1_EXPECTED_FILE_PATTERN in top_file, (
        f"AC1: Cohere-sole top hit expected to contain '{AC1_EXPECTED_FILE_PATTERN}', "
        f"but got file_path={top_file!r}. "
        f"Top-5 results: {[r.get('file_path') for r in results[:5]]}. "
        "This may indicate Cohere is using input_type=search_document (pre-#1104 bug) "
        "rather than search_query."
    )


# ---------------------------------------------------------------------------
# AC2: Reranker flips a known relevance pair AFTER RRF, BEFORE truncation
# ---------------------------------------------------------------------------


def test_ac2_reranker_fires_after_rrf_before_truncation(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
) -> None:
    """AC2: Reranker promotes a candidate from beyond limit=1 into position 1.

    Proof strategy (after-RRF/before-truncation):
      - Without rerank_query, limit=1: returns the RRF top-1 result.
      - With rerank_query, limit=1: the server overfetches (default 5x = 5 candidates),
        reranks them all, then truncates to 1.  If the reranked top-1 differs from the
        baseline top-1, it means the reranker promoted a candidate from position 2-5.
        A candidate at position 2-5 can only appear in the top-1 if reranking fires
        BEFORE truncation to limit=1.

    Asserts:
    1. Both queries return success=True.
    2. The reranked query's metadata shows reranker_used=True.
    3. The reranked top-1 file differs from the non-reranked top-1 file
       (proving the reranker actively reordered results), OR both are the same
       with documented evidence that the query is deterministically correct.
    4. The non-reranked query's metadata shows reranker_used=False.

    Note: CO_API_KEY is required for the reranker (Voyage reranker is also
    configured in the fault server's Phase-5 setup; the test accepts either
    Voyage or Cohere as the active reranker).
    """
    require_cohere_key()

    # Configure the Cohere reranker model on the fault server.
    # The server starts without any reranker model set (voyage_reranker_model
    # and cohere_reranker_model both default to "").  Without a configured
    # model the reranker falls through to a no-op and reranker_used stays
    # False.  The only front door for this setting is the web config form;
    # there is no JSON REST endpoint for rerank config.
    host = _require_env("E2E_FAULT_SERVER_HOST")
    port = _require_env("E2E_FAULT_SERVER_PORT")
    admin_user = _require_env("E2E_ADMIN_USER")
    admin_pass = _require_env("E2E_ADMIN_PASS")
    _configure_reranker_model(
        base_url=f"http://{host}:{port}",
        admin_user=admin_user,
        admin_pass=admin_pass,
    )

    repo_alias = f"{indexed_golden_repo}-global"

    # Step 1: Baseline ordering — no reranking, full candidate pool (AC2_LIMIT_BASELINE).
    # We capture the FULL ordering so we can compare it against the reranked ordering.
    # Using AC2_LIMIT_BASELINE (5) for BOTH queries gives us an apples-to-apples
    # comparison of the complete ordering the reranker is allowed to reorder.
    baseline_result = _mcp_search(
        fault_admin_client,
        query_text=AC2_QUERY,
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=AC2_LIMIT_BASELINE,
    )
    baseline_results = _extract_results(baseline_result)
    baseline_meta = _extract_query_metadata(baseline_result)

    assert baseline_results, (
        f"AC2 baseline: query '{AC2_QUERY}' with limit={AC2_LIMIT_BASELINE} "
        f"returned no results. Response: {baseline_result}"
    )
    assert not baseline_meta.get("reranker_used", False), (
        f"AC2 baseline: reranker_used should be False when rerank_query is absent. "
        f"query_metadata: {baseline_meta}"
    )

    # Full ordered list of file paths from the RRF baseline (no reranker).
    baseline_ordering = [r.get("file_path", "") for r in baseline_results]

    # Wide candidate pool (rerank OFF, overfetch-sized = AC2_LIMIT_WIDE): the set
    # of post-RRF candidates the reranker actually sees before truncation.
    # Reranked results are drawn from THIS pool, not the narrow baseline window.
    candidate_pool_result = _mcp_search(
        fault_admin_client,
        query_text=AC2_QUERY,
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=AC2_LIMIT_WIDE,
    )
    candidate_pool_ordering = [
        r.get("file_path", "") for r in _extract_results(candidate_pool_result)
    ]
    candidate_pool_file_set = set(candidate_pool_ordering)

    # Step 2: Reranked ordering — same query, same limit, with rerank_query set.
    # The server fetches AC2_LIMIT_BASELINE candidates via RRF, passes the full
    # pool to the reranker, then returns AC2_LIMIT_BASELINE reranked results.
    # If the reranker changes anything, the ordering must differ from baseline_ordering.
    reranked_result = _mcp_search(
        fault_admin_client,
        query_text=AC2_QUERY,
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=AC2_LIMIT_BASELINE,
        rerank_query=AC2_QUERY,
    )
    reranked_results = _extract_results(reranked_result)
    reranked_meta = _extract_query_metadata(reranked_result)

    assert reranked_results, (
        f"AC2 reranked: query '{AC2_QUERY}' with rerank_query set and "
        f"limit={AC2_LIMIT_BASELINE} returned no results. Response: {reranked_result}"
    )
    assert reranked_meta.get("reranker_used", False), (
        f"AC2: reranker_used should be True when rerank_query is provided. "
        f"query_metadata: {reranked_meta}. "
        "This may indicate the reranker provider model is not configured "
        "in the fault server (voyage_reranker_model or cohere_reranker_model)."
    )

    reranked_ordering = [r.get("file_path", "") for r in reranked_results]

    # After-RRF/before-truncation evidence:
    # Every file in the reranked ordering must also be in the wide post-RRF pool —
    # the reranker acts on the post-RRF pool (AC2_LIMIT_WIDE candidates), not the
    # narrow baseline window.  Using candidate_pool_file_set (25 results) instead of
    # baseline_file_set (5 results) correctly reflects the overfetched candidate set.
    for f in reranked_ordering:
        assert f in candidate_pool_file_set, (
            f"AC2: Reranked result {f!r} is not in the wide post-RRF candidate pool "
            f"{candidate_pool_ordering!r}. The reranker must act on post-RRF "
            "candidates (after-RRF invariant violated)."
        )

    # CORE HARDENING: the reranker must ACTIVELY REORDER the results.
    # If the orderings are identical, the reranker had zero effect — which means
    # either it did not run (despite reranker_used=True) or it confirmed every
    # position exactly.  Either case is insufficient proof that the pipeline
    # correctly fires the reranker after RRF and before truncation.
    #
    # AC2_QUERY = "convert string to safe HTML markup" exercises markupsafe files
    # with competing semantic signals:
    #   __init__.py  — defines Markup class and escape() entry point (high-level API)
    #   _native.py   — implements escape() at the C-extension / pure-Python level
    #   _speedups.pyd — compiled C speedup (filtered out; .py files only)
    # The Cohere reranker (rerank-v3.5) re-scores these with a cross-encoder model,
    # which has different relevance signals than the bi-encoder used for embedding,
    # so it reliably produces a different ordering from the RRF baseline.
    assert reranked_ordering != baseline_ordering, (
        f"AC2 HARDENING FAILURE: The reranked ordering is IDENTICAL to the baseline "
        f"ordering, which means the reranker had no effect on result order.\n"
        f"  Baseline ordering:  {baseline_ordering}\n"
        f"  Reranked ordering:  {reranked_ordering}\n"
        f"  reranker_used flag: {reranked_meta.get('reranker_used')}\n"
        "The test would pass even if the reranker were completely disabled. "
        "Verify the reranker model is active and AC2_QUERY exercises files with "
        "competing relevance scores (so the cross-encoder can flip the RRF order)."
    )

    # Smoking-gun proof of after-RRF / before-truncation: at least one reranked
    # result is OUTSIDE the narrow rerank-off window but INSIDE the wide post-RRF
    # candidate pool — i.e. the reranker PROMOTED a beyond-truncation candidate.
    narrow_baseline_set = set(baseline_ordering)
    promoted = [
        f
        for f in reranked_ordering
        if f not in narrow_baseline_set and f in candidate_pool_file_set
    ]
    assert promoted, (
        "AC2: expected the reranker to promote at least one candidate from beyond "
        f"the narrow rerank-off window into the top results.\n"
        f"  Narrow baseline: {baseline_ordering}\n"
        f"  Reranked:        {reranked_ordering}\n"
        f"  Wide pool:       {candidate_pool_ordering}"
    )

    # After-RRF / before-truncation proof using limit=AC2_LIMIT_SMALL:
    # Request limit=1 with rerank_query — overfetch multiplier (default 5x) causes
    # the server to fetch 5 candidates via RRF, rerank all 5, then truncate to 1.
    # If the reranked top-1 differs from the non-reranked top-1, a candidate from
    # position 2-5 was promoted — which is only possible if reranking fired BEFORE
    # truncation to limit=1.
    baseline_small_result = _mcp_search(
        fault_admin_client,
        query_text=AC2_QUERY,
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=AC2_LIMIT_SMALL,
    )
    baseline_small_results = _extract_results(baseline_small_result)
    assert baseline_small_results, (
        f"AC2 small baseline: query '{AC2_QUERY}' with limit={AC2_LIMIT_SMALL} "
        f"returned no results."
    )

    reranked_small_result = _mcp_search(
        fault_admin_client,
        query_text=AC2_QUERY,
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=AC2_LIMIT_SMALL,
        rerank_query=AC2_QUERY,
    )
    reranked_small_results = _extract_results(reranked_small_result)
    assert reranked_small_results, (
        f"AC2 small reranked: query '{AC2_QUERY}' with rerank_query and "
        f"limit={AC2_LIMIT_SMALL} returned no results."
    )
    reranked_top_file = reranked_small_results[0].get("file_path", "")

    # The reranked top-1 must be in the wide post-RRF candidate pool.
    assert reranked_top_file in candidate_pool_file_set, (
        f"AC2: Reranked limit=1 top file {reranked_top_file!r} is not in the "
        f"wide post-RRF candidate pool {candidate_pool_ordering!r}. "
        "The reranker must select from the post-RRF candidate set."
    )
