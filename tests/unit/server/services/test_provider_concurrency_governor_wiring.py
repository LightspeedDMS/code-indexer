"""Source-text wiring invariant guards for Bug #1078 Phase 1.

These tests do NOT execute the production code path — they assert structural
invariants in source text that must hold for the governor gating to remain
effective.  They act as trip-wires: if anyone accidentally removes a governor
call or re-introduces a factory bypass, these tests will fail immediately.

Test classification: fast, deterministic, zero I/O.
"""

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SRC = Path(__file__).parent.parent.parent.parent.parent / "src"


def _read(rel_path: str) -> str:
    return (_SRC / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Indexing-path isolation guards
# ---------------------------------------------------------------------------


class TestIndexingPathIsolation:
    """Governor symbols MUST NOT appear in the indexing/CLI paths.

    The indexing path (VectorCalculationManager, get_embeddings_batch,
    _make_sync_request, voyage_ai._make_sync_request, cohere embedding
    internals) must remain ungated so batch throughput is not throttled.
    """

    def test_governor_absent_from_voyage_ai(self):
        src = _read("code_indexer/services/voyage_ai.py")
        assert "ProviderConcurrencyGovernor" not in src, (
            "voyage_ai.py must NOT reference ProviderConcurrencyGovernor — "
            "indexing batch path must be ungated"
        )

    def test_governor_absent_from_cohere_embedding(self):
        src = _read("code_indexer/services/cohere_embedding.py")
        assert "ProviderConcurrencyGovernor" not in src, (
            "cohere_embedding.py must NOT reference ProviderConcurrencyGovernor"
        )

    def test_governor_absent_from_vector_calculation_manager(self):
        # Try both possible locations
        try:
            src = _read("code_indexer/services/vector_calculation_manager.py")
        except FileNotFoundError:
            src = _read("code_indexer/services/dual_vector_calculation_manager.py")
        assert "ProviderConcurrencyGovernor" not in src, (
            "VectorCalculationManager must NOT reference ProviderConcurrencyGovernor"
        )

    def test_get_embeddings_batch_ungated(self):
        """get_embeddings_batch in voyage_ai.py must not call governor.execute."""
        src = _read("code_indexer/services/voyage_ai.py")
        assert "get_embeddings_batch" in src, (
            "sanity: voyage_ai.py must define get_embeddings_batch"
        )
        # Parse AST and check that get_embeddings_batch body has no governor calls
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "get_embeddings_batch":
                    func_src = ast.unparse(node)
                    assert "ProviderConcurrencyGovernor" not in func_src, (
                        "get_embeddings_batch must NOT call ProviderConcurrencyGovernor"
                    )
                    return
        pytest.fail("get_embeddings_batch not found in voyage_ai.py")

    def test_make_sync_request_ungated(self):
        """_make_sync_request in voyage_ai.py must not call governor.execute."""
        src = _read("code_indexer/services/voyage_ai.py")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_make_sync_request":
                    func_src = ast.unparse(node)
                    assert "ProviderConcurrencyGovernor" not in func_src, (
                        "_make_sync_request must NOT call ProviderConcurrencyGovernor"
                    )
                    return
        # _make_sync_request may be called differently; ensure governor is absent overall
        assert "ProviderConcurrencyGovernor" not in src


# ---------------------------------------------------------------------------
# Gating site presence guards
# ---------------------------------------------------------------------------


class TestGatingSitePresence:
    """Governor gating MUST appear in each of the 5 serving call sites."""

    def test_site1_search_service_pg_backend_gated(self):
        """search_service.py PG-backend embedding call must be gated via governed_query_embedding.

        After the Bug #1078 refactor, gating is centralised in governed_call.py.
        Each site delegates to governed_query_embedding() rather than inlining the
        ProviderConcurrencyGovernor/execute_with_backoff boilerplate directly.
        """
        src = _read("code_indexer/server/services/search_service.py")
        assert "governed_query_embedding" in src, (
            "search_service.py must gate embedding via governed_query_embedding "
            "(from server/services/governed_call.py) — direct ProviderConcurrencyGovernor "
            "usage was consolidated into the shared helper"
        )

    def test_site2_filesystem_vector_store_gated(self):
        """filesystem_vector_store.py generate_embedding closure must be gated via governed_query_embedding."""
        src = _read("code_indexer/storage/filesystem_vector_store.py")
        assert "governed_query_embedding" in src, (
            "filesystem_vector_store.py must gate embedding via governed_query_embedding "
            "(from server/services/governed_call.py)"
        )

    def test_site3_memory_handler_gated(self):
        """handlers/search.py _compute_memory_query_vector must be gated via governed_query_embedding."""
        src = _read("code_indexer/server/mcp/handlers/search.py")
        assert "governed_query_embedding" in src, (
            "handlers/search.py must gate memory embedding via governed_query_embedding "
            "(from server/services/governed_call.py)"
        )

    def test_site4_reranking_gated(self):
        """reranking.py _attempt_provider_rerank must be gated directly (rerank is not an embedding call)."""
        src = _read("code_indexer/server/mcp/reranking.py")
        assert "ProviderConcurrencyGovernor" in src, (
            "reranking.py must gate rerank call via ProviderConcurrencyGovernor — "
            "reranking uses client.rerank(), a different operation not covered by "
            "governed_query_embedding()"
        )
        assert "_RERANKER_BUDGET" in src

    def test_site5_temporal_search_service_gated(self):
        """temporal_search_service.py embedding call must be gated via governed_query_embedding."""
        src = _read("code_indexer/services/temporal/temporal_search_service.py")
        assert "governed_query_embedding" in src, (
            "temporal_search_service.py must gate embedding via governed_query_embedding "
            "(from server/services/governed_call.py)"
        )


# ---------------------------------------------------------------------------
# Fault-injection factory wiring guards
# ---------------------------------------------------------------------------


class TestFaultInjectionFactoryWiring:
    """Constructors that previously bypassed fault injection now pass the factory."""

    def test_memory_handler_passes_factory_to_voyage_client(self):
        """_compute_memory_query_vector must pass http_client_factory to VoyageAIClient.

        The factory is retrieved via _get_http_client_factory() (with an AttributeError
        guard for test environments) and passed as http_client_factory= to the client.
        """
        src = _read("code_indexer/server/mcp/handlers/search.py")
        # Factory is fetched and then passed separately (AttributeError guard refactor).
        assert "_get_http_client_factory()" in src, (
            "handlers/search.py must call _get_http_client_factory() to obtain the factory"
        )
        assert "http_client_factory=_factory" in src, (
            "handlers/search.py must pass the factory as http_client_factory= to VoyageAIClient"
        )

    def test_reranking_passes_factory_to_reranker_clients(self):
        """_attempt_provider_rerank must pass http_client_factory to reranker client constructor.

        The factory is retrieved via _get_http_client_factory() (with an AttributeError
        guard for test environments) and passed as http_client_factory= to the client.
        """
        src = _read("code_indexer/server/mcp/reranking.py")
        # Factory is fetched and then passed separately (AttributeError guard refactor).
        assert "_get_http_client_factory()" in src, (
            "reranking.py must call _get_http_client_factory() to obtain the factory"
        )
        assert "http_client_factory=_factory" in src, (
            "reranking.py must pass the factory as http_client_factory= to the reranker client"
        )


# ---------------------------------------------------------------------------
# Config field guard
# ---------------------------------------------------------------------------


class TestConfigField:
    def test_query_provider_max_concurrency_in_server_config(self):
        """ServerConfig must declare the query_provider_max_concurrency field."""
        src = _read("code_indexer/server/utils/config_manager.py")
        assert "query_provider_max_concurrency" in src, (
            "ServerConfig must declare query_provider_max_concurrency field"
        )

    def test_governor_reads_config_field(self):
        """ProviderConcurrencyGovernor must attempt to read query_provider_max_concurrency."""
        src = _read("code_indexer/server/services/provider_concurrency_governor.py")
        assert "query_provider_max_concurrency" in src, (
            "ProviderConcurrencyGovernor must read query_provider_max_concurrency from config"
        )


# ---------------------------------------------------------------------------
# C1: execute_with_backoff must wrap governor.execute at every serving site
# ---------------------------------------------------------------------------


class TestExecuteWithBackoffWiring:
    """Bug #1078 C1: execute_with_backoff must wrap governor.execute at all 5 call sites.

    After the refactor, embedding sites 1/2/3/5 delegate to the shared helper
    governed_call.py::governed_query_embedding(), which inlines the canonical:
        execute_with_backoff(lambda: governor.execute(budget, lambda: provider.get_embedding(...)))

    The invariant is preserved at two levels:
      (a) governed_call.py must contain BOTH execute_with_backoff AND governor.execute.
      (b) Each embedding site must call governed_query_embedding (routing to that helper).
      (c) Reranking (site 4) retains execute_with_backoff directly since it uses
          a different client operation (client.rerank()) not covered by the helper.
    """

    def test_governed_call_helper_has_execute_with_backoff_and_governor_execute(self):
        """governed_call.py must contain both execute_with_backoff AND governor.execute.

        This is the canonical location of the backoff-outside-slot wiring since the
        Bug #1078 refactor centralised all 4 embedding-site call patterns here.
        """
        src = _read("code_indexer/server/services/governed_call.py")
        assert "execute_with_backoff" in src, (
            "governed_call.py must call execute_with_backoff — "
            "backoff-outside-slot wiring lives in this shared helper"
        )
        assert "governor.execute" in src, (
            "governed_call.py must call governor.execute — "
            "the governor slot acquisition lives in this shared helper"
        )

    def test_site1_search_service_uses_governed_query_embedding(self):
        """search_service.py PG-backend path must delegate to governed_query_embedding."""
        src = _read("code_indexer/server/services/search_service.py")
        assert "governed_query_embedding" in src, (
            "search_service.py must call governed_query_embedding() from governed_call.py — "
            "execute_with_backoff+governor.execute wiring now lives in the shared helper"
        )

    def test_site2_filesystem_vector_store_uses_governed_query_embedding(self):
        """filesystem_vector_store.py generate_embedding closure must delegate to governed_query_embedding."""
        src = _read("code_indexer/storage/filesystem_vector_store.py")
        assert "governed_query_embedding" in src, (
            "filesystem_vector_store.py must call governed_query_embedding() from governed_call.py"
        )

    def test_site3_memory_handler_uses_governed_query_embedding(self):
        """handlers/search.py _compute_memory_query_vector must delegate to governed_query_embedding."""
        src = _read("code_indexer/server/mcp/handlers/search.py")
        assert "governed_query_embedding" in src, (
            "handlers/search.py must call governed_query_embedding() from governed_call.py"
        )

    def test_site4_reranking_uses_execute_with_backoff(self):
        """reranking.py _attempt_provider_rerank must use execute_with_backoff directly.

        Reranking is NOT an embedding operation — it calls client.rerank() —
        so it is intentionally excluded from governed_query_embedding().
        The backoff-outside-slot wiring must remain directly in reranking.py.
        """
        src = _read("code_indexer/server/mcp/reranking.py")
        assert "execute_with_backoff" in src, (
            "reranking.py must call execute_with_backoff to wrap governor.execute — "
            "reranking uses client.rerank() and is excluded from governed_query_embedding()"
        )

    def test_site5_temporal_search_uses_governed_query_embedding(self):
        """temporal_search_service.py embedding call must delegate to governed_query_embedding."""
        src = _read("code_indexer/services/temporal/temporal_search_service.py")
        assert "governed_query_embedding" in src, (
            "temporal_search_service.py must call governed_query_embedding() from governed_call.py"
        )


# ---------------------------------------------------------------------------
# C2: in-slot 429 sleep must be absent from provider _make_sync_request bodies
# ---------------------------------------------------------------------------


class TestInSlotSleepAbsent:
    """Bug #1078 C2 + B1: _make_sync_request 429 handling invariants.

    The QUERY path (retry=False) must NEVER sleep inside the governor slot — it
    must raise immediately on 429 so execute_with_backoff can sleep OUTSIDE the
    slot.

    The INDEXING path (retry=True) is NOT called from inside a governor slot and
    MAY sleep+retry on 429 (B1 fix).  When time.sleep is present in the 429
    branch it MUST be guarded by a `retry` check so governor-slot callers
    (retry=False) are never affected.

    Invariant: the 429 branch must contain `raise` (query path raises) AND if
    it also contains `time.sleep` the branch must contain the `not retry` guard
    that ensures the query path raises before reaching any sleep.
    """

    def _get_function_src(self, module_rel_path: str, func_name: str) -> str:
        """Parse and return source of a named function using AST."""
        import ast

        src = _read(module_rel_path)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    return ast.unparse(node)
        raise AssertionError(f"{func_name} not found in {module_rel_path}")

    def _get_429_branch_src(self, func_src: str) -> str:
        """Extract the source lines in the 429-handling branch using AST.

        Finds an If node whose test compares something to 429 and returns
        the unparsed body of that branch.  Returns '' if no such branch exists.
        """
        import ast

        tree = ast.parse(func_src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # Match: x == 429 or 429 == x  (also status_code == 429)
            test = node.test
            is_429_eq = False
            if isinstance(test, ast.Compare):
                for comparator in test.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value == 429:
                        is_429_eq = True
                if isinstance(test.left, ast.Constant) and test.left.value == 429:
                    is_429_eq = True
            if is_429_eq:
                return ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        return ""

    def _assert_429_branch_invariants(self, branch_429: str, service_name: str) -> None:
        """Assert B1+C2 invariants on a 429 branch body (unparsed AST).

        Rules:
        1. `raise` must be present (query path must propagate the error).
        2. If `time.sleep` is present (indexing retry), `not retry` must also be
           present as the guard that ensures the query path raises before sleeping.
        """
        assert "raise" in branch_429, (
            f"_make_sync_request in {service_name}: the 429 branch must raise "
            f"(propagate the error for the query/governor path). "
            f"429 branch source: {branch_429[:200]}"
        )
        if "time.sleep" in branch_429:
            # B1: sleep is allowed only when guarded behind `not retry` so the
            # query path (retry=False) exits before reaching any sleep.
            assert "not retry" in branch_429, (
                f"_make_sync_request in {service_name}: the 429 branch calls "
                f"time.sleep but does NOT guard it with `if not retry: raise`. "
                f"The query path (retry=False) must raise before any sleep to "
                f"avoid sleeping inside the governor slot. "
                f"429 branch source: {branch_429[:400]}"
            )

    def test_voyage_make_sync_request_no_429_sleep(self):
        """voyage_ai._make_sync_request 429 branch: raise present; sleep guarded by retry."""
        func_src = self._get_function_src(
            "code_indexer/services/voyage_ai.py", "_make_sync_request"
        )
        branch_429 = self._get_429_branch_src(func_src)
        # If no 429 branch: nothing to check (governor layer handles it).
        if not branch_429:
            return
        self._assert_429_branch_invariants(branch_429, "voyage_ai.py")

    def test_cohere_make_sync_request_no_429_sleep(self):
        """cohere_embedding._make_sync_request 429 branch: raise present; sleep guarded by retry."""
        func_src = self._get_function_src(
            "code_indexer/services/cohere_embedding.py", "_make_sync_request"
        )
        branch_429 = self._get_429_branch_src(func_src)
        if not branch_429:
            return
        self._assert_429_branch_invariants(branch_429, "cohere_embedding.py")


# ---------------------------------------------------------------------------
# C3: Reranker _post must call raise_for_status (already true) AND the call
#     site must wrap with execute_with_backoff (checked in TestExecuteWithBackoffWiring)
# ---------------------------------------------------------------------------


class TestRerankerRaiseForStatus:
    """Reranker _post methods must call raise_for_status so 429 propagates as exception."""

    def test_voyage_reranker_post_calls_raise_for_status(self):
        """VoyageRerankerClient._post (or rerank) must call raise_for_status."""
        src = _read("code_indexer/server/clients/reranker_clients.py")
        # raise_for_status must appear in the file (both Voyage and Cohere use it)
        assert "raise_for_status" in src, (
            "reranker_clients.py must call raise_for_status so 429 propagates to execute_with_backoff"
        )


# ---------------------------------------------------------------------------
# M1: AttributeError factory guard must log at WARNING, not debug
# ---------------------------------------------------------------------------


class TestFactoryGuardLogLevel:
    """Bug #1078 M1: AttributeError factory-guard fallback must log at WARNING level.

    Silent debug-level logging masks lifespan wiring breaks during fault-injection E2E.
    The guard must use logger.warning() so the absence of http_client_factory is
    surfaced even in production log levels.
    """

    def test_search_handler_factory_guard_logs_warning(self):
        """handlers/search.py factory guard must use logger.warning, not logger.debug."""
        src = _read("code_indexer/server/mcp/handlers/search.py")
        # Split on "except AttributeError" to isolate the actual exception handler block
        # (not comments that contain the word "AttributeError").
        sections = src.split("except AttributeError")
        assert len(sections) > 1, (
            "handlers/search.py must have an 'except AttributeError' guard for factory"
        )
        # Check the block immediately after the except clause
        guard_section = sections[1]
        nearby = guard_section[:300]
        assert "logger.warning" in nearby, (
            "handlers/search.py AttributeError factory guard must use logger.warning(), "
            "not logger.debug() — a missing factory is a wiring break, not a debug note"
        )

    def test_reranking_factory_guard_logs_warning(self):
        """reranking.py factory guard must use logger.warning, not logger.debug."""
        src = _read("code_indexer/server/mcp/reranking.py")
        sections = src.split("except AttributeError")
        assert len(sections) > 1, (
            "reranking.py must have an 'except AttributeError' guard for factory"
        )
        guard_section = sections[1]
        nearby = guard_section[:300]
        assert "logger.warning" in nearby, (
            "reranking.py AttributeError factory guard must use logger.warning(), "
            "not logger.debug() — a missing factory is a wiring break, not a debug note"
        )
