"""
Unit tests for Bug #860: Auto-discovery silently fails to register private repos.

Root cause: auto_discovery.html hardcoded clone_url_https everywhere.
Fix: use preferredCloneUrl() helper that returns SSH URL for private repos.

These are structural content tests of the HTML/JS template source, which is
the appropriate testing approach for embedded JavaScript in a Python test suite
(same pattern as test_dependency_map_js_code_mass.py).
"""

from pathlib import Path

# Ordered list of all known top-level JS function names in auto_discovery.html.
# Used by _function_slice() to find deterministic function boundaries without
# relying on fragile formatting-based heuristics.
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
    "closeBatchModal",
    "executeBatchCreate",
    "doHide",
    "doUnhide",
    "getCsrfToken",
    "escHtml",
    "sizeTableContainer",
]


def _read_template() -> str:
    """Read the auto_discovery.html template content.

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

    Uses an explicit ordered list of known function names (_KNOWN_FUNCTIONS) as
    boundary markers, so the slice is deterministic regardless of whitespace or
    formatting changes. Does NOT attempt to parse braces or detect indentation.

    Returns empty string if the function is not found.
    """
    marker = "function " + function_name + "("
    start = html.find(marker)
    if start == -1:
        return ""

    # Find the current function's position in the known list
    try:
        func_idx = _KNOWN_FUNCTIONS.index(function_name)
    except ValueError:
        # Unknown function — fall back to end of html
        return html[start:]

    # Try each subsequent known function as a boundary marker
    end = len(html)
    for sibling in _KNOWN_FUNCTIONS[func_idx + 1 :]:
        sibling_marker = "function " + sibling + "("
        pos = html.find(sibling_marker, start + 1)
        if pos != -1:
            end = pos
            break

    return html[start:end]


class TestPreferredCloneUrlHelper:
    """AC: preferredCloneUrl(repo) helper extracted that returns SSH URL for private repos."""

    def test_preferred_clone_url_function_defined(self):
        """preferredCloneUrl function must be defined in the template."""
        html = _read_template()
        assert "function preferredCloneUrl(repo)" in html, (
            "auto_discovery.html must define preferredCloneUrl(repo) helper function"
        )

    def test_preferred_clone_url_returns_ssh_for_private(self):
        """preferredCloneUrl must reference clone_url_ssh for private repos."""
        body = _function_slice(_read_template(), "preferredCloneUrl")
        assert body, "preferredCloneUrl function must exist"
        assert "repo.clone_url_ssh" in body, (
            "preferredCloneUrl must return repo.clone_url_ssh for private repos"
        )

    def test_preferred_clone_url_ternary_structure(self):
        """preferredCloneUrl must use ternary: is_private ? ssh : https."""
        body = _function_slice(_read_template(), "preferredCloneUrl")
        assert body, "preferredCloneUrl function must exist"
        assert "repo.is_private ? repo.clone_url_ssh : repo.clone_url_https" in body, (
            "preferredCloneUrl must implement: "
            "repo.is_private ? repo.clone_url_ssh : repo.clone_url_https"
        )


class TestDataPreferredUrlAttribute:
    """AC: data-preferred-url attribute added to checkbox at render time."""

    def test_data_preferred_url_attribute_in_checkbox_render(self):
        """Checkbox render must include data-preferred-url attribute."""
        html = _read_template()
        assert "data-preferred-url=" in html, (
            "Checkbox render must include data-preferred-url attribute"
        )

    def test_data_preferred_url_uses_preferred_clone_url_helper(self):
        """data-preferred-url attribute value must use preferredCloneUrl() helper."""
        html = _read_template()
        assert "escHtml(preferredCloneUrl(repo))" in html, (
            "data-preferred-url value must be set via escHtml(preferredCloneUrl(repo))"
        )


class TestOnCheckboxChangeUsesDatasetPreferredUrl:
    """AC: onCheckboxChange uses checkbox.dataset.preferredUrl — no silent fallback."""

    def test_on_checkbox_change_uses_dataset_preferred_url(self):
        """onCheckboxChange must use checkbox.dataset.preferredUrl for clone_url."""
        body = _function_slice(_read_template(), "onCheckboxChange")
        assert body, "onCheckboxChange function must exist"
        assert "checkbox.dataset.preferredUrl" in body, (
            "onCheckboxChange must use checkbox.dataset.preferredUrl for clone_url, "
            "not checkbox.value (the HTTPS URL)"
        )

    def test_on_checkbox_change_no_https_fallback(self):
        """onCheckboxChange must NOT fall back to HTTPS via || operator.

        Anti-fallback principle (Messi Rule #2): fail loudly if
        dataset.preferredUrl is missing, do not silently use HTTPS.
        """
        body = _function_slice(_read_template(), "onCheckboxChange")
        assert body, "onCheckboxChange function must exist"
        assert "|| url" not in body, (
            "onCheckboxChange must NOT have a '|| url' fallback — "
            "fail loudly if dataset.preferredUrl is missing"
        )
        assert "|| checkbox.value" not in body, (
            "onCheckboxChange must NOT fall back to checkbox.value (HTTPS URL)"
        )


class TestToggleSelectAllUsesPreferredCloneUrl:
    """AC: toggleSelectAll uses preferredCloneUrl() helper for clone_url."""

    def test_toggle_select_all_uses_preferred_clone_url(self):
        """toggleSelectAll must use preferredCloneUrl(repo) for clone_url."""
        body = _function_slice(_read_template(), "toggleSelectAll")
        assert body, "toggleSelectAll function must exist"
        assert "preferredCloneUrl(repo)" in body, (
            "toggleSelectAll must use preferredCloneUrl(repo) for clone_url assignment, "
            "not hardcoded repo.clone_url_https"
        )

    def test_toggle_select_all_no_hardcoded_https_for_clone_url(self):
        """toggleSelectAll must NOT hardcode clone_url_https for the clone_url field."""
        body = _function_slice(_read_template(), "toggleSelectAll")
        assert body, "toggleSelectAll function must exist"
        assert "clone_url: repo.clone_url_https" not in body, (
            "toggleSelectAll must not hardcode clone_url: repo.clone_url_https — "
            "use preferredCloneUrl(repo) instead"
        )
