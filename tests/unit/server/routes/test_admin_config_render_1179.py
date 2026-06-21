"""Bug #1179 — Admin Config screen returns HTTP 500 (UndefinedError on
config.search_event_log) because _get_current_config() omits the
search_event_log section.

Root cause (Story #1159/#1160 regression): get_all_settings() was updated to
emit "search_event_log" and "export" sub-dicts, but _get_current_config() in
routes.py was never updated to surface them.  The Jinja template dereferences:

  config.search_event_log.search_event_log_retention_days  (line 2440/2464)
  config.search_event_log.export_retention_days            (line 2445/2471)

Because the `| default()` Jinja filter only guards the LEAF attribute (not the
missing intermediate section), an absent `config.search_event_log` raises
UndefinedError and returns HTTP 500 for every GET /admin/config and every
POST /admin/config/{section} (which re-renders the page).

Tests written BEFORE the fix (TDD RED phase) -- they MUST fail on unpatched
code, then pass after the fix.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Template path constant (used by completeness guard and render tests)
# ---------------------------------------------------------------------------

_CONFIG_SECTION_TEMPLATE = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "partials"
    / "config_section.html"
)

# Minimum rendered HTML length in characters for the config section partial.
# The template produces ~205 KB of HTML for a default config; anything shorter
# than this constant indicates an unexpected truncation or empty render.
_MIN_RENDERED_CONFIG_HTML_CHARS = 10_000

# ---------------------------------------------------------------------------
# Helper: build a real ConfigService backed by a temp SQLite DB
# ---------------------------------------------------------------------------


def _make_service(tmp_dir: str):
    """Return a ConfigService instance backed by a real SQLite DB in tmp_dir."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    return ConfigService(config_manager=mgr)


def _call_get_current_config(svc) -> dict:
    """Call _get_current_config() with the given ConfigService patched as the
    singleton, so it reads from the temp DB instead of the live server config.
    """
    import unittest.mock as mock
    from code_indexer.server.web import routes

    # _get_current_config() imports and calls get_config_service() internally.
    # Patch the singleton at the source so the import-inside-function call
    # gets our test service.
    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        result: dict = routes._get_current_config()
        return result


def _scan_template_config_sections() -> set:
    """Programmatically derive the set of top-level config.<section> names
    actually dereferenced by config_section.html.

    Uses a regex scan of the template source for ``config\\.([a-z_]+)``
    occurrences.  This is the canonical source of truth -- the test
    completeness guard uses this function so it can never drift from the
    real template again (Bug #1179 was partly caused by the prior hand-
    maintained list missing 'wiki_config' and including phantom entries
    'api_limits', 'error_handling', 'git_timeouts', 'web_security').
    """
    content = _CONFIG_SECTION_TEMPLATE.read_text()
    matches = re.findall(r"config\.([a-z_]+)", content)
    return set(matches)


# ---------------------------------------------------------------------------
# Section 1: _get_current_config() must include the search_event_log section
# ---------------------------------------------------------------------------


class TestGetCurrentConfigSearchEventLogSection:
    """_get_current_config() must include 'search_event_log' with both fields."""

    def test_search_event_log_key_present(self, tmp_path) -> None:
        """_get_current_config() must return a 'search_event_log' top-level key.

        Without the fix this key is absent and the template raises UndefinedError,
        causing HTTP 500 for every admin config page render.
        """
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "search_event_log" in result, (
            "'search_event_log' missing from _get_current_config() return dict. "
            "The Jinja template dereferences config.search_event_log.* which raises "
            "UndefinedError (HTTP 500) when this key is absent."
        )

    def test_search_event_log_retention_days_present(self, tmp_path) -> None:
        """search_event_log dict must contain search_event_log_retention_days."""
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result.get("search_event_log", {})
        assert "search_event_log_retention_days" in section, (
            "'search_event_log_retention_days' missing from "
            "_get_current_config()['search_event_log']. "
            "Template line 2440/2464 dereferences this field."
        )

    def test_search_event_log_retention_days_default_is_90(self, tmp_path) -> None:
        """Default value for search_event_log_retention_days must be 90."""
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result.get("search_event_log", {})
        val = section.get("search_event_log_retention_days")
        assert val == 90, (
            f"Expected search_event_log_retention_days==90 (template default), got {val!r}"
        )

    def test_export_retention_days_present_under_search_event_log(
        self, tmp_path
    ) -> None:
        """search_event_log dict must also contain export_retention_days.

        The template (line 2445/2471) dereferences
        config.search_event_log.export_retention_days -- both fields are rendered
        inside the same <details id="section-search_event_log"> block.
        """
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result.get("search_event_log", {})
        assert "export_retention_days" in section, (
            "'export_retention_days' missing from "
            "_get_current_config()['search_event_log']. "
            "Template line 2445/2471 dereferences config.search_event_log.export_retention_days."
        )

    def test_export_retention_days_default_is_30(self, tmp_path) -> None:
        """Default value for export_retention_days must be 30."""
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result.get("search_event_log", {})
        val = section.get("export_retention_days")
        assert val == 30, (
            f"Expected export_retention_days==30 (template default), got {val!r}"
        )


# ---------------------------------------------------------------------------
# Section 2: Completeness guard -- every template top-level section must be
# present in _get_current_config()
# ---------------------------------------------------------------------------
#
# The required section set is derived PROGRAMMATICALLY by scanning the real
# template for `config.<section>` references (see _scan_template_config_sections).
# This replaces the previous hand-maintained list which had drifted: it was
# missing 'wiki_config' (present in the template) and included phantom entries
# 'api_limits', 'error_handling', 'git_timeouts', 'web_security' (not present
# as top-level config.<section> dereferences in the template).
#
# With programmatic derivation, any future template section that forgets to
# update _get_current_config() will be caught automatically.


class TestGetCurrentConfigSectionCompleteness:
    """Every section the Jinja template dereferences must be present in the dict
    returned by _get_current_config().  This guard prevents future Story
    regressions where a new template section is added but _get_current_config()
    is not updated.
    """

    def test_all_required_sections_present(self, tmp_path) -> None:
        """_get_current_config() must contain every section the template uses.

        The required set is derived by regex-scanning config_section.html for
        config.<section> dereferences, making this guard self-maintaining.
        Missing sections cause Jinja UndefinedError -> HTTP 500.
        """
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        required = _scan_template_config_sections()
        missing = required - set(result.keys())
        assert not missing, (
            f"_get_current_config() is missing sections required by the Jinja "
            f"template (admin Config page will 500): {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Section 3: Render-level regression tests
#
# The dict-level tests above prove _get_current_config() returns the right
# keys, but they do NOT exercise the Jinja render path.  A future template
# section could be added that _get_current_config() forgets, and the dict
# tests would NOT catch it until _scan_template_config_sections() is exercised.
# These render tests close that gap: they render the REAL template through
# the application's real Jinja environment (routes.templates.env) to assert
# the render does not raise UndefinedError.
#
# Why routes.templates.env and not a freshly constructed jinja2.Environment:
#   routes.templates.env is the exact production Jinja environment -- same
#   loader, filters, globals (enumerate, get_server_time, static_version),
#   undefined class, and autoescape settings.  Using it validates that the
#   full production render pipeline does not raise, not just an approximation.
# ---------------------------------------------------------------------------


def _build_render_context(config: dict) -> dict:
    """Build the minimal template context matching config_section_partial().

    The context matches what routes.config_section_partial() passes to
    partials/config_section.html (verified against the real handler at
    routes.py:8938-8948).  Non-config variables are stubbed with safe
    empty values so only config-key absence can trigger UndefinedError.
    """
    return {
        "request": None,
        "csrf_token": "test_csrf_token",
        "config": config,
        "validation_errors": {},
        "restart_required_fields": [],
        "api_keys_status": {},
        "github_token_data": None,
        "gitlab_token_data": None,
    }


class TestConfigSectionTemplateRender:
    """Render-level regression tests: exercise the actual Jinja template render
    with the dict produced by _get_current_config(), proving Jinja does not
    raise UndefinedError.

    The app's Jinja environment (routes.templates.env) uses the default
    jinja2.Undefined class, which DOES raise UndefinedError when accessing an
    attribute on a missing dict key (e.g. ``config.search_event_log.field``
    when ``search_event_log`` is absent -- confirmed by inspection of
    routes.templates.env.undefined).  Both the positive and the negative test
    use the same production env so the behaviour matches production exactly.
    """

    def test_render_with_real_config_does_not_raise(self, tmp_path) -> None:
        """Rendering config_section.html with the real _get_current_config()
        dict must succeed without raising UndefinedError.

        This is the definitive regression test for Bug #1179: before the fix
        the render raised ``UndefinedError: 'dict object' has no attribute
        'search_event_log'``.  After the fix it must produce a non-empty HTML
        string.

        Unlike the dict-level tests, this test exercises the real Jinja render
        pipeline, so it will also catch future template additions that
        _get_current_config() forgets to cover.
        """
        import jinja2
        from code_indexer.server.web import routes

        svc = _make_service(str(tmp_path))
        config = _call_get_current_config(svc)

        # Use the application's real Jinja environment -- same loader, filters,
        # globals, and undefined class as production.
        template = routes.templates.env.get_template("partials/config_section.html")
        ctx = _build_render_context(config)

        # Must not raise -- if it does, the bug has regressed.
        try:
            html = template.render(**ctx)
        except jinja2.UndefinedError as exc:
            raise AssertionError(
                f"config_section.html raised UndefinedError during render: {exc}\n"
                "This means _get_current_config() is missing a section that the "
                "template dereferences.  Bug #1179 has regressed."
            ) from exc

        assert html, "Template render produced empty output -- unexpected."
        assert len(html) > _MIN_RENDERED_CONFIG_HTML_CHARS, (
            f"Template render output suspiciously short ({len(html)} chars). "
            f"Expected at least {_MIN_RENDERED_CONFIG_HTML_CHARS} chars for a "
            "full config section render."
        )

    def test_render_without_search_event_log_raises_undefined_error(
        self, tmp_path
    ) -> None:
        """RED-guard: removing 'search_event_log' from the config dict must
        cause the Jinja render to raise UndefinedError.

        This negative test proves the render test above actually exercises the
        failure mode.  If this test ever STOPS raising, it means Jinja's
        behaviour changed or the template no longer dereferences
        config.search_event_log -- and the test suite must be re-evaluated.
        """
        import jinja2
        import pytest
        from code_indexer.server.web import routes

        svc = _make_service(str(tmp_path))
        config = _call_get_current_config(svc)

        # Remove the key that Bug #1179 was about.
        config_without_sel = {
            k: v for k, v in config.items() if k != "search_event_log"
        }
        assert "search_event_log" not in config_without_sel, (
            "Setup error: search_event_log should be absent from the mutated dict"
        )

        # Use the application's real Jinja environment -- same as production.
        template = routes.templates.env.get_template("partials/config_section.html")
        ctx = _build_render_context(config_without_sel)

        with pytest.raises(jinja2.UndefinedError):
            template.render(**ctx)


# ---------------------------------------------------------------------------
# Section 4: Verify that _get_current_config() reflects saved values
# (regression guard: values must come from real config, not hardcoded defaults)
# ---------------------------------------------------------------------------


class TestGetCurrentConfigSearchEventLogReflectsSaved:
    """When search_event_log settings are saved to the DB, _get_current_config()
    must return those saved values (not stale defaults).
    """

    def test_saved_retention_days_reflected(self, tmp_path) -> None:
        """After saving search_event_log_retention_days=180, _get_current_config
        must return 180 (not the default 90).
        """
        svc = _make_service(str(tmp_path))
        svc.update_setting("search_event_log", "search_event_log_retention_days", 180)

        result = _call_get_current_config(svc)
        val = result.get("search_event_log", {}).get("search_event_log_retention_days")
        assert val == 180, (
            f"Expected saved value 180 but got {val!r}. "
            "_get_current_config() must read from real config, not return hardcoded defaults."
        )

    def test_saved_export_retention_days_reflected(self, tmp_path) -> None:
        """After saving export_retention_days=60, _get_current_config must
        return 60 (not the default 30).
        """
        svc = _make_service(str(tmp_path))
        svc.update_setting("export", "export_retention_days", 60)

        result = _call_get_current_config(svc)
        val = result.get("search_event_log", {}).get("export_retention_days")
        assert val == 60, (
            f"Expected saved value 60 but got {val!r}. "
            "_get_current_config() must reflect real saved config values."
        )
