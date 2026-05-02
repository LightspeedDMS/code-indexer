"""
Unit tests for Bug #943: TOTP step-up elevation runtime config (kill switch + timeouts)
missing from Web UI Config Screen.

Story #923 AC gap remediation. The three fields already exist on ServerConfig
(elevation_enforcement_enabled, elevation_idle_timeout_seconds, elevation_max_age_seconds)
but no Web UI control existed before this fix.
"""

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import ConfigService

# ---------------------------------------------------------------------------
# Named constants — bounds from the brief, matching ServerConfig defaults
# ---------------------------------------------------------------------------

IDLE_MIN = 60
IDLE_MAX = 3600
IDLE_DEFAULT = 300

MAX_AGE_MIN = 300
MAX_AGE_MAX = 7200
MAX_AGE_DEFAULT = 1800

KILL_SWITCH_DEFAULT = False

IDLE_VALID = 600
MAX_AGE_VALID = 3600

IDLE_BELOW_MIN = 30
IDLE_ABOVE_MAX = 4000
MAX_AGE_BELOW_MIN = 100
MAX_AGE_ABOVE_MAX = 8000

IDLE_FOR_CROSS_FIELD = 1200
MAX_AGE_CROSS_FIELD = (
    600  # less than IDLE_FOR_CROSS_FIELD, triggers cross-field rejection
)

# HTML tag length constants — no magic numbers in parser logic
DETAILS_OPEN_TAG_LEN = len("<details")
DETAILS_CLOSE_TAG_LEN = len("</details>")


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _read_template() -> str:
    """Read config_section.html template content."""
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    )
    return template_path.read_text()


def _read_totp_elevation_section() -> str:
    """Return the isolated totp_elevation <details>...</details> block."""
    html = _read_template()
    section_start = -1
    pos = html.find("<details")
    while pos != -1:
        tag_end = html.find(">", pos)
        if tag_end == -1:
            break
        if "section-totp_elevation" in html[pos : tag_end + 1]:
            section_start = pos
            break
        pos = html.find("<details", pos + 1)
    assert section_start != -1, (
        "No <details element with 'section-totp_elevation' in its opening tag found "
        "in config_section.html"
    )
    depth = 0
    i = section_start
    while i < len(html):
        if html[i : i + DETAILS_OPEN_TAG_LEN] == "<details":
            depth += 1
            i += DETAILS_OPEN_TAG_LEN
        elif html[i : i + DETAILS_CLOSE_TAG_LEN] == "</details>":
            depth -= 1
            if depth == 0:
                return html[section_start : i + DETAILS_CLOSE_TAG_LEN]
            i += DETAILS_CLOSE_TAG_LEN
        else:
            i += 1
    raise AssertionError(
        "Unclosed <details section-totp_elevation block in config_section.html"
    )


def _extract_input_element(html: str, input_name: str) -> str:
    """Return the text of the <input element whose name matches input_name."""
    for quote in ('"', "'"):
        search = f"name={quote}{input_name}{quote}"
        pos = html.find(search)
        if pos != -1:
            input_start = html.rfind("<input", 0, pos)
            assert input_start != -1, f"No opening <input before name='{input_name}'"
            input_end = html.find(">", input_start)
            assert input_end != -1, f"No closing > after <input name='{input_name}'"
            return html[input_start : input_end + 1]
    raise AssertionError(f"No <input with name='{input_name}' found in HTML block")


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def totp_elevation_section_html() -> str:
    """Load and cache the totp_elevation <details> block once per module."""
    return _read_totp_elevation_section()


# ---------------------------------------------------------------------------
# Class 2: ConfigService GET view-model tests
# ---------------------------------------------------------------------------


class TestConfigServiceTotpElevationView:
    """get_all_settings() must return a 'totp_elevation' key with the 3 fields."""

    def test_get_settings_includes_totp_elevation_block(self, tmp_path):
        """get_all_settings() returns a 'totp_elevation' key with all 3 fields."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "totp_elevation" in settings, (
            f"'totp_elevation' key missing from get_all_settings(). Keys: {list(settings)}"
        )
        block = settings["totp_elevation"]
        assert "elevation_enforcement_enabled" in block
        assert "elevation_idle_timeout_seconds" in block
        assert "elevation_max_age_seconds" in block

    def test_default_values_match_dataclass_defaults(self, tmp_path):
        """Default values are False / 300 / 1800 matching ServerConfig dataclass."""
        service = ConfigService(server_dir_path=str(tmp_path))
        block = service.get_all_settings()["totp_elevation"]
        assert block["elevation_enforcement_enabled"] == KILL_SWITCH_DEFAULT
        assert block["elevation_idle_timeout_seconds"] == IDLE_DEFAULT
        assert block["elevation_max_age_seconds"] == MAX_AGE_DEFAULT


# ---------------------------------------------------------------------------
# Class 3: ConfigService update tests
# ---------------------------------------------------------------------------


class TestConfigServiceTotpElevationUpdate:
    """update_setting('totp_elevation', key, value) must validate and persist."""

    def test_update_kill_switch_to_true_persists(self, tmp_path):
        """update_setting with 'true' persists elevation_enforcement_enabled = True to disk."""
        svc1 = ConfigService(server_dir_path=str(tmp_path))
        svc1.update_setting("totp_elevation", "elevation_enforcement_enabled", "true")
        # Re-read via a fresh instance to confirm disk persistence
        svc2 = ConfigService(server_dir_path=str(tmp_path))
        block = svc2.get_all_settings()["totp_elevation"]
        assert block["elevation_enforcement_enabled"] is True

    def test_update_kill_switch_to_false_persists(self, tmp_path):
        """Round-trip back to False after setting True — confirmed via new instance."""
        svc1 = ConfigService(server_dir_path=str(tmp_path))
        svc1.update_setting("totp_elevation", "elevation_enforcement_enabled", "true")
        svc1.update_setting("totp_elevation", "elevation_enforcement_enabled", "false")
        svc2 = ConfigService(server_dir_path=str(tmp_path))
        block = svc2.get_all_settings()["totp_elevation"]
        assert block["elevation_enforcement_enabled"] is False

    def test_idle_timeout_in_range_persists(self, tmp_path):
        """Setting idle timeout to IDLE_VALID (600) is accepted and persisted to disk."""
        svc1 = ConfigService(server_dir_path=str(tmp_path))
        svc1.update_setting(
            "totp_elevation", "elevation_idle_timeout_seconds", IDLE_VALID
        )
        svc2 = ConfigService(server_dir_path=str(tmp_path))
        block = svc2.get_all_settings()["totp_elevation"]
        assert block["elevation_idle_timeout_seconds"] == IDLE_VALID

    def test_idle_timeout_below_min_rejected(self, tmp_path):
        """idle_timeout below IDLE_MIN (30) raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_idle_timeout_seconds", IDLE_BELOW_MIN
            )

    def test_idle_timeout_above_max_rejected(self, tmp_path):
        """idle_timeout above IDLE_MAX (4000) raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_idle_timeout_seconds", IDLE_ABOVE_MAX
            )

    def test_max_age_in_range_persists(self, tmp_path):
        """Setting max_age to MAX_AGE_VALID (3600) is accepted and persisted to disk."""
        svc1 = ConfigService(server_dir_path=str(tmp_path))
        svc1.update_setting(
            "totp_elevation", "elevation_max_age_seconds", MAX_AGE_VALID
        )
        svc2 = ConfigService(server_dir_path=str(tmp_path))
        block = svc2.get_all_settings()["totp_elevation"]
        assert block["elevation_max_age_seconds"] == MAX_AGE_VALID

    def test_max_age_below_min_rejected(self, tmp_path):
        """max_age below MAX_AGE_MIN (100) raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_max_age_seconds", MAX_AGE_BELOW_MIN
            )

    def test_max_age_above_max_rejected(self, tmp_path):
        """max_age above MAX_AGE_MAX (8000) raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_max_age_seconds", MAX_AGE_ABOVE_MAX
            )

    def test_max_age_lt_idle_rejected(self, tmp_path):
        """max_age < idle_timeout raises ValueError (cross-field invariant)."""
        service = ConfigService(server_dir_path=str(tmp_path))
        # First set idle to IDLE_FOR_CROSS_FIELD (1200)
        service.update_setting(
            "totp_elevation", "elevation_idle_timeout_seconds", IDLE_FOR_CROSS_FIELD
        )
        # Now set max_age below idle (600 < 1200) — must fail
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_max_age_seconds", MAX_AGE_CROSS_FIELD
            )

    def test_kill_switch_string_coercion_true(self, tmp_path):
        """'true', 'on', '1' all coerce to True (in-memory verification sufficient for coercion)."""
        for truthy in ("true", "on", "1"):
            service = ConfigService(server_dir_path=str(tmp_path))
            service.update_setting(
                "totp_elevation", "elevation_enforcement_enabled", truthy
            )
            block = service.get_all_settings()["totp_elevation"]
            assert block["elevation_enforcement_enabled"] is True, (
                f"Expected True for '{truthy}'"
            )

    def test_kill_switch_string_coercion_false(self, tmp_path):
        """'false', 'off', '0' all coerce to False."""
        for falsy in ("false", "off", "0"):
            service = ConfigService(server_dir_path=str(tmp_path))
            # Set to True first so we can confirm the coercion back to False
            service.update_setting(
                "totp_elevation", "elevation_enforcement_enabled", "true"
            )
            service.update_setting(
                "totp_elevation", "elevation_enforcement_enabled", falsy
            )
            block = service.get_all_settings()["totp_elevation"]
            assert block["elevation_enforcement_enabled"] is False, (
                f"Expected False for '{falsy}'"
            )

    def test_kill_switch_invalid_string_rejected(self, tmp_path):
        """'yes' or 'maybe' raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_enforcement_enabled", "yes"
            )
        with pytest.raises(ValueError):
            service.update_setting(
                "totp_elevation", "elevation_enforcement_enabled", "maybe"
            )


# ---------------------------------------------------------------------------
# Class 1: Template structural tests (first 3 tests)
# ---------------------------------------------------------------------------


class TestTotpElevationSectionRenders:
    """Template must contain a totp_elevation <details> block with correct structure."""

    def test_section_present_with_correct_id(self, totp_elevation_section_html):
        """Template renders <details> with id containing 'section-totp_elevation'."""
        assert "section-totp_elevation" in totp_elevation_section_html

    def test_three_field_names_in_section(self, totp_elevation_section_html):
        """Display and edit modes reference all 3 field names."""
        assert "elevation_enforcement_enabled" in totp_elevation_section_html
        assert "elevation_idle_timeout_seconds" in totp_elevation_section_html
        assert "elevation_max_age_seconds" in totp_elevation_section_html

    def test_checkbox_for_enforcement_enabled(self, totp_elevation_section_html):
        """Edit form has a checkbox input for elevation_enforcement_enabled.

        The template renders TWO inputs for this field:
        1. A hidden fallback input (value="false") so the field is submitted
           even when the checkbox is unchecked.
        2. The real checkbox input (type="checkbox", value="true").

        The helper _extract_input_element finds the first match by name,
        which is the hidden fallback. We verify the checkbox exists in the
        section HTML directly.
        """
        assert 'type="checkbox"' in totp_elevation_section_html or (
            "type='checkbox'" in totp_elevation_section_html
        ), "No type='checkbox' found in totp_elevation section"
        assert "elevation_enforcement_enabled" in totp_elevation_section_html


# ---------------------------------------------------------------------------
# Shared fixture for ConfigService-based tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_svc(tmp_path):
    """Fresh ConfigService instance backed by a temporary directory."""
    return ConfigService(server_dir_path=str(tmp_path))


# ---------------------------------------------------------------------------
# Boundary tests — exact lower/upper limits (Fix #4, Codex brief)
# ---------------------------------------------------------------------------


class TestConfigServiceTotpElevationBoundaries:
    """Exact boundary values (min/max) must be accepted, not rejected."""

    def test_idle_timeout_exact_min_60_accepted(self, config_svc):
        """elevation_idle_timeout_seconds = 60 (exact lower bound) is accepted."""
        config_svc.update_setting(
            "totp_elevation", "elevation_idle_timeout_seconds", 60
        )
        block = config_svc.get_all_settings()["totp_elevation"]
        assert block["elevation_idle_timeout_seconds"] == 60

    def test_idle_timeout_exact_max_3600_accepted(self, config_svc):
        """elevation_idle_timeout_seconds = 3600 (exact upper bound) is accepted.

        max_age is raised to 7200 first so the cross-field invariant
        (max_age >= idle) is satisfied before setting idle=3600.
        """
        config_svc.update_setting("totp_elevation", "elevation_max_age_seconds", 7200)
        config_svc.update_setting(
            "totp_elevation", "elevation_idle_timeout_seconds", 3600
        )
        block = config_svc.get_all_settings()["totp_elevation"]
        assert block["elevation_idle_timeout_seconds"] == 3600

    def test_max_age_exact_min_300_accepted(self, config_svc):
        """elevation_max_age_seconds = 300 (exact lower bound) is accepted.

        Default idle=300, so 300 >= 300 satisfies cross-field invariant.
        """
        config_svc.update_setting("totp_elevation", "elevation_max_age_seconds", 300)
        block = config_svc.get_all_settings()["totp_elevation"]
        assert block["elevation_max_age_seconds"] == 300

    def test_max_age_exact_max_7200_accepted(self, config_svc):
        """elevation_max_age_seconds = 7200 (exact upper bound) is accepted."""
        config_svc.update_setting("totp_elevation", "elevation_max_age_seconds", 7200)
        block = config_svc.get_all_settings()["totp_elevation"]
        assert block["elevation_max_age_seconds"] == 7200


# ---------------------------------------------------------------------------
# Fix #1 — BLOCKER: Atomic batch validation for totp_elevation
# ---------------------------------------------------------------------------


class TestTotpElevationAtomicBatch:
    """update_totp_elevation_atomic validates the final tuple, not field-by-field."""

    def test_combined_post_idle_above_old_max_age_succeeds_when_new_max_age_valid(
        self, config_svc
    ):
        """POST idle=2000, max_age=2400 with old max_age=1800 succeeds via atomic path.

        BLOCKER 1 regression test. Per-field path rejects idle=2000 when current
        max_age=1800. Atomic path must accept because final tuple satisfies all
        invariants: 60<=2000<=3600, 300<=2400<=7200, 2400>=2000.
        """
        block = config_svc.get_all_settings()["totp_elevation"]
        assert block["elevation_max_age_seconds"] == 1800, (
            "Precondition: old max_age=1800"
        )

        config_svc.update_totp_elevation_atomic(
            enabled=False, idle_timeout_seconds=2000, max_age_seconds=2400
        )

        block2 = config_svc.get_all_settings()["totp_elevation"]
        assert block2["elevation_idle_timeout_seconds"] == 2000
        assert block2["elevation_max_age_seconds"] == 2400

    def test_save_failure_rolls_back_in_memory_kill_switch(self, tmp_path):
        """If config file is unwritable, elevation_enforcement_enabled rolls back.

        HIGH 1 regression test. Uses real filesystem permission (chmod 444 on
        the config file) as the external dependency that causes save to fail.
        No patching of the SUT — the failure is injected at the storage layer.
        """
        import os
        import stat

        svc = ConfigService(server_dir_path=str(tmp_path))
        original_enabled = svc.get_config().elevation_enforcement_enabled

        # Force config file into existence, then make it unwritable
        config_file = svc.get_config_file_path()
        # The config file may not exist yet (lazy write); trigger creation first
        svc.save_config(svc.get_config())
        assert os.path.exists(config_file), "Config file must exist after save_config()"

        # Remove write permission — external storage layer will refuse the write
        os.chmod(config_file, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            with pytest.raises(Exception):
                svc.update_totp_elevation_atomic(
                    enabled=(not original_enabled),
                    idle_timeout_seconds=300,
                    max_age_seconds=1800,
                )
            # In-memory kill switch must have rolled back
            assert svc.get_config().elevation_enforcement_enabled == original_enabled, (
                "In-memory kill switch was not rolled back after save failure"
            )
        finally:
            # Restore write permission so tmp_path cleanup can delete the file
            os.chmod(config_file, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Fix #2 — BLOCKER: ElevatedSessionManager.update_timeouts
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_session_mgr(tmp_path):
    """Real ElevatedSessionManager with initial idle=300, max_age=1800."""
    from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager

    return ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "el.db"),
    )


class TestElevatedSessionManagerHotReload:
    """ElevatedSessionManager.update_timeouts() propagates to the live instance."""

    def test_update_timeouts_propagates(self, real_session_mgr):
        """update_timeouts(600, 3600) updates _idle_timeout and _max_age in place."""
        real_session_mgr.update_timeouts(600, 3600)
        assert real_session_mgr._idle_timeout == 600
        assert real_session_mgr._max_age == 3600

    def test_update_timeouts_rejects_idle_above_max(self, real_session_mgr):
        """update_timeouts raises ValueError when idle > max_age."""
        with pytest.raises(ValueError):
            real_session_mgr.update_timeouts(
                idle_timeout_seconds=600, max_age_seconds=300
            )

    def test_update_timeouts_rejects_idle_out_of_range(self, real_session_mgr):
        """update_timeouts raises ValueError when idle > 3600 (above IDLE_MAX)."""
        with pytest.raises(ValueError):
            real_session_mgr.update_timeouts(
                idle_timeout_seconds=4000, max_age_seconds=7200
            )

    def test_config_save_invokes_hot_reload_on_real_manager(
        self, config_svc, real_session_mgr
    ):
        """update_totp_elevation_atomic calls update_timeouts on an injected real manager.

        Integration wiring test: a real ElevatedSessionManager is injected via
        the session_manager parameter (dependency injection, no mocking). After
        atomic save succeeds, the real manager's _idle_timeout and _max_age must
        reflect the new values — proving the wiring is real, not just asserted.
        """
        assert real_session_mgr._idle_timeout == 300, "Precondition: initial idle=300"
        assert real_session_mgr._max_age == 1800, "Precondition: initial max_age=1800"

        config_svc.update_totp_elevation_atomic(
            enabled=True,
            idle_timeout_seconds=600,
            max_age_seconds=3600,
            session_manager=real_session_mgr,
        )

        assert real_session_mgr._idle_timeout == 600, (
            f"Hot-reload wiring failed: _idle_timeout={real_session_mgr._idle_timeout}"
        )
        assert real_session_mgr._max_age == 3600, (
            f"Hot-reload wiring failed: _max_age={real_session_mgr._max_age}"
        )
