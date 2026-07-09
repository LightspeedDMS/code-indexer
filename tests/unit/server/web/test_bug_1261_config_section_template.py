"""
Regression test for Bug #1261: Web UI config_section.html template must accept
0 (unlimited) as a valid value for the dependency-map pass2/delta max-turns
inputs, matching the relaxed ConfigService clamp (max(0, ...) instead of
max(5, ...)). A stale min="5" HTML attribute would silently block an operator
from entering 0 in the browser even though the backend now accepts it.
"""

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def template_html():
    template_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    return (template_dir / "partials" / "config_section.html").read_text()


def test_pass2_max_turns_input_min_is_zero(template_html):
    assert (
        'name="dependency_map_pass2_max_turns" value="{{ config.claude_cli.dependency_map_pass2_max_turns }}" min="0"'
        in template_html
    )


def test_delta_max_turns_input_min_is_zero(template_html):
    assert (
        'name="dependency_map_delta_max_turns" value="{{ config.claude_cli.dependency_map_delta_max_turns }}" min="0"'
        in template_html
    )
