"""Tests for Wiki Toolbar and Sidebar UX Polish (Story #294).

RED phase: all tests written before implementation exists.
Covers:
 - AC1: Compact toolbar buttons — hamburger and theme toggle 32x32px, side-by-side, left-aligned
 - AC2: Sidebar resizable via drag handle on right edge
 - AC3: Min 180px, max 50% viewport width constraints
 - AC4: Width persists in localStorage (key: wiki_sidebar_width), saved on mouseup
 - AC5: Default width 280px via CSS, no localStorage write until user resizes
 - AC6: Works in both light and dark themes (CSS custom properties)
"""
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _template_path() -> Path:
    here = Path(__file__).parent
    project_root = here.parent.parent.parent.parent
    return project_root / "src" / "code_indexer" / "server" / "wiki" / "templates" / "article.html"


def _css_path() -> Path:
    here = Path(__file__).parent
    project_root = here.parent.parent.parent.parent
    return project_root / "src" / "code_indexer" / "server" / "wiki" / "static" / "wiki.css"


def _js_path() -> Path:
    here = Path(__file__).parent
    project_root = here.parent.parent.parent.parent
    return project_root / "src" / "code_indexer" / "server" / "wiki" / "static" / "wiki.js"


def _read_template() -> str:
    return _template_path().read_text(encoding="utf-8")


def _read_css() -> str:
    return _css_path().read_text(encoding="utf-8")


def _read_js() -> str:
    return _js_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Template structure tests (AC1, AC2)
# ---------------------------------------------------------------------------


class TestTemplateStructure:
    """Validate HTML template structure for toolbar and sidebar drag handle."""

    def test_sidebar_toggle_button_has_compact_class(self):
        """AC1: sidebar-toggle button must have toolbar-btn-compact class."""
        html = _read_template()
        assert 'id="sidebar-toggle"' in html
        lines = html.splitlines()
        sidebar_toggle_lines = [l for l in lines if 'id="sidebar-toggle"' in l]
        assert len(sidebar_toggle_lines) >= 1, "No line containing sidebar-toggle found"
        assert "toolbar-btn-compact" in sidebar_toggle_lines[0], (
            "sidebar-toggle button must have class toolbar-btn-compact"
        )

    def test_theme_toggle_button_has_compact_class(self):
        """AC1: theme-toggle button must have toolbar-btn-compact class."""
        html = _read_template()
        assert 'id="theme-toggle"' in html
        lines = html.splitlines()
        theme_toggle_lines = [l for l in lines if 'id="theme-toggle"' in l]
        assert len(theme_toggle_lines) >= 1, "No line containing theme-toggle found"
        assert "toolbar-btn-compact" in theme_toggle_lines[0], (
            "theme-toggle button must have class toolbar-btn-compact"
        )

    def test_both_buttons_appear_before_toolbar_spacer(self):
        """AC1: Both hamburger and theme toggle must appear before .toolbar-spacer (left-aligned)."""
        html = _read_template()
        sidebar_pos = html.find('id="sidebar-toggle"')
        theme_pos = html.find('id="theme-toggle"')
        spacer_pos = html.find('toolbar-spacer')

        assert sidebar_pos != -1, "sidebar-toggle not found in template"
        assert theme_pos != -1, "theme-toggle not found in template"
        assert spacer_pos != -1, "toolbar-spacer not found in template"

        assert sidebar_pos < spacer_pos, (
            "sidebar-toggle must appear before toolbar-spacer (left-aligned)"
        )
        assert theme_pos < spacer_pos, (
            "theme-toggle must appear before toolbar-spacer (left-aligned)"
        )

    def test_both_buttons_are_close_together_in_toolbar(self):
        """AC1: Both buttons should be adjacent (side-by-side) in the toolbar."""
        html = _read_template()
        sidebar_pos = html.find('id="sidebar-toggle"')
        theme_pos = html.find('id="theme-toggle"')

        assert sidebar_pos != -1, "sidebar-toggle not found"
        assert theme_pos != -1, "theme-toggle not found"

        # The buttons should be within 500 chars of each other (adjacent in toolbar)
        distance = abs(theme_pos - sidebar_pos)
        assert distance < 500, (
            f"Buttons are {distance} chars apart — they should be adjacent (side-by-side)"
        )

    def test_drag_handle_element_exists(self):
        """AC2: wiki-sidebar-handle element must exist in the template."""
        html = _read_template()
        assert 'id="wiki-sidebar-handle"' in html, (
            "Drag handle element with id='wiki-sidebar-handle' must exist in template"
        )

    def test_drag_handle_has_correct_class(self):
        """AC2: Drag handle element must have class 'wiki-sidebar-handle'."""
        html = _read_template()
        lines = html.splitlines()
        handle_lines = [l for l in lines if 'wiki-sidebar-handle' in l]
        assert len(handle_lines) >= 1, "No drag handle element found"

    def test_drag_handle_is_inside_or_adjacent_to_sidebar(self):
        """AC2: Drag handle should be positioned near the sidebar element."""
        html = _read_template()
        sidebar_start = html.find('id="wiki-sidebar"')
        handle_pos = html.find('id="wiki-sidebar-handle"')

        assert sidebar_start != -1, "wiki-sidebar not found"
        assert handle_pos != -1, "wiki-sidebar-handle not found"

        assert handle_pos > sidebar_start, (
            "Handle must come after sidebar opening tag"
        )

    def test_wiki_home_link_still_present(self):
        """Regression: Wiki Home link must still be present in the toolbar."""
        html = _read_template()
        assert 'toolbar-home' in html, "Wiki Home link with class toolbar-home must remain"
        assert '/wiki/' in html, "Wiki Home href must still be present"


# ---------------------------------------------------------------------------
# CSS validation tests (AC1, AC2, AC3, AC5, AC6)
# ---------------------------------------------------------------------------


class TestCSSValidation:
    """Validate CSS rules for compact buttons, drag handle, and theme support."""

    def test_toolbar_btn_compact_class_exists(self):
        """AC1: .toolbar-btn-compact CSS class must exist."""
        css = _read_css()
        assert ".toolbar-btn-compact" in css, (
            ".toolbar-btn-compact CSS class must be defined"
        )

    def test_toolbar_btn_compact_has_32px_dimensions(self):
        """AC1: .toolbar-btn-compact must set width and height to 32px."""
        css = _read_css()
        compact_idx = css.find(".toolbar-btn-compact")
        assert compact_idx != -1, ".toolbar-btn-compact not found in CSS"
        block_end = css.find("}", compact_idx)
        compact_block = css[compact_idx:block_end]
        assert "32px" in compact_block, (
            ".toolbar-btn-compact block must contain 32px (for width/height)"
        )

    def test_toolbar_btn_compact_uses_flex_display(self):
        """AC1: .toolbar-btn-compact must use display: flex for icon centering."""
        css = _read_css()
        compact_idx = css.find(".toolbar-btn-compact")
        assert compact_idx != -1
        block_end = css.find("}", compact_idx)
        compact_block = css[compact_idx:block_end]
        assert "display: flex" in compact_block or "display:flex" in compact_block, (
            ".toolbar-btn-compact must use display: flex"
        )

    def test_wiki_sidebar_handle_class_exists(self):
        """AC2: .wiki-sidebar-handle CSS class must exist."""
        css = _read_css()
        assert ".wiki-sidebar-handle" in css, (
            ".wiki-sidebar-handle CSS class must be defined"
        )

    def test_wiki_sidebar_handle_has_col_resize_cursor(self):
        """AC2: .wiki-sidebar-handle must have cursor: col-resize."""
        css = _read_css()
        handle_idx = css.find(".wiki-sidebar-handle")
        assert handle_idx != -1, ".wiki-sidebar-handle not found in CSS"
        block_end = css.find("}", handle_idx)
        handle_block = css[handle_idx:block_end]
        assert "col-resize" in handle_block, (
            ".wiki-sidebar-handle must have cursor: col-resize"
        )

    def test_wiki_sidebar_handle_uses_flex_stretch(self):
        """AC2: .wiki-sidebar-handle must use align-self: stretch as a flex sibling."""
        css = _read_css()
        handle_idx = css.find(".wiki-sidebar-handle")
        assert handle_idx != -1
        block_end = css.find("}", handle_idx)
        handle_block = css[handle_idx:block_end]
        assert "align-self: stretch" in handle_block or "align-self:stretch" in handle_block, (
            ".wiki-sidebar-handle must have align-self: stretch"
        )

    def test_wiki_sidebar_does_not_have_resize_horizontal(self):
        """AC2: .wiki-sidebar must NOT have 'resize: horizontal' (replaced by custom drag handle)."""
        css = _read_css()
        sidebar_idx = css.find(".wiki-sidebar {")
        assert sidebar_idx != -1, ".wiki-sidebar { block not found in CSS"
        block_end = css.find("}", sidebar_idx)
        sidebar_block = css[sidebar_idx:block_end]
        assert "resize: horizontal" not in sidebar_block, (
            ".wiki-sidebar must NOT have 'resize: horizontal' — replaced by custom drag handle"
        )

    def test_wiki_sidebar_has_max_width_50vw(self):
        """AC3: .wiki-sidebar must have max-width: 50vw (not fixed 500px)."""
        css = _read_css()
        sidebar_idx = css.find(".wiki-sidebar {")
        assert sidebar_idx != -1
        block_end = css.find("}", sidebar_idx)
        sidebar_block = css[sidebar_idx:block_end]
        assert "50vw" in sidebar_block, (
            ".wiki-sidebar must have max-width: 50vw (AC3 constraint)"
        )

    def test_wiki_sidebar_has_min_width_180px(self):
        """AC3: .wiki-sidebar must keep min-width: 180px constraint."""
        css = _read_css()
        sidebar_idx = css.find(".wiki-sidebar {")
        assert sidebar_idx != -1
        block_end = css.find("}", sidebar_idx)
        sidebar_block = css[sidebar_idx:block_end]
        assert "180px" in sidebar_block, (
            ".wiki-sidebar must keep min-width: 180px"
        )

    def test_wiki_sidebar_handle_hover_uses_css_variable(self):
        """AC6: Handle hover style must use CSS custom property (theme-aware)."""
        css = _read_css()
        handle_idx = css.find(".wiki-sidebar-handle")
        assert handle_idx != -1, ".wiki-sidebar-handle not found in CSS"
        # Check that a CSS variable (--pico-*) is used in the handle section
        handle_section = css[handle_idx:handle_idx + 500]
        assert "var(--pico-" in handle_section, (
            "Handle styles must use CSS custom properties for theme compatibility (AC6)"
        )

    def test_wiki_sidebar_resizing_class_exists(self):
        """AC2: .wiki-sidebar.resizing or .resizing CSS class must exist for drag UX."""
        css = _read_css()
        assert ".wiki-sidebar.resizing" in css or ".resizing" in css, (
            ".wiki-sidebar.resizing or .resizing class must be defined for drag UX"
        )

    def test_wiki_sidebar_has_position_for_handle_absolute_positioning(self):
        """AC2: .wiki-sidebar must have position: relative or sticky for handle placement."""
        css = _read_css()
        sidebar_idx = css.find(".wiki-sidebar {")
        assert sidebar_idx != -1
        block_end = css.find("}", sidebar_idx)
        sidebar_block = css[sidebar_idx:block_end]
        has_relative = "position: relative" in sidebar_block or "position:relative" in sidebar_block
        has_sticky = "position: sticky" in sidebar_block or "position:sticky" in sidebar_block
        assert has_relative or has_sticky, (
            ".wiki-sidebar must have position: relative or position: sticky "
            "so the absolute-positioned handle works correctly"
        )


# ---------------------------------------------------------------------------
# JavaScript validation tests (AC2, AC3, AC4, AC5)
# ---------------------------------------------------------------------------


class TestJavaScriptValidation:
    """Validate JavaScript for sidebar resize logic."""

    def test_init_sidebar_resize_function_exists(self):
        """AC2: initSidebarResize function must exist in wiki.js."""
        js = _read_js()
        assert "function initSidebarResize" in js, (
            "initSidebarResize function must be defined in wiki.js"
        )

    def test_init_sidebar_resize_is_called_in_init_block(self):
        """AC2: initSidebarResize() must be called in the initialization block."""
        js = _read_js()
        assert "initSidebarResize()" in js, (
            "initSidebarResize() must be called in the init block at the bottom of wiki.js"
        )

    def test_localStorage_key_wiki_sidebar_width_used(self):
        """AC4: localStorage key 'wiki_sidebar_width' must be used in wiki.js."""
        js = _read_js()
        assert "wiki_sidebar_width" in js, (
            "localStorage key 'wiki_sidebar_width' must be present in wiki.js (AC4)"
        )

    def test_min_width_180_constant_present(self):
        """AC3: Minimum sidebar width of 180 must be defined as a constant."""
        js = _read_js()
        assert "180" in js, (
            "Minimum width constant 180 (SIDEBAR_MIN) must be present in wiki.js (AC3)"
        )

    def test_max_ratio_0_5_constant_present(self):
        """AC3: Maximum ratio 0.5 (50% viewport width) must be defined as a constant."""
        js = _read_js()
        assert "0.5" in js, (
            "Maximum ratio constant 0.5 (SIDEBAR_MAX_RATIO) must be present in wiki.js (AC3)"
        )

    def test_default_width_280_constant_present(self):
        """AC5: Default width 280 must be defined as a constant."""
        js = _read_js()
        assert "280" in js, (
            "Default width constant 280 (SIDEBAR_DEFAULT) must be present in wiki.js (AC5)"
        )

    def test_request_animation_frame_used_in_mousemove(self):
        """AC2: requestAnimationFrame must be used in the mousemove handler for smooth dragging."""
        js = _read_js()
        assert "requestAnimationFrame" in js, (
            "requestAnimationFrame must be used in mousemove handler (AC2 — smooth dragging)"
        )

    def test_mouseup_handler_saves_to_local_storage(self):
        """AC4: mouseup handler must call localStorage.setItem for width persistence."""
        js = _read_js()
        assert "mouseup" in js, "mouseup event listener must be present in wiki.js"
        assert "localStorage.setItem" in js, (
            "localStorage.setItem must be called (AC4 — save width on drag end)"
        )

    def test_clamp_width_function_exists(self):
        """AC3: clampWidth function must exist to enforce min/max constraints."""
        js = _read_js()
        assert "clampWidth" in js, (
            "clampWidth function must exist in wiki.js (AC3 — enforces 180px min, 50vw max)"
        )

    def test_window_resize_listener_recalculates_constraints(self):
        """AC3: Window resize event must recalculate and re-clamp sidebar width."""
        js = _read_js()
        assert 'window.addEventListener' in js, (
            "window.addEventListener must be present in wiki.js"
        )
        assert '"resize"' in js or "'resize'" in js, (
            "window resize event listener must exist in wiki.js (AC3 — re-clamp on viewport change)"
        )

    def test_mousemove_only_acts_when_resizing(self):
        """AC2: mousemove handler must check isResizing flag before acting."""
        js = _read_js()
        assert "isResizing" in js, (
            "isResizing flag must be used to guard mousemove handler (AC2)"
        )

    def test_saved_width_is_applied_on_load(self):
        """AC4: Saved width from localStorage must be applied on page load."""
        js = _read_js()
        assert "localStorage.getItem" in js, (
            "localStorage.getItem must be called to restore saved width on page load (AC4)"
        )

    def test_setitem_appears_after_mouseup_handler_definition(self):
        """AC5: localStorage.setItem must only appear inside mouseup handler, not on init."""
        js = _read_js()
        set_item_idx = js.find("setItem(WIDTH_STORAGE_KEY")
        assert set_item_idx != -1, "localStorage.setItem(WIDTH_STORAGE_KEY not found"

        mouseup_idx = js.find("mouseup")
        assert mouseup_idx != -1, "mouseup not found"
        assert mouseup_idx < set_item_idx, (
            "localStorage.setItem must appear after mouseup handler definition "
            "(AC5 — only save after user actually resizes, not on page load)"
        )

    def test_init_sidebar_resize_called_after_init_sidebar_toggle(self):
        """AC2: initSidebarResize() must be called after initSidebarToggle() in init block."""
        js = _read_js()
        toggle_call_idx = js.find("initSidebarToggle()")
        resize_call_idx = js.find("initSidebarResize()")
        assert toggle_call_idx != -1, "initSidebarToggle() call not found"
        assert resize_call_idx != -1, "initSidebarResize() call not found"
        assert resize_call_idx > toggle_call_idx, (
            "initSidebarResize() must be called after initSidebarToggle()"
        )
