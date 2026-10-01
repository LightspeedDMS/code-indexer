"""Route-level display tests for Issue #1749: the admin Config page's
Temporal Legacy Migration section must reflect the REAL persisted state,
not the compiled-in class default.

Root cause (see test_config_service_temporal_legacy_migration_1749.py for
the service-layer fix): `_temporal_legacy_migration_settings(config)`
(Issue #1548) was defined in config_service.py but never invoked from
`get_all_settings()`. `routes.py`'s
`settings.get("temporal_legacy_migration", asdict(TemporalLegacyMigrationConfig()))`
therefore always fell back to the class default (relocation_enabled=False,
cleanup_authorized=False), regardless of the true persisted state.

This is a display-layer bug only -- the underlying config and the scheduler
that reads it directly are unaffected. But because the corresponding update
handler (`_update_temporal_legacy_migration_setting`) IS correctly wired, an
admin who loads the Config page (sees "No", wrongly) and saves the edit form
WITHOUT changing that pre-filled dropdown will silently DISABLE a currently-
working relocation job. These tests prove both the display fix and that this
exact harm scenario is prevented.

Mirrors the established patterns in test_admin_config_render_1179.py
(_get_current_config() + real Jinja template render) and
test_config_section_alias_lock_1546.py (sibling section structural checks).
"""

import re
import unittest.mock as mock

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.web import routes


def _get_current_config_with(svc: ConfigService) -> dict:
    """Call routes._get_current_config() against a real ConfigService backed
    by a temp SQLite DB, mirroring test_admin_config_render_1179.py's
    established patching pattern.
    """
    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        return routes._get_current_config()


def _build_render_context(config: dict) -> dict:
    """Minimal template context matching config_section_partial() (verified
    against routes.py:8938-8948 by test_admin_config_render_1179.py)."""
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


def _extract_section(html: str) -> str:
    start = html.find('id="section-temporal-legacy-migration"')
    assert start != -1, "Missing Temporal Legacy Migration <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def _render_temporal_legacy_migration_section(config: dict) -> str:
    template = routes.templates.env.get_template("partials/config_section.html")
    html = template.render(**_build_render_context(config))
    return _extract_section(html)


@pytest.fixture
def svc_with_relocation_enabled(tmp_path) -> ConfigService:
    """A real ConfigService, backed by a temp SQLite DB, with
    relocation_enabled persisted as True -- the discriminating non-default
    state every test in this module needs (both fields default to False, so
    only a genuine non-default persisted value can prove the fix is real).
    """
    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    svc.update_setting("temporal_legacy_migration", "relocation_enabled", "true")
    return svc


@pytest.fixture
def rendered_section(svc_with_relocation_enabled: ConfigService) -> str:
    """The rendered Temporal Legacy Migration <details> HTML fragment, built
    from the real _get_current_config() dict via the real Jinja template.
    """
    config = _get_current_config_with(svc_with_relocation_enabled)
    return _render_temporal_legacy_migration_section(config)


class TestGetCurrentConfigReflectsPersistedTemporalLegacyMigration:
    """_get_current_config() -- the dict fed to the Jinja template -- must
    reflect the true persisted state, not the class default."""

    def test_reflects_persisted_relocation_enabled_true(
        self, svc_with_relocation_enabled: ConfigService
    ) -> None:
        config = _get_current_config_with(svc_with_relocation_enabled)

        assert config["temporal_legacy_migration"]["relocation_enabled"] is True, (
            "_get_current_config()['temporal_legacy_migration']['relocation_enabled'] "
            "does not reflect the persisted True value -- admin Config page "
            "would silently display 'No'."
        )


class TestConfigPageRenderReflectsPersistedTemporalLegacyMigration:
    """Render-level proof: the REAL Jinja template must display 'Yes', not
    the hardcoded default 'No', when relocation_enabled=True is persisted."""

    def test_display_cell_shows_yes_when_relocation_enabled_true(
        self, rendered_section: str
    ) -> None:
        assert re.search(
            r'Relocation Enabled</td>\s*<td class="config-value">Yes</td>',
            rendered_section,
        ), (
            "Config page display cell still shows 'No' for Relocation Enabled "
            "despite relocation_enabled=True being persisted."
        )


class TestSilentDisableHarmScenarioPrevented:
    """The exact harm scenario described in Issue #1749: an admin who saves
    the edit form WITHOUT changing the (previously wrongly pre-filled "No")
    dropdown must NOT silently disable a currently-enabled relocation job.
    """

    def test_edit_dropdown_preselects_true_option(self, rendered_section: str) -> None:
        """Before the fix, the <select> always pre-selected value="false"
        regardless of the real state. After the fix it must pre-select
        value="true" when relocation_enabled=True is persisted.
        """
        true_option = re.search(
            r'<option value="true"[^>]*>Yes</option>', rendered_section
        )
        false_option = re.search(
            r'<option value="false"[^>]*>No</option>', rendered_section
        )
        assert true_option is not None and "selected" in true_option.group(0), (
            "Edit-mode dropdown does not pre-select 'Yes' (true) despite "
            "relocation_enabled=True persisted -- resubmitting the form "
            "unchanged would silently disable relocation."
        )
        assert false_option is not None and "selected" not in false_option.group(0)

    def test_resubmit_form_without_changing_dropdown_preserves_true(
        self, svc_with_relocation_enabled: ConfigService, rendered_section: str
    ) -> None:
        """End-to-end harm-scenario proof: derive the value an admin's
        browser would resubmit (the template's own pre-selected <option>)
        directly from the real rendered HTML, feed it back through the exact
        same update_settings_atomic() call update_config_section() (the
        POST /admin/config/{section} handler) uses, and confirm
        relocation_enabled is STILL True afterward -- not silently flipped
        to False.
        """
        selected_option = re.search(
            r'<option value="(true|false)" selected>', rendered_section
        )
        assert selected_option is not None, (
            "No pre-selected option found in the relocation_enabled dropdown."
        )
        resubmitted_value = selected_option.group(1)

        # Simulate the admin clicking Save without touching the dropdown:
        # the browser resubmits exactly the pre-selected value, applied via
        # the same atomic path update_config_section() uses.
        svc_with_relocation_enabled.update_settings_atomic(
            [("temporal_legacy_migration", "relocation_enabled", resubmitted_value)]
        )

        final_settings = svc_with_relocation_enabled.get_all_settings()
        assert (
            final_settings["temporal_legacy_migration"]["relocation_enabled"] is True
        ), (
            "Resubmitting the config form without changing the dropdown "
            "silently flipped relocation_enabled to False -- the exact harm "
            "scenario described in Issue #1749."
        )
