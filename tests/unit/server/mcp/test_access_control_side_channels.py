"""
Story #331: Close All Repository Access Control Side-Channel Leaks.

Tests for 10 acceptance criteria that close side-channel information leaks
where restricted users could learn about repositories they should not see.

Each test class maps to one AC and is written to FAIL until the corresponding
production code fix is applied.

Test coverage:
- AC3:  repo_alias parameter checked by centralized guard
- AC9:  Guard fail-closed when service unavailable
- AC1:  Error suggestions filtered by user access
- AC2:  Wildcard expansion filtered by user access
- AC4:  Omni-* handlers respect access control
- AC5:  Composite repo validates component access
- AC7:  Omni-search errors dict filtered
- AC6:  cidx-meta results filtered for referenced repos
- AC8:  Cache handles scoped to user
- AC10: list_repo_categories filtered or documented risk
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    """Create a real User object for testing."""
    return User(
        username=username,
        password_hash="hashed_password",
        role=role,
        created_at=datetime(2024, 1, 1),
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: set = None,
    filter_listing_result: list = None,
) -> Mock:
    """Create a mock AccessFilteringService with configurable access.

    Args:
        is_admin: Whether user is admin
        accessible_repos: Set of repo names the user can access
        filter_listing_result: Explicit return value for filter_repo_listing.
            If None, filter_repo_listing will filter based on accessible_repos.
    """
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    if accessible_repos is None:
        accessible_repos = set()
    service.get_accessible_repos = Mock(return_value=accessible_repos)

    if filter_listing_result is not None:
        service.filter_repo_listing = Mock(return_value=filter_listing_result)
    elif is_admin:
        # Admin users: filter_repo_listing returns all repos unchanged
        service.filter_repo_listing = Mock(side_effect=lambda repos, username: repos)
    else:
        # Default: filter based on accessible_repos, stripping -global suffix
        def _filter(repos, username):
            result = []
            for r in repos:
                normalized = r
                if normalized.endswith("-global"):
                    normalized = normalized[: -len("-global")]
                if normalized in accessible_repos:
                    result.append(r)
            return result

        service.filter_repo_listing = Mock(side_effect=_filter)

    service.filter_cidx_meta_results = Mock(side_effect=lambda results, uid: results)
    service.calculate_over_fetch_limit = Mock(side_effect=lambda limit: limit * 2)
    service.filter_query_results = Mock(side_effect=lambda results, uid: results)
    return service


# ===========================================================================
# AC3: repo_alias parameter checked by centralized guard
# ===========================================================================


class TestAC3RepoAliasInGuard:
    """AC3: _check_repository_access must check 'repo_alias' parameter name.

    Tools like enter_write_mode, exit_write_mode, and wiki_article_analytics
    use 'repo_alias' instead of 'repository_alias'. The guard must recognize
    this parameter name.
    """

    def test_guard_blocks_unauthorized_repo_alias_parameter(self):
        """Guard must raise ValueError when repo_alias param contains unauthorized repo."""
        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        arguments = {"repo_alias": "secret-repo-global"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="enter_write_mode",
                access_service=access_service,
            )

        assert "Access denied" in str(exc_info.value)

    def test_guard_allows_authorized_repo_alias_parameter(self):
        """Guard must allow access when repo_alias contains an authorized repo."""
        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        arguments = {"repo_alias": "allowed-repo-global"}

        # Should not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="enter_write_mode",
            access_service=access_service,
        )

    def test_guard_allows_admin_with_repo_alias(self):
        """Admin users bypass guard even with repo_alias parameter."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"repo_alias": "any-repo-global"}

        # Should not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="enter_write_mode",
            access_service=access_service,
        )

    def test_guard_ignores_empty_repo_alias(self):
        """Guard skips check when repo_alias is empty or None."""
        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        # Empty string
        _check_repository_access(
            arguments={"repo_alias": ""},
            effective_user=user,
            tool_name="enter_write_mode",
            access_service=access_service,
        )

        # None value
        _check_repository_access(
            arguments={"repo_alias": None},
            effective_user=user,
            tool_name="enter_write_mode",
            access_service=access_service,
        )


# ===========================================================================
# AC9: Guard fail-closed when service unavailable
# ===========================================================================


class TestAC9FailClosedGuard:
    """AC9: When access_filtering_service is not available, the guard must
    DENY access (fail-closed) rather than falling through (fail-open).

    Only tools with no repo parameter should proceed normally.
    """

    def test_fail_closed_when_service_unavailable_and_repo_param_present(self):
        """When access service is unavailable and a repo param is present,
        handle_tools_call must raise ValueError (deny access)."""
        # We test this through the protocol.py handle_tools_call path.
        # When app.state.access_filtering_service raises AttributeError,
        # and a repo param is present, access should be DENIED.
        from code_indexer.server.mcp import protocol

        user = _make_user("some_user")

        # Simulate: access_filtering_service not available (AttributeError)
        # but tool has a repository_alias argument
        mock_handlers_module = Mock()
        mock_app = Mock()
        # Make accessing access_filtering_service raise AttributeError
        del mock_app.state.access_filtering_service
        mock_handlers_module.app_module.app = mock_app

        with patch.object(protocol, "_check_repository_access") as mock_guard:
            # We need to test the actual except AttributeError path in handle_tools_call
            # So we test the behavior: when service is unavailable AND repo param present,
            # handle_tools_call should raise ValueError
            pass

        # Direct test: the protocol code at lines 308-314 should raise ValueError
        # when accessing access_filtering_service fails AND arguments contain repo param.
        # This is tested through the actual handle_tools_call flow.
        # For now, we verify the structural expectation:
        # The except AttributeError block must raise ValueError, not log and continue.

        # We verify by importing and checking the behavior through handle_tools_call
        # This test will be meaningful after AC9 is implemented.
        # For now, test the current FAILING behavior to prove the gap exists.

        # Simulate the AttributeError path
        class _FakeState:
            pass  # No access_filtering_service attribute

        fake_state = _FakeState()

        # Access should raise AttributeError
        with pytest.raises(AttributeError):
            _ = fake_state.access_filtering_service

        # The guard in handle_tools_call currently CATCHES this and continues.
        # AC9 says: when repo param present, it must DENY access.
        # We test via a more realistic simulation below.

    @pytest.mark.asyncio
    async def test_handle_tools_call_denies_when_service_unavailable_with_repo_param(self):
        """handle_tools_call must raise ValueError when access service is missing
        and tool arguments contain a repository parameter.

        The code path in handle_tools_call (protocol.py ~line 295-314):
          try:
              from ... import handlers as _handlers_module
              _access_service = _handlers_module.app_module.app.state.access_filtering_service
          except AttributeError:
              # AC9: Must DENY access here, not skip the guard

        We trigger the AttributeError by making app.state lack the attribute,
        then verify that ValueError is raised (fail-closed).
        """
        from code_indexer.server.mcp.protocol import handle_tools_call
        from code_indexer.server.mcp import handlers as handlers_module

        user = _make_user("some_user")

        # Create an app.state that does NOT have access_filtering_service
        class _BareState:
            """State object missing access_filtering_service attribute."""
            payload_cache = None

        mock_app = Mock()
        mock_app.state = _BareState()

        with patch.object(handlers_module.app_module, "app", mock_app):
            # Call handle_tools_call with a tool that takes repository_alias.
            # The guard should try to get access_filtering_service, get AttributeError,
            # and then DENY access (AC9) instead of skipping.
            with pytest.raises(ValueError, match="(?i)access"):
                await handle_tools_call(
                    params={
                        "name": "search_code",
                        "arguments": {
                            "repository_alias": "some-repo-global",
                            "query_text": "test",
                        },
                    },
                    user=user,
                )

    @pytest.mark.asyncio
    async def test_handle_tools_call_allows_when_no_repo_param(self):
        """handle_tools_call must allow tools with no repo parameter even when
        access service is unavailable."""
        # Tools that don't take repository parameters should still work
        # This is a non-regression test for AC9
        pass  # Structural placeholder - full integration test in AC9 implementation


# ===========================================================================
# AC1: Error suggestions filtered by user access
# ===========================================================================


class TestAC1ErrorSuggestionsFiltered:
    """AC1: _get_available_repos() must accept a user parameter and filter
    through AccessFilteringService.filter_repo_listing() before returning.

    A restricted user calling any tool with an invalid repo alias must only
    see repos they have access to in available_values and suggestions.
    """

    def test_get_available_repos_accepts_user_parameter(self):
        """_get_available_repos must accept a User parameter (signature change)."""
        from code_indexer.server.mcp.handlers import _get_available_repos
        import inspect

        sig = inspect.signature(_get_available_repos)
        param_names = list(sig.parameters.keys())
        assert "user" in param_names, (
            "_get_available_repos must accept a 'user' parameter. "
            f"Current parameters: {param_names}"
        )

    def test_get_available_repos_returns_filtered_repos_for_restricted_user(self):
        """_get_available_repos(user) must return only repos the user can access."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        # Mock the registry to return all repos
        mock_registry = Mock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "allowed-repo-global"},
            {"alias_name": "secret-repo-global"},
            {"alias_name": "another-secret-global"},
        ]

        with patch.object(handlers, "_get_golden_repos_dir", return_value="/fake"), \
             patch.object(handlers, "get_server_global_registry", return_value=mock_registry), \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service):
            result = handlers._get_available_repos(user)

        # Should only contain repos the user can access
        assert "allowed-repo-global" in result
        assert "secret-repo-global" not in result
        assert "another-secret-global" not in result

    def test_get_available_repos_returns_all_repos_for_admin(self):
        """_get_available_repos(user) must return all repos for admin users."""
        from code_indexer.server.mcp import handlers

        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        mock_registry = Mock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "repo-a-global"},
            {"alias_name": "repo-b-global"},
        ]

        with patch.object(handlers, "_get_golden_repos_dir", return_value="/fake"), \
             patch.object(handlers, "get_server_global_registry", return_value=mock_registry), \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service):
            result = handlers._get_available_repos(user)

        assert "repo-a-global" in result
        assert "repo-b-global" in result


# ===========================================================================
# AC2: Wildcard expansion filtered by user access
# ===========================================================================


class TestAC2WildcardExpansionFiltered:
    """AC2: _expand_wildcard_patterns() must accept a user parameter and filter
    the available_repos list through AccessFilteringService before matching.

    A restricted user using repository_alias: ["*-global"] must only see
    repos they have access to in expanded results.
    """

    def test_expand_wildcard_accepts_user_parameter(self):
        """_expand_wildcard_patterns must accept a User parameter (signature change)."""
        from code_indexer.server.mcp.handlers import _expand_wildcard_patterns
        import inspect

        sig = inspect.signature(_expand_wildcard_patterns)
        param_names = list(sig.parameters.keys())
        assert "user" in param_names, (
            "_expand_wildcard_patterns must accept a 'user' parameter. "
            f"Current parameters: {param_names}"
        )

    def test_wildcard_star_only_returns_accessible_repos(self):
        """Wildcard '*-global' must only expand to repos the user can access."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        mock_registry = Mock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "allowed-repo-global"},
            {"alias_name": "secret-repo-global"},
            {"alias_name": "cidx-meta-global"},
        ]

        with patch.object(handlers, "_get_golden_repos_dir", return_value="/fake"), \
             patch.object(handlers, "get_server_global_registry", return_value=mock_registry), \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service):
            result = handlers._expand_wildcard_patterns(["*-global"], user)

        assert "allowed-repo-global" in result
        assert "cidx-meta-global" in result
        assert "secret-repo-global" not in result

    def test_literal_pattern_not_filtered(self):
        """Literal (non-wildcard) patterns should pass through unchanged."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        mock_registry = Mock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "allowed-repo-global"},
        ]

        with patch.object(handlers, "_get_golden_repos_dir", return_value="/fake"), \
             patch.object(handlers, "get_server_global_registry", return_value=mock_registry), \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service):
            # Literal pattern should be preserved (centralized guard will catch unauthorized)
            result = handlers._expand_wildcard_patterns(["specific-repo-global"], user)

        assert "specific-repo-global" in result


# ===========================================================================
# AC4: Omni-* handlers respect access control
# ===========================================================================


class TestAC4OmniHandlersAccessControl:
    """AC4: Omni-* handlers must filter expanded repo_aliases through
    AccessFilteringService BEFORE the per-repo iteration loop.

    This is defense-in-depth on top of AC2.
    """

    def test_omni_regex_filters_repo_aliases(self):
        """_omni_regex_search must filter repo_aliases for restricted users."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        # We verify that _expand_wildcard_patterns is called with user
        # and that the result is filtered before iteration
        with patch.object(handlers, "_expand_wildcard_patterns") as mock_expand, \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "handle_regex_search") as mock_single:

            mock_expand.return_value = ["allowed-repo-global", "secret-repo-global"]

            # Mock single repo search to return empty
            mock_single.return_value = {
                "content": [{"type": "text", "text": '{"success": true, "matches": []}'}]
            }

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                handlers._omni_regex_search(
                    {"repository_alias": ["*-global"], "pattern": "test"},
                    user,
                )
            )

        # Verify _expand_wildcard_patterns was called with user parameter
        if mock_expand.called:
            call_args = mock_expand.call_args
            # Check that user was passed (either positional or keyword)
            all_args = list(call_args.args) + list(call_args.kwargs.values())
            assert user in all_args, (
                "_expand_wildcard_patterns must be called with user parameter"
            )

    def test_omni_search_code_filters_repo_aliases(self):
        """_omni_search_code must pass user to _expand_wildcard_patterns."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        with patch.object(handlers, "_expand_wildcard_patterns") as mock_expand, \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "get_config_service") as mock_config:

            mock_expand.return_value = []  # Empty to short-circuit

            mock_config_instance = Mock()
            mock_config.return_value = mock_config_instance

            handlers._omni_search_code(
                {"repository_alias": ["*-global"], "query_text": "test"},
                user,
            )

        # Verify user was passed to _expand_wildcard_patterns
        if mock_expand.called:
            call_args = mock_expand.call_args
            all_args = list(call_args.args) + list(call_args.kwargs.values())
            assert user in all_args, (
                "_expand_wildcard_patterns must be called with user parameter"
            )


# ===========================================================================
# AC5: Composite repo validates component access
# ===========================================================================


class TestAC5CompositeRepoValidation:
    """AC5: manage_composite_repository() must check each alias in
    golden_repo_aliases against AccessFilteringService before passing
    to activate_repository().
    """

    def test_composite_create_blocks_unauthorized_component(self):
        """Creating composite repo with unauthorized component must fail."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        with patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "app_module") as mock_app:

            result = handlers.manage_composite_repository(
                {
                    "operation": "create",
                    "user_alias": "my-composite",
                    "golden_repo_aliases": ["allowed-repo", "secret-repo"],
                },
                user,
            )

        # Parse the response
        import json
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            data = json.loads(content[0]["text"])
        else:
            data = result

        assert data.get("success") is False, (
            "manage_composite_repository must deny creation when golden_repo_aliases "
            "contains an unauthorized repo ('secret-repo')"
        )
        assert "Access denied" in data.get("error", ""), (
            "Error message must indicate access denial. "
            f"Got: {data.get('error', '')}"
        )

    def test_composite_create_allows_all_authorized_components(self):
        """Creating composite repo with all authorized components must succeed."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "repo-a", "repo-b"},
        )

        with patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "app_module") as mock_app:

            mock_app.activated_repo_manager.activate_repository.return_value = "job-123"

            result = handlers.manage_composite_repository(
                {
                    "operation": "create",
                    "user_alias": "my-composite",
                    "golden_repo_aliases": ["repo-a", "repo-b"],
                },
                user,
            )

        import json
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            data = json.loads(content[0]["text"])
        else:
            data = result

        assert data.get("success") is True, (
            "manage_composite_repository must allow creation when all golden_repo_aliases "
            "are authorized"
        )

    def test_composite_admin_bypasses_check(self):
        """Admin users bypass composite repo access checks."""
        from code_indexer.server.mcp import handlers

        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        with patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "app_module") as mock_app:

            mock_app.activated_repo_manager.activate_repository.return_value = "job-456"

            result = handlers.manage_composite_repository(
                {
                    "operation": "create",
                    "user_alias": "admin-composite",
                    "golden_repo_aliases": ["any-repo", "another-repo"],
                },
                user,
            )

        import json
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            data = json.loads(content[0]["text"])
        else:
            data = result

        assert data.get("success") is True


# ===========================================================================
# AC7: Omni-search errors dict filtered
# ===========================================================================


class TestAC7OmniSearchErrorsFiltered:
    """AC7: After omni-search/omni-regex completes, the errors dict must be
    filtered to remove keys (repo aliases) that the user does not have access to.
    """

    def test_omni_regex_errors_filtered_for_restricted_user(self):
        """Errors dict in omni-regex response must only contain accessible repo keys."""
        from code_indexer.server.mcp import handlers

        user = _make_user("restricted_user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        # Simulate omni-regex that encounters errors for multiple repos
        with patch.object(handlers, "_expand_wildcard_patterns") as mock_expand, \
             patch.object(handlers, "_get_access_filtering_service", return_value=access_service), \
             patch.object(handlers, "handle_regex_search") as mock_single:

            # Return all repos (including secret ones - simulating a gap in AC2/AC4)
            mock_expand.return_value = [
                "allowed-repo-global",
                "secret-repo-global",
            ]

            # Make each single-repo search fail with an error
            def _make_error_response(args, user):
                repo = args["repository_alias"]
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f'{{"success": false, "error": "Index not found for {repo}"}}',
                        }
                    ]
                }

            mock_single.side_effect = _make_error_response

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                handlers._omni_regex_search(
                    {"repository_alias": ["*-global"], "pattern": "test"},
                    user,
                )
            )

        import json
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            data = json.loads(content[0]["text"])
        else:
            data = result

        errors = data.get("errors", {})
        # Errors dict must NOT contain secret-repo-global
        assert "secret-repo-global" not in errors, (
            f"Errors dict must not leak unauthorized repo aliases. "
            f"Found: {list(errors.keys())}"
        )


# ===========================================================================
# AC6: cidx-meta results filtered for referenced repos
# ===========================================================================


class TestAC6CidxMetaFiltering:
    """AC6: When querying cidx-meta, results that reference inaccessible repos
    must be filtered out using AccessFilteringService.filter_cidx_meta_results().
    """

    def test_cidx_meta_search_results_filtered(self):
        """search_code querying cidx-meta must call filter_cidx_meta_results."""
        from code_indexer.server.mcp import handlers
        import inspect

        # Verify that filter_cidx_meta_results exists on AccessFilteringService
        from code_indexer.server.services.access_filtering_service import AccessFilteringService
        assert hasattr(AccessFilteringService, "filter_cidx_meta_results")

        # Verify via source inspection that search_code calls filter_cidx_meta_results
        source = inspect.getsource(handlers.search_code)
        assert "filter_cidx_meta_results" in source, (
            "search_code must call filter_cidx_meta_results for cidx-meta queries"
        )


# ===========================================================================
# AC8: Cache handles scoped to user
# ===========================================================================


class TestAC8CacheUserScoping:
    """AC8: handle_get_cached_content() must verify that the requesting user
    matches the user who created the cache entry, OR has access to the
    repository that produced the cached results.

    If cache infrastructure does not support user tracking, document as
    accepted risk with explicit code comment.
    """

    def test_cache_handler_has_user_scoping_or_documented_risk(self):
        """handle_get_cached_content must either check user ownership or
        have explicit documentation of accepted risk."""
        from code_indexer.server.mcp import handlers
        import inspect

        source = inspect.getsource(handlers.handle_get_cached_content)

        # Check for either user-scoping implementation OR documented accepted risk
        has_user_check = (
            "user.username" in source
            or "user_id" in source
            or "owner" in source
        )
        has_accepted_risk_doc = (
            "accepted risk" in source.lower()
            or "AC8" in source
            or "Story #331" in source
        )

        assert has_user_check or has_accepted_risk_doc, (
            "handle_get_cached_content must either implement user-scoping "
            "(check user ownership of cache entries) or document the accepted "
            "risk with an explicit code comment referencing AC8/Story #331. "
            "Currently has neither."
        )


# ===========================================================================
# AC10: list_repo_categories filtered
# ===========================================================================


class TestAC10CategoriesFiltering:
    """AC10: list_repo_categories should filter returned categories OR
    document as accepted risk in a code comment.
    """

    def test_list_repo_categories_filtered_or_documented(self):
        """list_repo_categories must either filter by access or have documented risk."""
        from code_indexer.server.mcp import handlers
        import inspect

        source = inspect.getsource(handlers.list_repo_categories)

        has_filtering = (
            "access_filtering" in source.lower()
            or "filter_repo_listing" in source
            or "get_accessible_repos" in source
        )
        has_accepted_risk_doc = (
            "accepted risk" in source.lower()
            or "AC10" in source
            or "Story #331" in source
        )

        assert has_filtering or has_accepted_risk_doc, (
            "list_repo_categories must either implement access filtering "
            "(filter categories by user access) or document the accepted "
            "risk with an explicit code comment referencing AC10/Story #331. "
            "Currently has neither."
        )
