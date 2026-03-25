"""
Query route handler extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 1 route handler:
- POST /api/query

Zero behavior change: same path, method, response models, and handler logic.
"""

import logging

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
)

from ..models.query import (
    SemanticQueryRequest,
    FTSResultItem,
)
from ..models.api_models import QueryResultItem
from ..query.semantic_query_manager import SemanticQueryError
from ..auth import dependencies
from ..logging_utils import format_error_log
from ..middleware.correlation import get_correlation_id
from ..app_helpers import (
    _apply_rest_semantic_truncation,
    _apply_rest_fts_truncation,
)

# Module-level logger
logger = logging.getLogger(__name__)


def register_query_routes(
    app: FastAPI,
    *,
    semantic_query_manager,
    activated_repo_manager,
) -> None:
    """
    Register the POST /api/query route handler onto the FastAPI app.

    The handler is defined as a closure over the function parameters,
    exactly as it was a closure over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        semantic_query_manager: SemanticQueryManager instance
        activated_repo_manager: ActivatedRepoManager instance
    """

    @app.post("/api/query")
    def semantic_query(
        request: SemanticQueryRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Unified search endpoint supporting semantic, FTS, and hybrid modes (Story 5).

        Args:
            request: Search request with mode selection and parameters
            current_user: Current authenticated user

        Returns:
            UnifiedSearchResponse for FTS/hybrid modes, or
            SemanticQueryResponse for backward compatibility with semantic mode

        Raises:
            HTTPException: If query fails, index missing, or invalid parameters
        """
        import time
        from pathlib import Path as PathLib

        start_time = time.time()

        try:
            # Handle background job submission (semantic mode only)
            if request.async_query:
                if request.search_mode != "semantic":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Async query only supported for semantic search mode",
                    )

                job_id = semantic_query_manager.submit_query_job(
                    username=current_user.username,
                    query_text=request.query_text,
                    repository_alias=request.repository_alias,
                    limit=request.limit,
                    min_score=request.min_score,
                    file_extensions=request.file_extensions,
                )
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "job_id": job_id,
                        "message": "Semantic query submitted as background job",
                    },
                )

            # Story 5: Handle FTS and Hybrid modes
            if request.search_mode in ["fts", "hybrid"]:
                # Get user's activated repositories
                activated_repos = activated_repo_manager.list_activated_repositories(
                    current_user.username
                )

                # ALSO get global repos from BackendRegistry (works in standalone and cluster mode)
                global_repos_list = []
                try:
                    backend_registry = getattr(app.state, "backend_registry", None)
                    if backend_registry:
                        repos_dict = backend_registry.global_repos.list_repos()
                        for alias_name, repo_data in repos_dict.items():
                            global_repos_list.append(
                                {
                                    "user_alias": repo_data["alias_name"],
                                    "username": "global",
                                    "is_global": True,
                                    "repo_url": repo_data.get("repo_url", ""),
                                }
                            )
                except Exception as e:
                    logger.warning(f"Failed to load global repos for FTS/hybrid: {e}")

                # Merge user repos and global repos
                activated_repos = activated_repos + global_repos_list

                if not activated_repos:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No activated repositories found for user",
                    )

                # Filter to specific repository if requested
                if request.repository_alias:
                    activated_repos = [
                        repo
                        for repo in activated_repos
                        if repo["user_alias"] == request.repository_alias
                    ]
                    if not activated_repos:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Repository '{request.repository_alias}' not found",
                        )

                # Check FTS index availability for each repository
                fts_available = False
                repo_path = None
                for repo in activated_repos:
                    # Construct path - different for global vs user repos
                    if repo.get("is_global"):
                        # Global repos: use AliasManager to resolve versioned snapshot path
                        from code_indexer.global_repos.alias_manager import AliasManager

                        golden_repos_dir = getattr(app.state, "golden_repos_dir", None)
                        if not golden_repos_dir:
                            logger.warning(
                                f"golden_repos_dir not configured, skipping global repo {repo['user_alias']}"
                            )
                            continue
                        alias_manager = AliasManager(
                            str(PathLib(golden_repos_dir) / "aliases")
                        )
                        resolved_path = alias_manager.read_alias(repo["user_alias"])
                        if not resolved_path:
                            logger.warning(
                                f"Failed to resolve alias path for {repo['user_alias']}, skipping"
                            )
                            continue
                        repo_path = PathLib(resolved_path)
                    else:
                        # User repos: activated-repos/username/alias
                        repo_path = (
                            PathLib(activated_repo_manager.activated_repos_dir)
                            / current_user.username
                            / repo["user_alias"]
                        )
                    if repo_path is None:
                        continue
                    fts_index_dir = repo_path / ".code-indexer" / "tantivy_index"
                    if fts_index_dir.exists():
                        fts_available = True
                        break

                # Validate search mode based on index availability
                search_mode_actual = request.search_mode
                if request.search_mode == "fts" and not fts_available:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "FTS index not available",
                            "suggestion": "Build FTS index with 'cidx index --fts' in the repository",
                            "available_modes": ["semantic"],
                        },
                    )

                if request.search_mode == "hybrid" and not fts_available:
                    # Graceful degradation for hybrid mode
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-032",
                            f"FTS index not available for user {current_user.username}, degrading hybrid to semantic-only",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    search_mode_actual = "semantic"

                # Execute FTS or hybrid search
                fts_results = []
                semantic_results_list = []

                if search_mode_actual in ["fts", "hybrid"] and fts_available:
                    # Execute FTS search
                    from ..services.tantivy_index_manager import TantivyIndexManager
                    from ..services.api_metrics_service import api_metrics_service

                    # Track FTS search at service layer (Story #4 AC2)
                    api_metrics_service.increment_other_index_search()

                    try:
                        # repo_path is guaranteed to be set if fts_available is True
                        if repo_path is None:
                            raise RuntimeError(
                                "repo_path is None despite FTS being available"
                            )

                        # Initialize Tantivy manager for first available repository
                        tantivy_manager = TantivyIndexManager(
                            repo_path / ".code-indexer" / "tantivy_index"
                        )
                        tantivy_manager.initialize_index(create_new=False)

                        # Handle fuzzy flag
                        edit_dist = request.edit_distance
                        if request.fuzzy and edit_dist == 0:
                            edit_dist = 1

                        # Execute FTS query
                        fts_raw_results = tantivy_manager.search(
                            query_text=request.query_text,
                            case_sensitive=request.case_sensitive,
                            edit_distance=edit_dist,
                            snippet_lines=request.snippet_lines,
                            limit=request.limit,
                            language_filter=request.language,
                            path_filter=request.path_filter,
                            exclude_languages=(
                                [request.exclude_language]
                                if request.exclude_language
                                else None
                            ),  # Story #503 Phase 1
                            exclude_paths=(
                                [request.exclude_path] if request.exclude_path else None
                            ),  # Story #503 Phase 1
                            use_regex=request.regex,  # Story #503 Phase 1
                        )

                        # Convert to API response format
                        for result in fts_raw_results:
                            fts_results.append(
                                FTSResultItem(
                                    path=result.get("path", ""),
                                    line_start=result.get("line_start", 0),
                                    line_end=result.get("line_end", 0),
                                    snippet=result.get("snippet", ""),
                                    language=result.get("language", "unknown"),
                                    repository_alias=request.repository_alias
                                    or activated_repos[0]["user_alias"],
                                )
                            )

                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-033",
                                f"FTS search failed: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        if request.search_mode == "fts":
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"FTS search failed: {str(e)}",
                            )
                        # For hybrid mode, continue with semantic only
                        search_mode_actual = "semantic"

                # Execute semantic search for hybrid or degraded mode
                if search_mode_actual in ["semantic", "hybrid"]:
                    try:
                        semantic_results_raw = (
                            semantic_query_manager.query_user_repositories(
                                username=current_user.username,
                                query_text=request.query_text,
                                repository_alias=request.repository_alias,
                                limit=request.limit,
                                min_score=request.min_score,
                                file_extensions=request.file_extensions,
                                # Phase 1 parameters (Story #503)
                                exclude_language=request.exclude_language,
                                exclude_path=request.exclude_path,
                                accuracy=request.accuracy,
                                # Temporal parameters (Story #446)
                                time_range=request.time_range,
                                time_range_all=request.time_range_all,
                                at_commit=request.at_commit,
                                include_removed=request.include_removed,
                                show_evolution=request.show_evolution,
                                evolution_limit=request.evolution_limit,
                                # Phase 3 temporal filtering parameters (Story #503)
                                diff_type=request.diff_type,
                                author=request.author,
                                chunk_type=request.chunk_type,
                            )
                        )
                        semantic_results_list = [
                            QueryResultItem(**result)
                            for result in semantic_results_raw["results"]
                        ]
                    except ValueError as e:
                        # Surface validation errors as HTTP 400
                        logger.warning(
                            format_error_log(
                                "APP-GENERAL-034",
                                f"Validation error in query: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "error": "Invalid query parameters",
                                "message": str(e),
                            },
                        )
                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-035",
                                f"Semantic search failed: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        if search_mode_actual == "semantic":
                            raise

                # Calculate execution time
                execution_time_ms = int((time.time() - start_time) * 1000)

                # Apply access filtering based on user's group membership (Story #707)
                if (
                    hasattr(app.state, "access_filtering_service")
                    and app.state.access_filtering_service
                ):
                    fts_dicts = [r.model_dump() for r in fts_results]
                    fts_dicts = app.state.access_filtering_service.filter_query_results(
                        fts_dicts, current_user.username
                    )
                    semantic_dicts = [r.model_dump() for r in semantic_results_list]
                    semantic_dicts = (
                        app.state.access_filtering_service.filter_query_results(
                            semantic_dicts, current_user.username
                        )
                    )
                else:
                    fts_dicts = [r.model_dump() for r in fts_results]
                    semantic_dicts = [r.model_dump() for r in semantic_results_list]

                # Apply payload truncation for consistency with MCP handlers
                _payload_cache = getattr(app.state, "payload_cache", None)
                truncated_fts = _apply_rest_fts_truncation(
                    fts_dicts, payload_cache=_payload_cache
                )
                truncated_semantic = _apply_rest_semantic_truncation(
                    semantic_dicts, payload_cache=_payload_cache
                )

                # Return as JSONResponse to support truncated fields
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    content={
                        "search_mode": search_mode_actual,
                        "query": request.query_text,
                        "fts_results": truncated_fts,
                        "semantic_results": truncated_semantic,
                        "metadata": {
                            "query_text": request.query_text,
                            "search_mode_requested": request.search_mode,
                            "search_mode_actual": search_mode_actual,
                            "execution_time_ms": execution_time_ms,
                            "fts_available": fts_available,
                            "semantic_available": True,
                            "repositories_searched": len(activated_repos),
                        },
                    }
                )

            # Default semantic mode (backward compatibility)
            results = semantic_query_manager.query_user_repositories(
                username=current_user.username,
                query_text=request.query_text,
                repository_alias=request.repository_alias,
                limit=request.limit,
                min_score=request.min_score,
                file_extensions=request.file_extensions,
                # Phase 1 parameters (Story #503)
                exclude_language=request.exclude_language,
                exclude_path=request.exclude_path,
                accuracy=request.accuracy,
                # Temporal parameters (Story #446)
                time_range=request.time_range,
                time_range_all=request.time_range_all,
                at_commit=request.at_commit,
                include_removed=request.include_removed,
                show_evolution=request.show_evolution,
                evolution_limit=request.evolution_limit,
                # Phase 3 temporal filtering parameters (Story #503)
                diff_type=request.diff_type,
                author=request.author,
                chunk_type=request.chunk_type,
            )

            # Apply access filtering based on user's group membership (Story #707)
            if (
                hasattr(app.state, "access_filtering_service")
                and app.state.access_filtering_service
            ):
                results["results"] = (
                    app.state.access_filtering_service.filter_query_results(
                        results["results"], current_user.username
                    )
                )
                results["total_results"] = len(results["results"])

            # Apply payload truncation for consistency with MCP handlers
            truncated_results = _apply_rest_semantic_truncation(
                results["results"],
                payload_cache=getattr(app.state, "payload_cache", None),
            )

            # Return as JSONResponse to support truncated fields
            from fastapi.responses import JSONResponse

            return JSONResponse(
                content={
                    "results": truncated_results,
                    "total_results": results["total_results"],
                    "query_metadata": results["query_metadata"],
                    "warning": results.get("warning"),
                }
            )

        except HTTPException:
            # Re-raise HTTP exceptions as-is
            raise

        except ValueError as e:
            # Surface validation errors from backend as HTTP 400
            logger.warning(
                format_error_log(
                    "APP-GENERAL-036",
                    f"Validation error in query: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "Invalid query parameters", "message": str(e)},
            )

        except SemanticQueryError as e:
            error_message = str(e)

            # Determine appropriate HTTP status code based on error type
            if "not found" in error_message.lower():
                status_code = status.HTTP_404_NOT_FOUND
            elif "timed out" in error_message.lower():
                status_code = status.HTTP_408_REQUEST_TIMEOUT
            elif "no activated repositories" in error_message.lower():
                status_code = status.HTTP_400_BAD_REQUEST
            else:
                status_code = status.HTTP_400_BAD_REQUEST

            raise HTTPException(
                status_code=status_code,
                detail=error_message,
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Unexpected error in unified search: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal search error: {str(e)}",
            )
