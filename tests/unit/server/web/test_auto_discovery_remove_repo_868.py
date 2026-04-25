"""
Unit tests for Story #868: Remove Repo from Auto-Discovery Pending List.

These are structural content tests of the HTML/JS/CSS template source,
following the same pattern as test_autodiscovery_ssh_url_fix.py.

5 Acceptance Criteria tested:
  AC1: Remove a repo -> row removed from DOM, selectedRepos entry deleted,
       updateSelectionUI() called; in-flight branch fetch silently discarded (async safety)
  AC2: Remove all repos -> "No repositories selected." shown, execute-batch-btn disabled
  AC3: Batch-create payload contains ONLY remaining repos (removed repo absent)
  AC4: Remove button: <button type="button"> with aria-label naming the specific repo
  AC5: Remove button styled matching .close-btn (background:none, border:none, cursor:pointer);
       no confirmation dialog on click
"""

from pathlib import Path

# Characters to scan from the start of a CSS rule to find its property declarations.
_CSS_BLOCK_SCAN_CHARS = 400

# Characters to scan from the start of an aria-label= attribute to find the repo-name
# expression interpolated inside the attribute value.
_ARIA_LABEL_VALUE_SCAN_CHARS = 80

# Ordered known function list — must stay in sync with test_autodiscovery_ssh_url_fix.py.
# removeRepo is inserted after showCreateDialog.
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
    "removeRepo",
    "fetchBranchesForSelectedRepos",
    "updateRepoBranchDropdown",
    "onBranchChange",
    "closeBatchModal",
    "executeBatchCreate",
    "doHide",
    "doUnhide",
    "getCsrfToken",
    "escHtml",
    "sizeTableContainer",
]


def _read_template() -> str:
    """Read auto_discovery.html template content.

    Path traversal from tests/unit/server/web/:
      .parent       -> tests/unit/server/web/
      .parent.parent -> tests/unit/server/
      .parent x3    -> tests/unit/
      .parent x4    -> tests/
      .parent x5    -> project root
    """
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
    """Return the text slice from a JS function declaration to the next known sibling.

    Uses _KNOWN_FUNCTIONS as boundary markers so the slice is deterministic.
    Returns empty string if the function is not found.
    """
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


def _css_rule_block(html: str, selector: str) -> str:
    """Return a text slice starting at selector covering _CSS_BLOCK_SCAN_CHARS characters.

    Used to inspect CSS property declarations inside a rule body.
    Returns empty string if selector is not found.
    """
    start = html.find(selector)
    if start == -1:
        return ""
    return html[start : start + _CSS_BLOCK_SCAN_CHARS]


def _aria_label_contains_repo_name(function_body: str) -> bool:
    """Return True if an aria-label attribute in function_body contains a repo-name expression.

    Scans each occurrence of 'aria-label=' in the body and checks whether the
    repo-name expression (data.name or escHtml(data.name)) appears within
    _ARIA_LABEL_VALUE_SCAN_CHARS characters of the attribute start — i.e. inside
    the attribute value rather than elsewhere in the function.
    """
    search_start = 0
    while True:
        pos = function_body.find("aria-label=", search_start)
        if pos == -1:
            return False
        # Scan the window immediately after the attribute name
        window = function_body[pos : pos + _ARIA_LABEL_VALUE_SCAN_CHARS]
        if "data.name" in window or "escHtml(data.name)" in window:
            return True
        search_start = pos + 1


class TestAC1RemoveRepoFunction:
    """AC1: removeRepo JS function removes row from DOM, deletes from selectedRepos,
    calls updateSelectionUI(); in-flight branch fetch silently discarded (async safety).
    """

    def test_remove_repo_function_defined(self):
        """removeRepo function must be defined in the template."""
        html = _read_template()
        assert "function removeRepo(" in html, (
            "auto_discovery.html must define removeRepo() JS function"
        )

    def test_remove_repo_deletes_from_selected_repos(self):
        """removeRepo must call selectedRepos.delete() to remove the entry."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "selectedRepos.delete(" in body, (
            "removeRepo must call selectedRepos.delete(key) to remove the entry"
        )

    def test_remove_repo_calls_update_selection_ui(self):
        """removeRepo must call updateSelectionUI() to sync discovery-page checkbox/count."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "updateSelectionUI()" in body, (
            "removeRepo must call updateSelectionUI() to keep selection bar consistent"
        )

    def test_remove_repo_removes_row_from_dom(self):
        """removeRepo must call .remove() on the row element to delete it from the DOM."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert ".remove(" in body, (
            "removeRepo must call .remove() to delete the row element from the DOM"
        )

    def test_async_safety_update_repo_branch_dropdown_guards_missing_item(self):
        """AC1 async safety: branch fetch completing after row removal must be silently discarded.

        updateRepoBranchDropdown has 'if (!item) return;' — when removeRepo() deletes
        the DOM row, querySelector returns null and the function exits without error,
        discarding the stale branch response.
        """
        body = _function_slice(_read_template(), "updateRepoBranchDropdown")
        assert body, "updateRepoBranchDropdown function must exist"
        assert "if (!item) return" in body, (
            "updateRepoBranchDropdown must have 'if (!item) return' guard to silently "
            "discard branch-fetch responses that complete after row removal (AC1 async safety)"
        )


class TestAC2EmptyStateGuard:
    """AC2: Removing all repos shows 'No repositories selected.' and disables execute button."""

    def test_remove_repo_shows_empty_state_message(self):
        """removeRepo must show 'No repositories selected.' when selectedRepos becomes empty."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "No repositories selected." in body, (
            "removeRepo must display 'No repositories selected.' when list is empty"
        )

    def test_remove_repo_disables_execute_btn_when_empty(self):
        """removeRepo must disable #execute-batch-btn when selectedRepos becomes empty."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "execute-batch-btn" in body, (
            "removeRepo must reference #execute-batch-btn to disable it when list is empty"
        )

    def test_remove_repo_updates_batch_count_label(self):
        """removeRepo must update the #batch-count label on each removal."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "batch-count" in body, (
            "removeRepo must update #batch-count label on each removal"
        )


class TestAC3BatchPayloadContainsOnlyRemainingRepos:
    """AC3: executeBatchCreate payload contains ONLY repos still in selectedRepos.

    Guarantee: executeBatchCreate iterates selectedRepos directly at submit time.
    Since removeRepo deletes from selectedRepos, a removed repo is automatically
    absent from the payload — no secondary filtering needed.
    """

    def test_execute_batch_create_iterates_selected_repos_directly(self):
        """executeBatchCreate must iterate selectedRepos.forEach() to build the payload.

        Direct iteration means any repo deleted by removeRepo() before submit is
        automatically excluded from the batch-create request body.
        """
        body = _function_slice(_read_template(), "executeBatchCreate")
        assert body, "executeBatchCreate function must exist"
        assert "selectedRepos.forEach(" in body, (
            "executeBatchCreate must iterate selectedRepos.forEach() to build the payload, "
            "ensuring removed repos (deleted from selectedRepos) are absent"
        )

    def test_execute_batch_create_does_not_spread_snapshot_before_removal(self):
        """executeBatchCreate must NOT snapshot selectedRepos into a separate array.

        Spreading selectedRepos at modal-open time would include repos subsequently
        removed. The correct pattern is direct iteration at submit time.
        """
        body = _function_slice(_read_template(), "executeBatchCreate")
        assert body, "executeBatchCreate function must exist"
        assert "[...selectedRepos]" not in body, (
            "executeBatchCreate must not snapshot selectedRepos with spread — "
            "iterate directly so removed repos are excluded from the payload"
        )
        assert "Array.from(selectedRepos)" not in body, (
            "executeBatchCreate must not snapshot selectedRepos with Array.from — "
            "iterate directly so removed repos are excluded from the payload"
        )


class TestAC4RemoveButtonKeyboardAccessible:
    """AC4: Remove button is <button type='button'> with aria-label naming the specific repo."""

    def test_show_create_dialog_renders_remove_repo_btn_class(self):
        """showCreateDialog must render a button with class 'remove-repo-btn' per row."""
        body = _function_slice(_read_template(), "showCreateDialog")
        assert body, "showCreateDialog function must exist"
        assert "remove-repo-btn" in body, (
            "showCreateDialog must render a button with class 'remove-repo-btn' in each row"
        )

    def test_show_create_dialog_button_has_type_button(self):
        """Remove button must have type='button' so Enter/Space triggers click (AC4)."""
        body = _function_slice(_read_template(), "showCreateDialog")
        assert body, "showCreateDialog function must exist"
        has_type_button = (
            'type=\\"button\\"' in body
            or "type='button'" in body
            or 'type="button"' in body
        )
        assert has_type_button, (
            "Remove button must have type='button' for keyboard accessibility"
        )

    def test_show_create_dialog_button_aria_label_names_specific_repo(self):
        """aria-label value must contain the repo name expression, not just 'Remove' (AC4).

        Uses _aria_label_contains_repo_name() which scans within _ARIA_LABEL_VALUE_SCAN_CHARS
        characters of each 'aria-label=' occurrence to confirm data.name (or
        escHtml(data.name)) is interpolated inside the attribute value itself —
        not merely present elsewhere in the function body.
        """
        body = _function_slice(_read_template(), "showCreateDialog")
        assert body, "showCreateDialog function must exist"
        assert _aria_label_contains_repo_name(body), (
            "Remove button aria-label must interpolate the repo name (data.name or "
            "escHtml(data.name)) inside the attribute value so screen readers announce "
            "'Remove <repo name>' rather than a generic 'Remove' label"
        )

    def test_show_create_dialog_button_calls_remove_repo(self):
        """Remove button onclick must call removeRepo() handler."""
        body = _function_slice(_read_template(), "showCreateDialog")
        assert body, "showCreateDialog function must exist"
        assert "removeRepo(" in body, (
            "Remove button must call removeRepo() on click"
        )


class TestAC5RemoveButtonCSS:
    """AC5: Remove button styled to match .close-btn pattern; no confirmation dialog on click."""

    def test_remove_repo_btn_css_selector_defined(self):
        """CSS for .remove-repo-btn must be defined in the template <style> block."""
        html = _read_template()
        assert ".remove-repo-btn" in html, (
            "Template must define a CSS rule for .remove-repo-btn class"
        )

    def test_remove_repo_btn_css_has_background_none(self):
        """CSS for .remove-repo-btn must set background: none, matching .close-btn pattern."""
        html = _read_template()
        block = _css_rule_block(html, ".remove-repo-btn")
        assert block, ".remove-repo-btn CSS selector must exist"
        assert "background: none" in block or "background:none" in block, (
            ".remove-repo-btn CSS must set 'background: none' to match the .close-btn pattern"
        )

    def test_remove_repo_btn_css_has_border_none(self):
        """CSS for .remove-repo-btn must set border: none, matching .close-btn pattern."""
        html = _read_template()
        block = _css_rule_block(html, ".remove-repo-btn")
        assert block, ".remove-repo-btn CSS selector must exist"
        assert "border: none" in block or "border:none" in block, (
            ".remove-repo-btn CSS must set 'border: none' to match the .close-btn pattern"
        )

    def test_remove_repo_btn_css_has_cursor_pointer(self):
        """CSS for .remove-repo-btn must set cursor: pointer, matching .close-btn pattern."""
        html = _read_template()
        block = _css_rule_block(html, ".remove-repo-btn")
        assert block, ".remove-repo-btn CSS selector must exist"
        assert "cursor: pointer" in block or "cursor:pointer" in block, (
            ".remove-repo-btn CSS must set 'cursor: pointer' to match the .close-btn pattern"
        )

    def test_no_confirmation_dialog_in_remove_repo(self):
        """removeRepo must NOT call confirm() — no confirmation dialog on click (AC5)."""
        body = _function_slice(_read_template(), "removeRepo")
        assert body, "removeRepo function must exist"
        assert "confirm(" not in body, (
            "removeRepo must not call confirm() — no confirmation dialog per AC5"
        )
