"""
Unit tests for Bug #862: Batch-create registration failures produce no server
logs and no UI error detail.

Acceptance criteria tested with behavioral precision:
1. _sanitize_batch_create_error() trims to first 197 chars + "..." for >200 char messages
2. _sanitize_batch_create_error() replaces newlines with spaces (exact output asserted)
3. MAX_BATCH_CREATE_REPOS = 50 cap: >50 returns HTTP 400; exactly 50 invokes manager
4. _batch_create_repos() exception causes logger.warning with exc_info=True and
   WEB-GENERAL-067 in message
5. WEB-GENERAL-067 added to error_codes registry with WARNING severity
6. Template: #batch-create-status div exists inside #batch-create-modal
7. Template: removeSuccessfulSelections — single unified regex proves success guard
   structurally precedes selectedRepos.delete(); renderBatchCreateFailures uses
   .filter( with status and 'failed' in the same callback expression
8. Template: closeBatchModal — helper extracts variable name from getElementById
   assignment, then verifies .style.display='none' and .innerHTML='' on that variable
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Template helpers (same pattern as test_autodiscovery_ssh_url_fix.py)
# ---------------------------------------------------------------------------

# Ordered list of all known top-level JS function names in auto_discovery.html.
# Includes new Bug #862 helpers before closeBatchModal.
_KNOWN_FUNCTIONS = [
    "preferredCloneUrl",
    "switchPlatform",
    "fetchAll",
    "enrichVisible",
    "patchEnrichmentRows",
    "getFilteredSorted",
    "renderPanel",
    "sortHeader",
    "renderPagination",
    "onSearch",
    "onShowHidden",
    "onSort",
    "goPage",
    "onCheckboxChange",
    "toggleSelectAll",
    "updateSelectionUI",
    "clearSelection",
    "showCreateDialog",
    "fetchBranchesForSelectedRepos",
    "updateRepoBranchDropdown",
    "onBranchChange",
    "removeSuccessfulSelections",
    "renderBatchCreateFailures",
    "closeBatchModal",
    "executeBatchCreate",
    "doHide",
    "doUnhide",
    "getCsrfToken",
    "escHtml",
    "sizeTableContainer",
]


def _read_template() -> str:
    """Read the auto_discovery.html template content."""
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "auto_discovery.html"
    )
    return template_path.read_text()


def _function_slice(html: str, function_name: str) -> str:
    """Return the text slice from a JS function declaration to the next known sibling."""
    marker = "function " + function_name + "("
    start = html.find(marker)
    if start == -1:
        return ""

    try:
        func_idx = _KNOWN_FUNCTIONS.index(function_name)
    except ValueError:
        return html[start:]

    end = len(html)
    for sibling in _KNOWN_FUNCTIONS[func_idx + 1 :]:
        sibling_marker = "function " + sibling + "("
        pos = html.find(sibling_marker, start + 1)
        if pos != -1:
            end = pos
            break

    return html[start:end]


# ---------------------------------------------------------------------------
# Template assertion helpers — eliminate repeated _function_slice boilerplate
# ---------------------------------------------------------------------------


def _assert_template_contains(
    function_name: str, pattern: re.Pattern, msg: str
) -> None:
    """Assert that the named JS function body matches the given compiled regex.

    Raises AssertionError with `msg` if the function is not found or pattern
    does not match.
    """
    body = _function_slice(_read_template(), function_name)
    assert body, f"{function_name} function must exist in auto_discovery.html"
    assert pattern.search(body), msg


def _assert_template_not_contains(
    function_name: str, pattern: re.Pattern, msg: str
) -> None:
    """Assert that the named JS function body does NOT match the given compiled regex.

    Raises AssertionError with `msg` if the function is not found or pattern matches.
    """
    body = _function_slice(_read_template(), function_name)
    assert body, f"{function_name} function must exist in auto_discovery.html"
    assert not pattern.search(body), msg


# Compiled pattern: var/let/const <name> = document.getElementById('batch-create-status')
_GEBI_ASSIGN_PATTERN = re.compile(
    r"(?:var|let|const)\s+(\w+)\s*=\s*document\.getElementById\(['\"]batch-create-status['\"]\)"
)


def _get_status_var_name(function_body: str) -> str:
    """Extract the JS variable name assigned from getElementById('batch-create-status').

    Returns the variable name string, or raises AssertionError if not found.
    Used by closeBatchModal tests to avoid duplicating the extraction regex.
    """
    match = _GEBI_ASSIGN_PATTERN.search(function_body)
    assert match, (
        "closeBatchModal must assign document.getElementById('batch-create-status') "
        "to a variable (var/let/const <name> = document.getElementById(...))"
    )
    return match.group(1)


# ---------------------------------------------------------------------------
# AC1 + AC2: _sanitize_batch_create_error helper — exact output assertions
# ---------------------------------------------------------------------------


class TestSanitizeBatchCreateError:
    """AC1/AC2: _sanitize_batch_create_error trims to <=200 chars and replaces newlines."""

    def test_truncates_long_error_message_preserves_first_197_chars(self):
        """Error messages > 200 chars are trimmed: first 197 chars kept + '...' appended."""
        from src.code_indexer.server.web.routes import _sanitize_batch_create_error

        long_msg = "x" * 300
        result = _sanitize_batch_create_error(Exception(long_msg))

        assert result == "x" * 197 + "...", (
            "Truncated result must be exactly the first 197 chars plus '...'"
        )

    def test_short_message_unchanged(self):
        """Error messages <= 200 chars are returned stripped and unmodified."""
        from src.code_indexer.server.web.routes import _sanitize_batch_create_error

        result = _sanitize_batch_create_error(Exception("short error"))

        assert result == "short error"

    def test_newlines_replaced_with_spaces_exact_output(self):
        """Newlines are replaced with spaces; other text is preserved exactly."""
        from src.code_indexer.server.web.routes import _sanitize_batch_create_error

        result = _sanitize_batch_create_error(Exception("line1\nline2\nline3"))

        assert result == "line1 line2 line3", (
            "Newlines must be replaced with spaces, not deleted; "
            "expected 'line1 line2 line3'"
        )

    def test_exactly_200_chars_not_truncated(self):
        """Error message of exactly 200 chars is returned without truncation."""
        from src.code_indexer.server.web.routes import _sanitize_batch_create_error

        msg = "a" * 200
        result = _sanitize_batch_create_error(Exception(msg))

        assert result == "a" * 200, "A 200-char message must not be truncated"

    def test_201_chars_truncated_to_first_197_plus_ellipsis(self):
        """Error message of 201 chars is trimmed to first 197 chars + '...'."""
        from src.code_indexer.server.web.routes import _sanitize_batch_create_error

        msg = "b" * 201
        result = _sanitize_batch_create_error(Exception(msg))

        assert result == "b" * 197 + "...", (
            "A 201-char message must become first 197 chars + '...'"
        )


# ---------------------------------------------------------------------------
# AC3: MAX_BATCH_CREATE_REPOS cap returns HTTP 400
# ---------------------------------------------------------------------------


class TestMaxBatchCreateReposCap:
    """AC3: Sending more than MAX_BATCH_CREATE_REPOS repos returns HTTP 400."""

    def test_batch_create_returns_400_when_over_limit(self):
        """batch_create_golden_repos returns 400 when repo count exceeds cap."""
        import json
        from src.code_indexer.server.web.routes import batch_create_golden_repos
        from src.code_indexer.server.web.routes import MAX_BATCH_CREATE_REPOS

        over_limit = [
            {"clone_url": f"https://github.com/org/repo{i}", "alias": f"repo{i}"}
            for i in range(MAX_BATCH_CREATE_REPOS + 1)
        ]

        mock_request = MagicMock()
        mock_request.cookies = {}

        mock_session = MagicMock()
        mock_session.username = "admin"

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
        ):
            result = batch_create_golden_repos(
                request=mock_request,
                repos=json.dumps(over_limit),
                csrf_token="dummy",
            )

        assert result.status_code == 400
        body = json.loads(result.body)
        assert body["success"] is False
        assert str(MAX_BATCH_CREATE_REPOS) in body["error"]

    def test_max_batch_create_repos_constant_is_50(self):
        """MAX_BATCH_CREATE_REPOS constant must be defined as 50."""
        from src.code_indexer.server.web.routes import MAX_BATCH_CREATE_REPOS

        assert MAX_BATCH_CREATE_REPOS == 50

    def test_batch_create_at_limit_invokes_manager_for_all_repos(self):
        """Exactly MAX_BATCH_CREATE_REPOS repos does NOT trigger the 400 cap.

        Proves cap is not triggered by verifying add_golden_repo is called
        exactly MAX_BATCH_CREATE_REPOS times and HTTP 200 is returned.
        """
        import json
        from src.code_indexer.server.web.routes import batch_create_golden_repos
        from src.code_indexer.server.web.routes import MAX_BATCH_CREATE_REPOS

        at_limit = [
            {"clone_url": f"https://github.com/org/repo{i}", "alias": f"repo{i}"}
            for i in range(MAX_BATCH_CREATE_REPOS)
        ]

        mock_request = MagicMock()
        mock_request.cookies = {}

        mock_session = MagicMock()
        mock_session.username = "admin"

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.return_value = "job-id-001"

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            result = batch_create_golden_repos(
                request=mock_request,
                repos=json.dumps(at_limit),
                csrf_token="dummy",
            )

        assert mock_manager.add_golden_repo.call_count == MAX_BATCH_CREATE_REPOS, (
            f"add_golden_repo must be called {MAX_BATCH_CREATE_REPOS} times "
            f"when submitting exactly {MAX_BATCH_CREATE_REPOS} repos"
        )
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# AC4: logger.warning with exc_info=True on per-repo exception
# ---------------------------------------------------------------------------


class TestBatchCreateReposLogging:
    """AC4: _batch_create_repos calls logger.warning with exc_info=True on exception."""

    def test_exception_triggers_logger_warning_with_exc_info(self):
        """When add_golden_repo raises, logger.warning is called with exc_info=True."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = RuntimeError("DB connection lost")

        repos = [{"clone_url": "https://github.com/org/repo", "alias": "repo"}]

        with patch("src.code_indexer.server.web.routes.logger") as mock_logger:
            _batch_create_repos(repos, "admin", mock_manager)

        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args[1]
        assert call_kwargs.get("exc_info") is True, (
            "logger.warning must be called with exc_info=True for stack trace capture"
        )

    def test_exception_includes_error_code_web_general_067(self):
        """logger.warning message must include WEB-GENERAL-067 error code."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = ValueError("Invalid URL")

        repos = [{"clone_url": "https://github.com/org/repo", "alias": "my-repo"}]

        with patch("src.code_indexer.server.web.routes.logger") as mock_logger:
            _batch_create_repos(repos, "admin", mock_manager)

        call_args = mock_logger.warning.call_args[0]
        assert len(call_args) > 0, (
            "logger.warning must receive a positional message argument"
        )
        assert "WEB-GENERAL-067" in call_args[0], (
            "Warning message must contain error code WEB-GENERAL-067"
        )

    def test_failed_repo_still_appended_to_results(self):
        """Even when exception is raised, the failed repo is added to results."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = RuntimeError("Network error")

        repos = [{"clone_url": "https://github.com/org/repo", "alias": "my-repo"}]

        with patch("src.code_indexer.server.web.routes.logger"):
            result = _batch_create_repos(repos, "admin", mock_manager)

        assert result["success"] is False
        assert len(result["results"]) == 1
        assert result["results"][0]["status"] == "failed"
        assert result["results"][0]["alias"] == "my-repo"

    def test_error_message_is_sanitized_in_result(self):
        """The error field in results uses _sanitize_batch_create_error (no raw newlines)."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = RuntimeError("Error\nwith\nnewlines")

        repos = [{"clone_url": "https://github.com/org/repo", "alias": "my-repo"}]

        with patch("src.code_indexer.server.web.routes.logger"):
            result = _batch_create_repos(repos, "admin", mock_manager)

        error_msg = result["results"][0]["error"]
        assert "\n" not in error_msg, (
            "Error message in result must not contain newlines"
        )


# ---------------------------------------------------------------------------
# AC5: WEB-GENERAL-067 in error_codes registry
# ---------------------------------------------------------------------------


class TestWebGeneral067ErrorCode:
    """AC5: WEB-GENERAL-067 must exist in the error_codes registry."""

    def test_web_general_067_exists_in_registry(self):
        """WEB-GENERAL-067 must be present in ERROR_REGISTRY."""
        from code_indexer.server.error_codes import ERROR_REGISTRY

        assert "WEB-GENERAL-067" in ERROR_REGISTRY, (
            "WEB-GENERAL-067 must be added to ERROR_REGISTRY in error_codes.py"
        )

    def test_web_general_067_severity_is_warning(self):
        """WEB-GENERAL-067 must have WARNING severity."""
        from code_indexer.server.error_codes import ERROR_REGISTRY, Severity

        entry = ERROR_REGISTRY["WEB-GENERAL-067"]
        assert entry.severity == Severity.WARNING, (
            "WEB-GENERAL-067 must have WARNING severity"
        )

    def test_web_general_067_code_field_matches_key(self):
        """WEB-GENERAL-067 ErrorDefinition.code field must match the registry key."""
        from code_indexer.server.error_codes import ERROR_REGISTRY

        entry = ERROR_REGISTRY["WEB-GENERAL-067"]
        assert entry.code == "WEB-GENERAL-067"


# ---------------------------------------------------------------------------
# AC6: Template - #batch-create-status div in modal
# ---------------------------------------------------------------------------


class TestBatchCreateStatusBanner:
    """AC6: #batch-create-status error banner exists inside #batch-create-modal."""

    def test_batch_create_status_div_exists(self):
        """#batch-create-status element must be defined in auto_discovery.html."""
        html = _read_template()
        assert 'id="batch-create-status"' in html, (
            "auto_discovery.html must contain an element with id='batch-create-status'"
        )

    def test_batch_create_status_inside_modal(self):
        """#batch-create-status must appear inside the #batch-create-modal dialog."""
        html = _read_template()
        modal_start = html.find('id="batch-create-modal"')
        assert modal_start != -1, "#batch-create-modal must exist"

        modal_end = html.find("</dialog>", modal_start)
        assert modal_end != -1, "batch-create-modal dialog must be closed"

        modal_section = html[modal_start:modal_end]
        assert 'id="batch-create-status"' in modal_section, (
            "#batch-create-status must be inside the #batch-create-modal dialog"
        )


# ---------------------------------------------------------------------------
# AC7: Template - removeSuccessfulSelections and renderBatchCreateFailures defined
# ---------------------------------------------------------------------------


class TestBatchCreateHelperFunctions:
    """AC7: removeSuccessfulSelections and renderBatchCreateFailures must be defined."""

    def test_remove_successful_selections_function_defined(self):
        """removeSuccessfulSelections function must be defined in auto_discovery.html."""
        html = _read_template()
        assert "function removeSuccessfulSelections(" in html, (
            "auto_discovery.html must define removeSuccessfulSelections() function"
        )

    def test_render_batch_create_failures_function_defined(self):
        """renderBatchCreateFailures function must be defined in auto_discovery.html."""
        html = _read_template()
        assert "function renderBatchCreateFailures(" in html, (
            "auto_discovery.html must define renderBatchCreateFailures() function"
        )

    def test_remove_successful_selections_success_guard_unified_with_delete(self):
        """Single unified regex proves success guard and selectedRepos.delete are linked.

        The regex matches: a success-status comparison ([!=]== 'success') followed
        within 500 chars by selectedRepos.delete(, proving they are part of the same
        conditional block rather than unrelated occurrences elsewhere in the function.
        The 500-char window accommodates nested forEach patterns between guard and delete.
        """
        body = _function_slice(_read_template(), "removeSuccessfulSelections")
        assert body, "removeSuccessfulSelections function must exist"

        # One unified pattern: status guard followed (within 500 chars) by delete
        # 500 chars accommodates nested forEach structures between the guard and delete call
        unified_pattern = re.compile(
            r"[!=]==\s*['\"]success['\"]\s*.{0,500}selectedRepos\.delete\(",
            re.DOTALL,
        )
        assert unified_pattern.search(body), (
            "removeSuccessfulSelections must contain a unified block where a "
            "status [!=]== 'success' guard is followed within 500 chars by "
            "selectedRepos.delete( — proving the delete is inside the success path"
        )

    def test_render_batch_create_failures_filter_contains_status_and_failed(self):
        """renderBatchCreateFailures .filter( callback must reference status and 'failed'.

        The regex matches .filter(function(...) { <body> }) and requires that
        status === 'failed' appears inside the callback body (between { and }),
        proving the condition is the filter predicate itself and not somewhere
        else in the function body.
        """
        body = _function_slice(_read_template(), "renderBatchCreateFailures")
        assert body, "renderBatchCreateFailures function must exist"

        # Pattern: .filter(function(...) { <body containing status === 'failed'> })
        # Bounded by the closing }) to prove the check is inside the filter predicate.
        filter_pattern = re.compile(
            r"\.filter\(function\([^)]*\)\s*\{[^}]*status[^}]*===\s*['\"]failed['\"][^}]*\}\s*\)",
            re.DOTALL,
        )
        assert filter_pattern.search(body), (
            "renderBatchCreateFailures must use .filter(function(...) { ... }) "
            "with status === 'failed' inside the callback body, bounded by the closing })"
        )

    def test_render_batch_create_failures_targets_status_element(self):
        """renderBatchCreateFailures must reference batch-create-status element."""
        body = _function_slice(_read_template(), "renderBatchCreateFailures")
        assert body, "renderBatchCreateFailures function must exist"
        assert "batch-create-status" in body, (
            "renderBatchCreateFailures must reference the #batch-create-status element"
        )


# ---------------------------------------------------------------------------
# AC8: Template - closeBatchModal clears the error banner
# ---------------------------------------------------------------------------


class TestCloseBatchModalClearsBanner:
    """AC8: closeBatchModal must select #batch-create-status via getElementById,
    assign the result to a variable, then reset .style.display and .innerHTML on it."""

    def test_close_batch_modal_assigns_status_element_from_get_element_by_id(self):
        """closeBatchModal must assign getElementById('batch-create-status') to a variable."""
        body = _function_slice(_read_template(), "closeBatchModal")
        assert body, "closeBatchModal function must exist"
        # _get_status_var_name raises AssertionError if pattern not found
        _get_status_var_name(body)

    def test_close_batch_modal_resets_display_none_on_assigned_variable(self):
        """closeBatchModal must set .style.display = 'none' on the assigned status variable."""
        body = _function_slice(_read_template(), "closeBatchModal")
        assert body, "closeBatchModal function must exist"

        var_name = _get_status_var_name(body)
        display_pattern = re.compile(
            r"\b" + re.escape(var_name) + r"\b.*?\.style\.display\s*=\s*['\"]none['\"]",
            re.DOTALL,
        )
        assert display_pattern.search(body), (
            f"closeBatchModal must set {var_name}.style.display = 'none' after "
            "assigning the batch-create-status element"
        )

    def test_close_batch_modal_clears_inner_html_on_assigned_variable(self):
        """closeBatchModal must set .innerHTML = '' on the assigned status variable."""
        body = _function_slice(_read_template(), "closeBatchModal")
        assert body, "closeBatchModal function must exist"

        var_name = _get_status_var_name(body)
        inner_html_pattern = re.compile(
            r"\b" + re.escape(var_name) + r"\b.*?\.innerHTML\s*=\s*['\"]['\"]",
            re.DOTALL,
        )
        assert inner_html_pattern.search(body), (
            f"closeBatchModal must set {var_name}.innerHTML = '' after "
            "assigning the batch-create-status element"
        )


# ---------------------------------------------------------------------------
# Finding #1 (routes.py): _batch_create_repos must include clone_url in results
# ---------------------------------------------------------------------------


class TestBatchCreateResultsIncludeCloneUrl:
    """Finding #1 fix: _batch_create_repos must include clone_url in both success
    and failure result dicts so the frontend can match by clone_url."""

    def test_success_result_includes_clone_url(self):
        """Success result dict must contain clone_url field matching repo_data clone_url."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.return_value = "job-id-001"

        repos = [{"clone_url": "https://github.com/org/repo-a", "alias": "repo-a"}]

        with patch("src.code_indexer.server.web.routes.logger"):
            result = _batch_create_repos(repos, "admin", mock_manager)

        assert result["results"][0]["status"] == "success"
        assert "clone_url" in result["results"][0], (
            "Success result must include clone_url field"
        )
        assert result["results"][0]["clone_url"] == "https://github.com/org/repo-a"

    def test_failure_result_includes_clone_url(self):
        """Failure result dict must contain clone_url field matching repo_data clone_url."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = RuntimeError("Network error")

        repos = [{"clone_url": "https://github.com/org/repo-b", "alias": "repo-b"}]

        with patch("src.code_indexer.server.web.routes.logger"):
            result = _batch_create_repos(repos, "admin", mock_manager)

        assert result["results"][0]["status"] == "failed"
        assert "clone_url" in result["results"][0], (
            "Failure result must include clone_url field"
        )
        assert result["results"][0]["clone_url"] == "https://github.com/org/repo-b"

    def test_partial_success_both_results_include_clone_url(self):
        """When one repo succeeds and one fails, both results include clone_url."""
        from src.code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.side_effect = [
            "job-id-001",
            RuntimeError("Failed"),
        ]

        repos = [
            {"clone_url": "https://github.com/org/repo-a", "alias": "repo-a"},
            {"clone_url": "https://github.com/org/repo-b", "alias": "repo-b"},
        ]

        with patch("src.code_indexer.server.web.routes.logger"):
            result = _batch_create_repos(repos, "admin", mock_manager)

        assert result["results"][0]["status"] == "success"
        assert result["results"][0]["clone_url"] == "https://github.com/org/repo-a"
        assert result["results"][1]["status"] == "failed"
        assert result["results"][1]["clone_url"] == "https://github.com/org/repo-b"


# ---------------------------------------------------------------------------
# Finding #1 (template): removeSuccessfulSelections partial-success regression
# ---------------------------------------------------------------------------

_CLONE_URL_MATCH_PATTERN = re.compile(
    r"data\.clone_url\s*===\s*item\.clone_url"
    r"|item\.clone_url\s*===\s*data\.clone_url",
    re.DOTALL,
)
_BUGGY_ENDS_WITH_PATTERN = re.compile(
    r"key\.endsWith\s*\(\s*['\"]:\s*['\"\s]*\+\s*data\.clone_url\s*\)",
    re.DOTALL,
)


class TestRemoveSuccessfulSelectionsPartialSuccess:
    """Finding #1 fix: removeSuccessfulSelections must compare data.clone_url against
    item.clone_url (the result item's field).

    Partial-success regression: when A succeeds and B fails on the same platform,
    B must not be deleted. The old key.endsWith(':' + data.clone_url) predicate
    deleted B because it evaluated data.clone_url from B's own map entry, which
    always satisfied the condition. The fix matches on item.clone_url instead.
    """

    def test_remove_successful_selections_compares_data_clone_url_to_item_clone_url(
        self,
    ):
        """removeSuccessfulSelections must compare data.clone_url === item.clone_url.

        This is the correct predicate: item.clone_url comes from the batch result,
        not from the selectedRepos entry being iterated. Only the exact matching
        entry is removed, leaving failed repos intact.
        """
        _assert_template_contains(
            "removeSuccessfulSelections",
            _CLONE_URL_MATCH_PATTERN,
            "removeSuccessfulSelections must compare data.clone_url === item.clone_url "
            "so only the exact successful entry is removed from selectedRepos",
        )

    def test_remove_successful_selections_no_ends_with_data_clone_url(self):
        """removeSuccessfulSelections must NOT use key.endsWith(':' + data.clone_url).

        Partial-success regression: with A (success, url_a) and B (failed, url_b)
        both selected on the same platform, the buggy predicate evaluates
        data.clone_url from B's own entry — endsWith(':' + url_b) is true for B's
        key — so B is incorrectly deleted. The fix removes this predicate entirely.
        """
        _assert_template_not_contains(
            "removeSuccessfulSelections",
            _BUGGY_ENDS_WITH_PATTERN,
            "removeSuccessfulSelections must NOT use key.endsWith(':' + data.clone_url). "
            "This predicate incorrectly deletes failed repos sharing the same platform.",
        )


# ---------------------------------------------------------------------------
# Finding #2 (template): executeBatchCreate shape validation
# ---------------------------------------------------------------------------

_TYPEOF_SUCCESS_PATTERN = re.compile(
    r"typeof\s+result\.success\s*!==\s*['\"]boolean['\"]",
    re.DOTALL,
)
_ARRAY_IS_ARRAY_PATTERN = re.compile(
    r"Array\.isArray\s*\(\s*result\.results\s*\)",
    re.DOTALL,
)
_RESULTS_OR_FALLBACK_PATTERN = re.compile(r"result\.results\s*\|\|\s*\[\]", re.DOTALL)
_SUMMARY_OR_FALLBACK_PATTERN = re.compile(r"result\.summary\s*\|\|", re.DOTALL)
_ITEM_ERROR_OR_FALLBACK_PATTERN = re.compile(r"item\.error\s*\|\|", re.DOTALL)


class TestExecuteBatchCreateNoFallbacks:
    """Finding #2 fix: executeBatchCreate must validate response shape explicitly
    and remove || fallback chains that mask contract violations (Messi Rule #2)."""

    def test_execute_batch_create_validates_response_shape(self):
        """executeBatchCreate must check typeof result.success !== 'boolean'.

        Malformed response must trigger alert + early return, not silent misbehaviour.
        """
        _assert_template_contains(
            "executeBatchCreate",
            _TYPEOF_SUCCESS_PATTERN,
            "executeBatchCreate must validate typeof result.success !== 'boolean' "
            "to detect malformed responses and fail loudly",
        )

    def test_execute_batch_create_validates_results_is_array(self):
        """executeBatchCreate must check Array.isArray(result.results)."""
        _assert_template_contains(
            "executeBatchCreate",
            _ARRAY_IS_ARRAY_PATTERN,
            "executeBatchCreate must validate Array.isArray(result.results)",
        )

    def test_no_results_or_fallback_in_execute_or_render(self):
        """Neither executeBatchCreate nor renderBatchCreateFailures may use result.results || []."""
        _assert_template_not_contains(
            "executeBatchCreate",
            _RESULTS_OR_FALLBACK_PATTERN,
            "executeBatchCreate must not use result.results || [] after shape validation",
        )
        _assert_template_not_contains(
            "renderBatchCreateFailures",
            _RESULTS_OR_FALLBACK_PATTERN,
            "renderBatchCreateFailures must not use result.results || []",
        )

    def test_no_summary_or_fallback_in_execute_or_render(self):
        """Neither executeBatchCreate nor renderBatchCreateFailures may use result.summary || fallback."""
        _assert_template_not_contains(
            "executeBatchCreate",
            _SUMMARY_OR_FALLBACK_PATTERN,
            "executeBatchCreate must not use result.summary || fallback",
        )
        _assert_template_not_contains(
            "renderBatchCreateFailures",
            _SUMMARY_OR_FALLBACK_PATTERN,
            "renderBatchCreateFailures must not use result.summary || fallback",
        )

    def test_no_item_error_or_fallback_in_render(self):
        """renderBatchCreateFailures must not use item.error || fallback."""
        _assert_template_not_contains(
            "renderBatchCreateFailures",
            _ITEM_ERROR_OR_FALLBACK_PATTERN,
            "renderBatchCreateFailures must not use item.error || fallback — "
            "backend always populates error field for failed items",
        )
