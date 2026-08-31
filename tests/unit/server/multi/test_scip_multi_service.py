"""
TDD tests for SCIPMultiService (AC1-AC8: Multi-Repository SCIP Intelligence).

Tests written FIRST before implementation.

Verifies:
AC1: Multi-Repository Definition Lookup
AC2: Multi-Repository Reference Lookup
AC3: Multi-Repository Dependency Analysis
AC4: Multi-Repository Dependents Analysis
AC5: Per-Repository Call Chain Tracing (no cross-repo stitching)
AC6: Result Aggregation with Repository Attribution
AC7: Timeout Handling (30s) with Recommendations
AC8: SCIP Index Availability Handling
"""

import pytest
from unittest.mock import patch
from code_indexer.server.multi.scip_models import (
    SCIPMultiRequest,
)
from code_indexer.scip.query.primitives import QueryResult


class TestSCIPMultiServiceDefinition:
    """Test multi-repository definition lookup (AC1)."""

    @pytest.mark.asyncio
    async def test_definition_across_multiple_repos(self):
        """Find definition across multiple repositories."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"], symbol="UserService"
        )

        # Mock the single-repo definition method
        with patch.object(service, "_find_definition_in_repo") as mock_find:
            # Repo1 has definition, repo2 doesn't
            mock_find.side_effect = [
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo1",
                        file_path="src/auth.py",
                        line=42,
                        column=4,
                        kind="definition",
                    )
                ],
                [],  # repo2 has no definition
            ]

            response = service.definition(request)

            assert response.metadata.repos_searched == 2
            assert response.metadata.repos_with_results == 1
            assert len(response.results["repo1"]) == 1
            assert response.results["repo1"][0].symbol == "UserService"
            assert response.results["repo1"][0].kind == "definition"

    @pytest.mark.asyncio
    async def test_definition_no_scip_index(self):
        """Handle repos without SCIP indexes gracefully."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo_no_scip"], symbol="UserService"
        )

        with patch.object(service, "_find_definition_in_repo") as mock_find:
            # repo1 has definition, repo_no_scip has no SCIP index
            mock_find.side_effect = [
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo1",
                        file_path="src/auth.py",
                        line=42,
                        column=4,
                        kind="definition",
                    )
                ],
                None,  # Indicates no SCIP index
            ]

            response = service.definition(request)

            assert response.metadata.repos_searched == 1
            assert "repo_no_scip" in response.skipped
            assert "No SCIP index" in response.skipped["repo_no_scip"]


class TestSCIPMultiServiceReferences:
    """Test multi-repository reference lookup (AC2)."""

    @pytest.mark.asyncio
    async def test_references_across_multiple_repos(self):
        """Find references across multiple repositories."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"], symbol="UserService"
        )

        with patch.object(service, "_find_references_in_repo") as mock_find:
            # Both repos have references
            mock_find.side_effect = [
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo1",
                        file_path="tests/test_auth.py",
                        line=10,
                        column=0,
                        kind="reference",
                    )
                ],
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo2",
                        file_path="lib/user.py",
                        line=5,
                        column=4,
                        kind="reference",
                    )
                ],
            ]

            response = service.references(request)

            assert response.metadata.repos_searched == 2
            assert response.metadata.repos_with_results == 2
            assert len(response.results["repo1"]) == 1
            assert len(response.results["repo2"]) == 1
            assert response.results["repo1"][0].kind == "reference"
            assert response.results["repo2"][0].kind == "reference"


class TestSCIPMultiServiceDependencies:
    """Test multi-repository dependency analysis (AC3)."""

    @pytest.mark.asyncio
    async def test_dependencies_across_multiple_repos(self):
        """Find dependencies across multiple repositories."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"], symbol="UserService"
        )

        with patch.object(service, "_get_dependencies_in_repo") as mock_deps:
            # Both repos have dependencies
            mock_deps.side_effect = [
                [
                    QueryResult(
                        symbol="DatabaseConnection",
                        project="repo1",
                        file_path="src/auth.py",
                        line=5,
                        column=0,
                        kind="dependency",
                    )
                ],
                [
                    QueryResult(
                        symbol="Logger",
                        project="repo2",
                        file_path="lib/user.py",
                        line=2,
                        column=0,
                        kind="dependency",
                    )
                ],
            ]

            response = service.dependencies(request)

            assert response.metadata.repos_searched == 2
            assert response.metadata.repos_with_results == 2
            assert response.results["repo1"][0].kind == "dependency"
            assert response.results["repo2"][0].kind == "dependency"

    @pytest.mark.parametrize("invalid_max_depth", [11, 0])
    def test_get_dependencies_in_repo_rejects_out_of_range_max_depth(
        self, tmp_path, invalid_max_depth
    ):
        """Bug #1626: unlike _execute_callchain (which explicitly validates
        max_depth before ever calling the engine), _get_dependencies_in_repo
        passed request.max_depth straight through to
        engine.get_dependencies(), relying on the engine's own deep
        ValueError (queries.py, Bug #1625: range 1-10) to propagate up.
        This must now raise ValueError with the engine's own bounds/message
        BEFORE SCIPQueryEngine is ever constructed or called, mirroring
        _execute_callchain's established guard pattern exactly."""
        from unittest.mock import MagicMock
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=invalid_max_depth
        )

        mock_engine = MagicMock()

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            with patch(
                "code_indexer.server.multi.scip_multi_service.SCIPQueryEngine",
                return_value=mock_engine,
            ) as mock_engine_cls:
                with pytest.raises(ValueError, match="Depth must be between 1 and 10"):
                    service._get_dependencies_in_repo("repo1", request)

        mock_engine_cls.assert_not_called()
        mock_engine.get_dependencies.assert_not_called()

    def test_dependencies_surfaces_invalid_max_depth_via_errors_map(self, tmp_path):
        """End-to-end through dependencies()/_execute_parallel_operation: an
        invalid max_depth must surface as a per-repo error entry, not a
        silent 200 success."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=11
        )

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            response = service.dependencies(request)

        assert response.errors is not None
        assert "repo1" in response.errors
        assert "Depth must be between 1 and 10" in response.errors["repo1"]
        assert "repo1" not in response.results

    @pytest.mark.parametrize("valid_max_depth", [1, 10])
    def test_get_dependencies_in_repo_accepts_boundary_depths(
        self, tmp_path, valid_max_depth
    ):
        """Valid boundary depths (1 and 10) must still reach the engine
        unchanged -- the new guard must not reject in-range values."""
        from unittest.mock import MagicMock
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=valid_max_depth
        )

        mock_engine = MagicMock()
        mock_engine.get_dependencies.return_value = []

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            with patch(
                "code_indexer.server.multi.scip_multi_service.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                result = service._get_dependencies_in_repo("repo1", request)

        assert result == []
        mock_engine.get_dependencies.assert_called_once_with(
            "UserService", depth=valid_max_depth, exact=False
        )


class TestSCIPMultiServiceDependents:
    """Test multi-repository dependents analysis (AC4)."""

    @pytest.mark.asyncio
    async def test_dependents_across_multiple_repos(self):
        """Find dependents across multiple repositories."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"], symbol="UserService"
        )

        with patch.object(service, "_get_dependents_in_repo") as mock_deps:
            # Both repos have dependents
            mock_deps.side_effect = [
                [
                    QueryResult(
                        symbol="APIHandler",
                        project="repo1",
                        file_path="src/api.py",
                        line=20,
                        column=4,
                        kind="dependent",
                    )
                ],
                [
                    QueryResult(
                        symbol="WebController",
                        project="repo2",
                        file_path="controllers/user.py",
                        line=15,
                        column=0,
                        kind="dependent",
                    )
                ],
            ]

            response = service.dependents(request)

            assert response.metadata.repos_searched == 2
            assert response.metadata.repos_with_results == 2
            assert response.results["repo1"][0].kind == "dependent"
            assert response.results["repo2"][0].kind == "dependent"

    @pytest.mark.parametrize("invalid_max_depth", [11, 0])
    def test_get_dependents_in_repo_rejects_out_of_range_max_depth(
        self, tmp_path, invalid_max_depth
    ):
        """Bug #1626: unlike _execute_callchain (which explicitly validates
        max_depth before ever calling the engine), _get_dependents_in_repo
        passed request.max_depth straight through to
        engine.get_dependents(), relying on the engine's own deep
        ValueError (queries.py, Bug #1625: range 1-10) to propagate up.
        This must now raise ValueError with the engine's own bounds/message
        BEFORE SCIPQueryEngine is ever constructed or called, mirroring
        _execute_callchain's established guard pattern exactly."""
        from unittest.mock import MagicMock
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=invalid_max_depth
        )

        mock_engine = MagicMock()

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            with patch(
                "code_indexer.server.multi.scip_multi_service.SCIPQueryEngine",
                return_value=mock_engine,
            ) as mock_engine_cls:
                with pytest.raises(ValueError, match="Depth must be between 1 and 10"):
                    service._get_dependents_in_repo("repo1", request)

        mock_engine_cls.assert_not_called()
        mock_engine.get_dependents.assert_not_called()

    def test_dependents_surfaces_invalid_max_depth_via_errors_map(self, tmp_path):
        """End-to-end through dependents()/_execute_parallel_operation: an
        invalid max_depth must surface as a per-repo error entry, not a
        silent 200 success."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=0
        )

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            response = service.dependents(request)

        assert response.errors is not None
        assert "repo1" in response.errors
        assert "Depth must be between 1 and 10" in response.errors["repo1"]
        assert "repo1" not in response.results

    @pytest.mark.parametrize("valid_max_depth", [1, 10])
    def test_get_dependents_in_repo_accepts_boundary_depths(
        self, tmp_path, valid_max_depth
    ):
        """Valid boundary depths (1 and 10) must still reach the engine
        unchanged -- the new guard must not reject in-range values."""
        from unittest.mock import MagicMock
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"], symbol="UserService", max_depth=valid_max_depth
        )

        mock_engine = MagicMock()
        mock_engine.get_dependents.return_value = []

        with patch.object(
            service,
            "_get_scip_file_for_repo",
            return_value=tmp_path / "index.scip.db",
        ):
            with patch(
                "code_indexer.server.multi.scip_multi_service.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                result = service._get_dependents_in_repo("repo1", request)

        assert result == []
        mock_engine.get_dependents.assert_called_once_with(
            "UserService", depth=valid_max_depth, exact=False
        )


class TestSCIPMultiServiceCallChain:
    """Test per-repository call chain tracing (AC5)."""

    @pytest.mark.asyncio
    async def test_callchain_per_repository_no_stitching(self):
        """Trace call chains per repository without cross-repo stitching."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"],
            symbol="",  # Not used for callchain
            from_symbol="api_handler",
            to_symbol="database_query",
        )

        with patch.object(service, "_trace_callchain_in_repo") as mock_chain:
            # Each repo has its own call chain (with different lengths to verify independence)
            mock_chain.side_effect = [
                [
                    QueryResult(
                        symbol="api_handler -> service -> database_query",
                        project="repo1",
                        file_path="",
                        line=0,
                        column=0,
                        kind="callchain",
                        context="api_handler -> service -> database_query",
                    )
                ],
                [
                    QueryResult(
                        symbol="api_handler -> database_query",
                        project="repo2",
                        file_path="",
                        line=0,
                        column=0,
                        kind="callchain",
                        context="api_handler -> database_query",
                    )
                ],
            ]

            response = service.callchain(request)

            # Each repo has independent call chains (no cross-repo stitching)
            assert response.metadata.repos_searched == 2
            assert response.metadata.repos_with_results == 2
            assert len(response.results["repo1"]) == 1
            assert len(response.results["repo2"]) == 1

            # Verify chains are separate by checking they have different lengths
            # repo1 chain has 3 symbols (includes 'service'), repo2 has 2 symbols
            repo1_chain = response.results["repo1"][0].context
            repo2_chain = response.results["repo2"][0].context
            assert "service" in repo1_chain  # repo1 has intermediate symbol
            assert (
                "service" not in repo2_chain
            )  # repo2 doesn't have intermediate symbol

            # Verify repository attribution is correct
            assert response.results["repo1"][0].repository == "repo1"
            assert response.results["repo2"][0].repository == "repo2"

    @pytest.mark.parametrize("invalid_max_depth", [100000, 0])
    def test_trace_callchain_in_repo_rejects_out_of_range_max_depth(
        self, tmp_path, invalid_max_depth
    ):
        """Bug #1603 code review round 2 (Priority 1, item 2): a
        caller-supplied out-of-range max_depth (above the cap, e.g.
        100000, or below the minimum, e.g. 0) used to be silently
        clamped into [1, 3] with only a WARNING log -- inconsistent with
        the MCP handler (scip.py's scip_callchain, which REJECTS via
        _MAX_CALLCHAIN_DEPTH) and the REST route (scip_queries.py's
        FastAPI Query(le=3) -> HTTP 422). _trace_callchain_in_repo must
        now raise ValueError for an explicit out-of-range max_depth
        rather than silently rewriting it."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"],
            symbol="",
            from_symbol="api_handler",
            to_symbol="database_query",
            max_depth=invalid_max_depth,
        )

        with patch.object(
            service, "_get_scip_file_for_repo", return_value=tmp_path / "index.scip"
        ):
            with pytest.raises(ValueError, match="max_depth must be between 1 and 3"):
                service._trace_callchain_in_repo("repo1", request)

    def test_callchain_surfaces_invalid_max_depth_via_errors_map(self, tmp_path):
        """End-to-end through callchain()/_execute_parallel_operation: an
        invalid max_depth must surface as a per-repo error entry, not a
        silent 200 success with a rewritten depth."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"],
            symbol="",
            from_symbol="api_handler",
            to_symbol="database_query",
            max_depth=100000,
        )

        with patch.object(
            service, "_get_scip_file_for_repo", return_value=tmp_path / "index.scip"
        ):
            response = service.callchain(request)

        assert response.errors is not None
        assert "repo1" in response.errors
        assert "max_depth must be between 1 and 3" in response.errors["repo1"]
        assert "repo1" not in response.results


class TestSCIPMultiServiceCallChainTimeout:
    """Bug #1603 code review round 2 (Priority 1, item 1): both round-2
    reviewers independently found that _trace_callchain_in_repo called
    engine.trace_call_chain(...) WITHOUT passing timeout_errors=, so
    POST /api/scip/multi/callchain turned a query timeout into a silent
    empty-result success -- the exact 'timeout reported as success' bug
    round 1 was supposed to close everywhere, left open on this one
    front door. Verifies the fix: a non-empty timeout_errors list
    (mutated in place by the real trace_call_chain contract) must surface
    as a per-repo failure through the errors map, never an empty success
    in the results map."""

    def test_timeout_surfaces_as_per_repo_error_not_silent_empty_success(
        self, tmp_path
    ):
        from unittest.mock import patch, MagicMock
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1"],
            symbol="",
            from_symbol="api_handler",
            to_symbol="database_query",
        )

        def _fake_trace_call_chain(
            from_symbol, to_symbol, max_depth, limit, timeout_errors
        ):
            # Mirrors the real contract: the backend mutates the
            # caller-supplied list in place on timeout instead of
            # raising or returning a sentinel.
            timeout_errors.append("Query exceeded 30-second timeout")
            return []

        mock_engine = MagicMock()
        mock_engine.trace_call_chain.side_effect = _fake_trace_call_chain

        with patch.object(
            service, "_get_scip_file_for_repo", return_value=tmp_path / "index.scip"
        ):
            with patch(
                "code_indexer.server.multi.scip_multi_service.SCIPQueryEngine",
                return_value=mock_engine,
            ):
                response = service.callchain(request)

        assert "repo1" not in response.results, (
            "A timed-out callchain query must not be reported in the "
            "results map as an (empty) success"
        )
        assert response.errors is not None
        assert "repo1" in response.errors
        assert "timed out" in response.errors["repo1"].lower()


class TestSCIPMultiServiceTimeout:
    """Test timeout handling with recommendations (AC7)."""

    @pytest.mark.asyncio
    async def test_timeout_parameter_accepted(self):
        """Service accepts timeout parameter and processes multiple repos."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        # Verify service accepts custom timeout
        service = SCIPMultiService(query_timeout_seconds=1)
        assert service.query_timeout_seconds == 1

        request = SCIPMultiRequest(
            repositories=["repo1", "repo2"], symbol="UserService"
        )

        with patch.object(service, "_find_definition_in_repo") as mock_find:
            # Both repos succeed quickly
            mock_find.return_value = [
                QueryResult(
                    symbol="UserService",
                    project="repo1",
                    file_path="src/auth.py",
                    line=42,
                    column=4,
                    kind="definition",
                )
            ]

            response = service.definition(request)

            # Verify both repos were processed
            assert response.metadata.repos_searched == 2
            assert len(response.results) == 2


class TestSCIPMultiServiceResultAggregation:
    """Test result aggregation with repository attribution (AC6)."""

    @pytest.mark.asyncio
    async def test_results_grouped_by_repository(self):
        """Results are grouped by repository with proper attribution."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo2", "repo3"], symbol="UserService"
        )

        with patch.object(service, "_find_definition_in_repo") as mock_find:
            mock_find.side_effect = [
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo1",
                        file_path="src/auth.py",
                        line=42,
                        column=4,
                        kind="definition",
                    )
                ],
                [],  # repo2 has no results
                [
                    QueryResult(
                        symbol="UserService",
                        project="repo3",
                        file_path="lib/auth.py",
                        line=10,
                        column=0,
                        kind="definition",
                    )
                ],
            ]

            response = service.definition(request)

            # Verify grouping by repository
            assert "repo1" in response.results
            assert "repo2" in response.results  # Searched but no results (empty list)
            assert "repo3" in response.results

            # Verify repo2 has empty results
            assert len(response.results["repo2"]) == 0

            # Verify attribution
            assert response.results["repo1"][0].repository == "repo1"
            assert response.results["repo3"][0].repository == "repo3"

            # Verify metadata
            assert response.metadata.repos_searched == 3
            assert (
                response.metadata.repos_with_results == 2
            )  # repo1 and repo3 have results
            assert response.metadata.total_results == 2


class TestSCIPMultiServicePartialFailure:
    """Test partial failure handling."""

    @pytest.mark.asyncio
    async def test_partial_failure_continues_other_repos(self):
        """Continue with other repos when one fails."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()
        request = SCIPMultiRequest(
            repositories=["repo1", "repo_error", "repo3"], symbol="UserService"
        )

        with patch.object(service, "_find_definition_in_repo") as mock_find:

            def find_with_error(repo_id, symbol):
                if repo_id == "repo_error":
                    raise RuntimeError("Database connection failed")
                return [
                    QueryResult(
                        symbol="UserService",
                        project=repo_id,
                        file_path="src/auth.py",
                        line=42,
                        column=4,
                        kind="definition",
                    )
                ]

            mock_find.side_effect = find_with_error

            response = service.definition(request)

            # repo1 and repo3 succeed, repo_error fails
            assert len(response.results) == 2
            assert "repo1" in response.results
            assert "repo3" in response.results
            assert "repo_error" in response.errors
            assert "Database connection failed" in response.errors["repo_error"]
