"""
Unit tests for Story #724 Phase C: post-generation verification pass config
fields on the Web UI Config Screen.

The fields (``dep_map_fact_check_enabled``, ``fact_check_timeout_seconds``)
are the cidx-depmap feature's settings: the config screen shows and edits
them only in the generic Feature Settings block (``feature_settings.html``),
never in the core's Claude CLI section. Tests cover:
- the feature section renders both fields with the timeout's 60..3600 bounds
  and the "Post-generation verification" label;
- the core's Claude CLI values and template carry neither field.
"""

from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.web.routes import _get_current_config
from tests.unit.server._depmap_feature import render_depmap_feature_settings

MIN_FACT_CHECK_TIMEOUT = 60
MAX_FACT_CHECK_TIMEOUT = 3600

_TEMPLATES = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)


def test_feature_settings_render_the_verification_pass_fields() -> None:
    html = render_depmap_feature_settings()
    assert 'name="setting__dep_map_fact_check_enabled"' in html
    assert "Post-generation verification pass (fact-check)" in html
    timeout = html[html.index('name="setting__fact_check_timeout_seconds"') :]
    timeout = timeout[: timeout.index(">")]
    assert f'min="{MIN_FACT_CHECK_TIMEOUT}"' in timeout
    assert f'max="{MAX_FACT_CHECK_TIMEOUT}"' in timeout


def test_core_claude_cli_section_has_no_verification_pass_fields() -> None:
    template = (_TEMPLATES / "partials" / "config_section.html").read_text()
    assert 'name="dep_map_fact_check_enabled"' not in template
    assert 'name="fact_check_timeout_seconds"' not in template
    with patch(
        "code_indexer.server.services.config_service.get_config_service"
    ) as mock_service:
        mock_cs = Mock()
        mock_cs.get_all_settings.return_value = {
            "server": {},
            "cache": {},
            "timeouts": {},
            "password_security": {},
        }
        mock_service.return_value = mock_cs
        claude_cli = _get_current_config()["claude_cli"]
    assert "dep_map_fact_check_enabled" not in claude_cli
    assert "fact_check_timeout_seconds" not in claude_cli
