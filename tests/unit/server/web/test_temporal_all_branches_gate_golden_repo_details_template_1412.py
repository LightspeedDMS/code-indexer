"""Unit tests for Story #1412 - golden_repo_details.html must disable/hide
the all-branches checkbox with an explanatory note when the server-wide
temporal_all_branches_enabled gate is off.

Renders the real template with a controlled context via a Jinja2 Environment
pointed at the templates directory, mirroring the pattern in
tests/unit/server/web/test_dashboard_provider_results.py.
"""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates"
)


@pytest.fixture()
def jinja_env():
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


@pytest.fixture()
def details_template(jinja_env):
    return jinja_env.get_template("partials/golden_repo_details.html")


def _make_repo(alias: str = "my-repo") -> dict:
    return {
        "alias": alias,
        "repo_url": "git+https://example.test/org/repo",
        "default_branch": "main",
        "status": "ready",
        "created_at": "2024-01-01T00:00:00",
        "wiki_enabled": False,
        "temporal_options": None,
        "has_semantic": False,
        "has_fts": False,
        "has_temporal": False,
        "has_scip": False,
    }


def _extract_checkbox_html(html: str) -> str:
    """Extract the <input> tag for the all_branches checkbox from rendered HTML."""
    checkbox_pos = html.find('name="all_branches"')
    assert checkbox_pos != -1, "all_branches checkbox not found in rendered HTML"
    input_start = html.rfind("<input", 0, checkbox_pos)
    input_end = html.find(">", checkbox_pos)
    return html[input_start : input_end + 1]


class TestAllBranchesCheckboxGateOff:
    """Gate off -> checkbox disabled + explanatory note."""

    def test_checkbox_is_disabled(self, details_template) -> None:
        html = details_template.render(
            repo=_make_repo(),
            csrf_token="tok",
            temporal_all_branches_enabled=False,
        )
        checkbox_html = _extract_checkbox_html(html)
        assert "disabled" in checkbox_html, (
            f"Checkbox must be disabled when gate is off. Got: {checkbox_html}"
        )

    def test_explanatory_note_present(self, details_template) -> None:
        html = details_template.render(
            repo=_make_repo(),
            csrf_token="tok",
            temporal_all_branches_enabled=False,
        )
        assert "disabled" in html.lower() and (
            "administrator" in html.lower() or "server config" in html.lower()
        ), "Expected an explanatory note about the gate being disabled by server config"


class TestAllBranchesCheckboxGateOn:
    """Gate on -> checkbox enabled (unchanged behavior)."""

    def test_checkbox_is_not_disabled(self, details_template) -> None:
        html = details_template.render(
            repo=_make_repo(),
            csrf_token="tok",
            temporal_all_branches_enabled=True,
        )
        checkbox_html = _extract_checkbox_html(html)
        assert "disabled" not in checkbox_html, (
            f"Checkbox must NOT be disabled when gate is on. Got: {checkbox_html}"
        )
