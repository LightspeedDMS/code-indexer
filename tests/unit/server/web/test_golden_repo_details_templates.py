"""
Integration tests for Story #863: Lazy-load golden repo details template structure.

Static template analysis tests covering:
  - golden_repos_list.html: placeholder cells, no full details card, no flood triggers
  - golden_repo_details.html: file exists, no inline scripts, required sections present
  - golden_repos.html: toggleDetails three-state cache machine, htmx.ajax usage,
    restoreOpenDetails re-fetches via htmx.ajax

Acceptance criteria:
  - AC1: List template only contains table-row-visible fields; per-alias enrichment deferred
  - AC5: No loadAllGlobalActivatedData flood on page load or after Refresh
  - AC2: Details partial contains all required card sections
  - AC3: toggleDetails skips fetch if data-loaded == 'true' (cache hit)
  - AC4: restoreOpenDetails re-fetches after Refresh via htmx.ajax
"""

from pathlib import Path

import pytest

# Window size (chars) used when checking for loadAllGlobalActivatedData
# immediately after an htmx:afterSettle listener definition.
FLOOD_TRIGGER_SCAN_WINDOW = 500

# Single authoritative path to Jinja2 templates directory.
_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates"
)


def _read_template(relative_path: str) -> str:
    """Read a template file relative to the project templates directory."""
    return (_TEMPLATES_DIR / relative_path).read_text()


# ---------------------------------------------------------------------------
# List template structure tests
# ---------------------------------------------------------------------------


class TestGoldenRepoListTemplateStructure:
    """
    AC1 / AC5: golden_repos_list.html must use lazy-load placeholder structure.

    Asserts:
    - placeholder td id="details-content-{alias}" present
    - full details card <article class="repo-details"> absent
    - DOMContentLoaded + loadAllGlobalActivatedData removed (AC5)
    - htmx:afterSettle + loadAllGlobalActivatedData near-adjacency removed (AC5)
    """

    @pytest.fixture(scope="class")
    def content(self) -> str:
        return _read_template("partials/golden_repos_list.html")

    def test_has_details_content_placeholder(self, content: str):
        """Placeholder td id='details-content-{alias}' must replace the full details card."""
        assert "details-content-" in content, (
            "Expected 'details-content-' placeholder in golden_repos_list.html. "
            "The lazy-load refactor must add <td id='details-content-{alias}'> cells."
        )

    def test_no_full_details_card_article(self, content: str):
        """
        Full details card <article class='repo-details'> must be absent.

        After the refactor the 377-line details block lives in golden_repo_details.html
        and is fetched lazily; it must not be rendered inline in the list template.
        """
        assert '<article class="repo-details">' not in content, (
            "Found full details card in golden_repos_list.html. "
            "Must be extracted to golden_repo_details.html for lazy loading."
        )

    def test_dom_content_loaded_flood_trigger_removed(self, content: str):
        """
        AC5: DOMContentLoaded listener calling loadAllGlobalActivatedData must be removed.

        This trigger was firing one /api/repositories/{alias}/indexes request per
        globally-activated repo on every page load (potentially 500+ requests).
        """
        has_dom = "DOMContentLoaded" in content
        has_load_all = "loadAllGlobalActivatedData" in content
        assert not (has_dom and has_load_all), (
            "Found DOMContentLoaded + loadAllGlobalActivatedData in golden_repos_list.html. "
            "AC5 requires removing this trigger to prevent index flood on page load."
        )

    def test_htmx_aftesettle_flood_trigger_removed(self, content: str):
        """
        AC5: htmx:afterSettle listener calling loadAllGlobalActivatedData must be removed.

        This trigger caused a second flood on every Refresh button click.
        """
        if "htmx:afterSettle" not in content:
            return  # Listener fully removed — AC5 satisfied

        settle_idx = content.find("htmx:afterSettle")
        nearby = content[settle_idx : settle_idx + FLOOD_TRIGGER_SCAN_WINDOW]
        assert "loadAllGlobalActivatedData" not in nearby, (
            "Found htmx:afterSettle calling loadAllGlobalActivatedData in "
            "golden_repos_list.html. AC5 requires removing the Refresh flood trigger."
        )


# ---------------------------------------------------------------------------
# Details partial template tests
# ---------------------------------------------------------------------------


class TestGoldenRepoDetailsTemplateStructure:
    """
    AC2: golden_repo_details.html must exist and contain all required card sections.

    Asserts:
    - File exists
    - No inline <script> blocks
    - Description section present
    - Branch selector present
    - Indexes management section present
    - HNSW health section present
    - Wiki toggle present
    - csrf_token used in forms
    """

    @pytest.fixture(scope="class")
    def content(self) -> str:
        path = _TEMPLATES_DIR / "partials/golden_repo_details.html"
        assert path.exists(), (
            f"golden_repo_details.html not found at {path}. "
            "Create this template as part of the lazy-load refactor."
        )
        return _read_template("partials/golden_repo_details.html")

    def test_no_inline_script_blocks(self, content: str):
        """
        No inline <script> blocks allowed.

        htmx.ajax(..., swap='innerHTML') does not execute inline scripts by default.
        All JS must come from globally-loaded scripts already present on the page.
        """
        assert "<script>" not in content and "<script " not in content, (
            "Found <script> block in golden_repo_details.html. "
            "htmx.ajax swap='innerHTML' does not execute inline scripts."
        )

    def test_contains_description_section(self, content: str):
        """Details card must include the repository description section."""
        assert "repo-description" in content or "Repository Description" in content, (
            "Missing description section in golden_repo_details.html."
        )

    def test_contains_branch_selector(self, content: str):
        """Details card must include the branch selector element."""
        assert "branch-select" in content, (
            "Missing branch selector in golden_repo_details.html."
        )

    def test_contains_indexes_management(self, content: str):
        """Details card must include the indexes management section."""
        assert "indexes-management" in content or "Indexes Management" in content, (
            "Missing indexes management section in golden_repo_details.html."
        )

    def test_contains_health_section(self, content: str):
        """Details card must include the HNSW Index Health section."""
        assert "health-status-section" in content or "HNSW Index Health" in content, (
            "Missing health section in golden_repo_details.html."
        )

    def test_contains_wiki_toggle(self, content: str):
        """Details card must include the wiki toggle form."""
        assert "wiki-toggle" in content or "wiki_enabled" in content, (
            "Missing wiki toggle in golden_repo_details.html."
        )

    def test_uses_csrf_token(self, content: str):
        """Details card forms must use csrf_token."""
        assert "csrf_token" in content, (
            "Missing csrf_token in golden_repo_details.html."
        )


# ---------------------------------------------------------------------------
# Main template toggleDetails / restoreOpenDetails tests
# ---------------------------------------------------------------------------


class TestGoldenReposMainTemplateToggleDetails:
    """
    AC3 / AC4: golden_repos.html must implement the three-state cache machine.

    Asserts:
    - toggleDetails checks data-loaded attribute (AC3: skip fetch if 'true')
    - toggleDetails calls htmx.ajax on first open or retry
    - toggleDetails references the details partial URL pattern
    - restoreOpenDetails calls htmx.ajax to re-fetch after Refresh swap (AC4)
    """

    @pytest.fixture(scope="class")
    def content(self) -> str:
        return _read_template("golden_repos.html")

    def test_toggle_details_uses_data_loaded_cache_state(self, content: str):
        """
        AC3: toggleDetails must check data-loaded to skip redundant fetches.

        The three-state machine must detect 'true' (cached) and skip the fetch,
        preventing extra HTTP requests on re-expand.
        """
        assert "data-loaded" in content or "dataset.loaded" in content, (
            "toggleDetails must check data-loaded for the three-state cache machine "
            "(unset/loading/true/error)."
        )

    def test_toggle_details_fires_htmx_ajax(self, content: str):
        """AC2: toggleDetails must call htmx.ajax to fetch the details partial on first open."""
        assert "htmx.ajax" in content, (
            "toggleDetails must call htmx.ajax to fetch the details partial."
        )

    def test_toggle_details_references_details_partial_url(self, content: str):
        """toggleDetails must reference /admin/partials/golden-repos/{alias}/details."""
        assert "partials/golden-repos" in content, (
            "toggleDetails must reference the /admin/partials/golden-repos/{alias}/details URL."
        )

    def test_restore_open_details_refetches_via_htmx_ajax(self, content: str):
        """
        AC4: restoreOpenDetails must call htmx.ajax after Refresh swap.

        After the Refresh button replaces the repo list DOM, all previously-expanded
        details rows need re-fetching (content is stale). restoreOpenDetails must
        issue htmx.ajax fetches rather than just showing hidden rows.
        """
        restore_idx = content.find("function restoreOpenDetails")
        assert restore_idx >= 0, (
            "restoreOpenDetails function not found in golden_repos.html."
        )

        next_func_idx = content.find("\nfunction ", restore_idx + 1)
        func_body = (
            content[restore_idx:next_func_idx]
            if next_func_idx != -1
            else content[restore_idx:]
        )

        assert "htmx.ajax" in func_body, (
            "restoreOpenDetails must call htmx.ajax to re-fetch details "
            "after the Refresh button replaces the repo list (AC4)."
        )


# ---------------------------------------------------------------------------
# Named constants for JS block extraction windows
# ---------------------------------------------------------------------------

# Window (chars) when extracting the .then() callback body from htmx.ajax calls
_THEN_BLOCK_WINDOW = 800

# Window (chars) when searching for shouldSwap override near htmx:beforeSwap
_BEFORE_SWAP_WINDOW = 600

# Window (chars) when searching for retry selector inside an addEventListener block
_EVENT_LISTENER_WINDOW = 600


# ---------------------------------------------------------------------------
# Helpers: extract JS constructs from template content
# ---------------------------------------------------------------------------


def _extract_function_body(content: str, func_name: str) -> str:
    """
    Extract the body of a JS function named `func_name` from `content`.

    Finds 'function <func_name>' and returns from that point to the next
    top-level 'function ' declaration (or end of content).  Returns empty
    string if the function is not found.
    """
    start = content.find(f"function {func_name}")
    if start == -1:
        return ""
    end = content.find("\nfunction ", start + 1)
    return content[start:end] if end != -1 else content[start:]


def _extract_then_block(func_body: str) -> str:
    """
    Extract _THEN_BLOCK_WINDOW chars starting at '.then(' in `func_body`.

    Returns empty string if '.then(' is not found.
    """
    then_idx = func_body.find(".then(")
    if then_idx == -1:
        return ""
    return func_body[then_idx : then_idx + _THEN_BLOCK_WINDOW]


def _extract_initializer_body(content: str) -> str:
    """
    Return the body of the per-row post-swap initializer function.

    Tries '_initDetailsRow' first, then 'initDetailsRow'.
    Returns empty string if neither is found.
    """
    return _extract_function_body(content, "_initDetailsRow") or _extract_function_body(
        content, "initDetailsRow"
    )


# ---------------------------------------------------------------------------
# Finding 4 (part 1): post-swap initializer definition and calls (3 tests)
# ---------------------------------------------------------------------------


class TestGoldenReposJsLifecyclePart1:
    """
    Finding 4 (part 1): per-row initializer must be defined and called from
    the htmx.ajax .then() chain, and must invoke loadRepoDescription and
    loadBranches in the correct order.
    """

    def test_toggle_details_invokes_per_row_post_swap_initializer(self):
        """
        Finding 1: (a) Initializer function defined in file, AND (b) called
        from within the .then() block of toggleDetails htmx.ajax.
        """
        content = _read_template("golden_repos.html")
        has_def = (
            "function _initDetailsRow" in content
            or "function initDetailsRow" in content
        )
        assert has_def, (
            "Per-row initializer (_initDetailsRow or initDetailsRow) must be "
            "defined in golden_repos.html."
        )
        toggle_body = _extract_function_body(content, "toggleDetails")
        assert toggle_body, "toggleDetails function not found in golden_repos.html."
        then_block = _extract_then_block(toggle_body)
        assert then_block, "toggleDetails htmx.ajax must have a .then() callback."
        has_call = "_initDetailsRow(" in then_block or "initDetailsRow(" in then_block
        assert has_call, (
            "The .then() block of toggleDetails must call the per-row initializer."
        )

    def test_post_swap_initializer_calls_load_repo_description(self):
        """
        Finding 1: Initializer must call loadRepoDescription(alias), scoped to
        the initializer body.
        """
        content = _read_template("golden_repos.html")
        init_body = _extract_initializer_body(content)
        assert init_body, (
            "Per-row initializer (_initDetailsRow or initDetailsRow) "
            "not found in golden_repos.html."
        )
        assert "loadRepoDescription(" in init_body, (
            "Per-row initializer must call loadRepoDescription(alias)."
        )

    def test_post_swap_initializer_resets_branches_dataset_loaded(self):
        """
        Finding 1: Initializer must reset dataset.loaded='false' BEFORE
        calling loadBranches, verified by position in the initializer body.
        """
        content = _read_template("golden_repos.html")
        init_body = _extract_initializer_body(content)
        assert init_body, "Per-row initializer not found in golden_repos.html."
        reset_idx = init_body.find("dataset.loaded = 'false'")
        if reset_idx == -1:
            reset_idx = init_body.find('dataset.loaded = "false"')
        assert reset_idx != -1, (
            "Initializer must reset branchSelect.dataset.loaded = 'false'."
        )
        load_branches_idx = init_body.find("loadBranches(")
        assert load_branches_idx != -1, "Initializer must call loadBranches()."
        assert reset_idx < load_branches_idx, (
            "dataset.loaded reset must precede loadBranches() in the initializer."
        )


# ---------------------------------------------------------------------------
# Finding 4 (part 2): error-recovery state machine — 3 tests
# ---------------------------------------------------------------------------


class TestGoldenReposJsLifecyclePart2:
    """
    Finding 4 (part 2): per-row initializer global-indexes call, htmx:beforeSwap
    error override, and Retry button event handler.
    """

    def test_post_swap_initializer_calls_load_global_activated_indexes(self):
        """
        Finding 1: Initializer must call loadGlobalActivatedIndexes, scoped to
        the initializer body so the assertion proves lifecycle wiring.
        """
        content = _read_template("golden_repos.html")
        init_body = _extract_initializer_body(content)
        assert init_body, "Per-row initializer not found in golden_repos.html."
        assert "loadGlobalActivatedIndexes(" in init_body, (
            "Per-row initializer must call loadGlobalActivatedIndexes so the "
            "global-activated indexes section populates after the htmx swap."
        )

    def test_htmx_before_swap_handler_allows_error_swap_for_details_targets(self):
        """
        Finding 2: htmx:beforeSwap listener must set shouldSwap=true within
        _BEFORE_SWAP_WINDOW chars of the event name registration.
        """
        content = _read_template("golden_repos.html")
        assert "htmx:beforeSwap" in content, (
            "golden_repos.html must define an htmx:beforeSwap listener."
        )
        before_idx = content.find("htmx:beforeSwap")
        nearby = content[before_idx : before_idx + _BEFORE_SWAP_WINDOW]
        assert "shouldSwap" in nearby, (
            "htmx:beforeSwap handler must set event.detail.shouldSwap = true "
            "to allow error fragments to be swapped into details cells."
        )

    def test_retry_button_handler_present(self):
        """
        Finding 2: details-error-retry selector must appear inside an
        addEventListener block (anchored on listener, not on selector).
        """
        content = _read_template("golden_repos.html")
        found = False
        search_start = 0
        while True:
            idx = content.find("addEventListener", search_start)
            if idx == -1:
                break
            block = content[idx : idx + _EVENT_LISTENER_WINDOW]
            if "details-error-retry" in block:
                found = True
                break
            search_start = idx + 1
        assert found, (
            ".details-error-retry selector must appear inside an addEventListener "
            "block so the Retry button has a functional click handler (AC8)."
        )


# ---------------------------------------------------------------------------
# Finding 1b: nested sub-panel restore ordering — 2 tests
# ---------------------------------------------------------------------------


class TestGoldenReposNestedSubpanelRestoreOrdering:
    """
    Finding 1b: nested sub-panel restoration (add-index forms, health details)
    must be wired into the per-row _initDetailsRow path, NOT into the global
    list-level htmx:afterSettle handler.

    Without this fix, restoreOpenAddIndexForms() and restoreOpenHealthDetails()
    fire synchronously in htmx:afterSettle while the per-row htmx.ajax fetches
    are still in-flight, so the DOM nodes do not exist yet and nothing is restored.
    """

    @pytest.fixture(scope="class")
    def content(self) -> str:
        return _read_template("golden_repos.html")

    def test_init_details_row_restores_nested_subpanel_state(self, content: str):
        """
        Finding 1b: _initDetailsRow body must reference openAddIndexForms and
        openHealthDetailsSet so nested sub-panels are restored at the correct
        moment (after the per-row htmx.ajax swap, NOT at list-level afterSettle).
        """
        init_body = _extract_initializer_body(content)
        assert init_body, (
            "Per-row initializer (_initDetailsRow or initDetailsRow) "
            "not found in golden_repos.html."
        )
        assert "openAddIndexForms" in init_body, (
            "_initDetailsRow must reference openAddIndexForms to restore the "
            "add-index form for this alias after the per-row swap. "
            "Without this the add-index form is lost after every Refresh."
        )
        assert "openHealthDetailsSet" in init_body, (
            "_initDetailsRow must reference openHealthDetailsSet to restore the "
            "health details panel for this alias after the per-row swap. "
            "Without this the health details panel is lost after every Refresh."
        )

    def test_list_afterSettle_does_not_call_global_restore_helpers(self, content: str):
        """
        Finding 1b: the list-level htmx:afterSettle handler must NOT call
        restoreOpenAddIndexForms() or restoreOpenHealthDetails() directly.

        Those calls race against in-flight htmx.ajax fetches — the DOM nodes
        they target do not exist yet. The bug is fixed by removing these calls
        from afterSettle and instead driving them from _initDetailsRow.
        """
        settle_idx = content.find("htmx:afterSettle")
        assert settle_idx != -1, (
            "htmx:afterSettle listener not found in golden_repos.html."
        )
        # Extract from the afterSettle listener to end of the script block.
        after_settle_block = content[settle_idx:]
        # Find the closing brace / next top-level event listener boundary.
        # Use a generous window — we only need to confirm these calls are absent.
        scan_window = after_settle_block[:800]
        assert "restoreOpenAddIndexForms()" not in scan_window, (
            "htmx:afterSettle handler must NOT call restoreOpenAddIndexForms() "
            "directly — it races against in-flight per-row htmx.ajax fetches. "
            "The call must be moved into _initDetailsRow (Finding 1b fix)."
        )
        assert "restoreOpenHealthDetails()" not in scan_window, (
            "htmx:afterSettle handler must NOT call restoreOpenHealthDetails() "
            "directly — it races against in-flight per-row htmx.ajax fetches. "
            "The call must be moved into _initDetailsRow (Finding 1b fix)."
        )
