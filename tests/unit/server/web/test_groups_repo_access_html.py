"""
Unit tests for Story #686: HTML structure of groups_repo_access partial.

Tests that:
- C2: Toggle button with id="toggle-grouped" has onclick="toggleGroupedView()"
       and default text "Group by Category"
- C3: Repo <tr> rows carry data-repo-alias, data-category-name, data-category-priority
       on the same element (verified via BeautifulSoup element parsing)
- C5: applyStoredGroupedView() bootstrap call is the only non-whitespace content
       after the final </style> tag
- C6: repo_categories.js _getGroupedStorageKey() function body contains both
       .repo-access-table selector and the dedicated key 'cidx-groups-repo-access-grouped'
- C7: toggleGroupedView() and applyStoredGroupedView() function bodies contain .repo-access-table
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path
import tempfile
from bs4 import BeautifulSoup

# Repository root derived from test file location — established convention in this test suite
# (see test_dashboard_chart.py for prior art)
REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent

_JS_FILE = (
    REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "static"
    / "js"
    / "repo_categories.js"
)

ENDPOINT = "/admin/partials/groups-repo-access"


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    from code_indexer.server.services.group_access_manager import GroupAccessManager

    return GroupAccessManager(temp_db_path)


@pytest.fixture
def test_client(group_manager):
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    app.state.group_manager = group_manager

    mock_session = MagicMock()
    mock_session.username = "admin_user"
    mock_session.role = "admin"

    with patch("code_indexer.server.web.routes._require_admin_session") as mock_auth:
        mock_auth.return_value = mock_session
        with patch("code_indexer.server.web.routes._get_group_manager") as mock_gm:
            mock_gm.return_value = group_manager
            with patch(
                "code_indexer.server.web.routes.get_csrf_token_from_cookie"
            ) as mock_csrf:
                mock_csrf.return_value = "test-csrf-token"
                yield TestClient(app)


def _fetch_partial(test_client, repos=None, repo_map=None):
    """Fetch the partial with given repos and category map."""
    if repos is None:
        repos = [{"alias": "repo-a"}]

    mock_grm = MagicMock()
    mock_grm.list_golden_repos.return_value = repos

    mock_svc = MagicMock()
    mock_svc.get_repo_category_map.return_value = repo_map or {}

    with (
        patch(
            "code_indexer.server.web.routes._get_golden_repo_manager"
        ) as mock_grm_patch,
        patch(
            "code_indexer.server.web.routes._get_repo_category_service"
        ) as mock_svc_patch,
        patch("code_indexer.server.web.routes.set_csrf_cookie"),
    ):
        mock_grm_patch.return_value = mock_grm
        mock_svc_patch.return_value = mock_svc
        return test_client.get(ENDPOINT)


def _extract_function_body(js_text: str, function_name: str) -> str:
    """
    Extract the body of a top-level JS function by finding its opening brace
    and scanning for the matching closing brace.
    Returns the text from the opening brace to the matching closing brace inclusive.
    """
    marker = f"function {function_name}("
    start = js_text.find(marker)
    assert start != -1, f"Function {function_name} not found in JS"
    brace_start = js_text.index("{", start)
    depth = 0
    for i in range(brace_start, len(js_text)):
        if js_text[i] == "{":
            depth += 1
        elif js_text[i] == "}":
            depth -= 1
            if depth == 0:
                return js_text[brace_start : i + 1]
    raise AssertionError(f"Unbalanced braces in function {function_name}")


class TestToggleButton:
    """C2: Toggle button must be present with correct onclick and default text."""

    def test_toggle_button_has_onclick_toggle_grouped_view(self, test_client):
        """Button with id='toggle-grouped' has onclick='toggleGroupedView()'."""
        response = _fetch_partial(test_client)
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, "html.parser")
        btn = soup.find(id="toggle-grouped")
        assert btn is not None, "Button with id='toggle-grouped' not found"
        assert btn.get("onclick") == "toggleGroupedView()", (
            f"Expected onclick='toggleGroupedView()' but got {btn.get('onclick')!r}"
        )

    def test_toggle_button_default_text_is_group_by_category(self, test_client):
        """Button with id='toggle-grouped' shows 'Group by Category' as default text."""
        response = _fetch_partial(test_client)
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, "html.parser")
        btn = soup.find(id="toggle-grouped")
        assert btn is not None, "Button with id='toggle-grouped' not found"
        assert "Group by Category" in btn.get_text(), (
            f"Expected 'Group by Category' in button text but got {btn.get_text()!r}"
        )


class TestRepoRowDataAttributes:
    """C3: Repo <tr> rows must carry all three data-* attributes on the same element."""

    def test_repo_tr_carries_all_three_data_attributes(self, test_client):
        """A single <tr> row has data-repo-alias, data-category-name, and data-category-priority."""
        response = _fetch_partial(test_client, repos=[{"alias": "my-repo"}])
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, "html.parser")
        repo_rows = soup.find_all("tr", attrs={"data-repo-alias": True})
        assert len(repo_rows) > 0, "No <tr> with data-repo-alias found"

        row = repo_rows[0]
        assert row.get("data-repo-alias") == "my-repo", (
            f"Expected data-repo-alias='my-repo' but got {row.get('data-repo-alias')!r}"
        )
        assert row.has_attr("data-category-name"), (
            "<tr> is missing data-category-name attribute"
        )
        assert row.has_attr("data-category-priority"), (
            "<tr> is missing data-category-priority attribute"
        )


class TestBootstrapScript:
    """C5: applyStoredGroupedView() bootstrap call must be the only content after </style>."""

    def test_apply_stored_grouped_view_is_only_content_after_style(self, test_client):
        """After the final </style> tag, the only non-whitespace content is the bootstrap script."""
        response = _fetch_partial(test_client)
        assert response.status_code == 200

        parts = response.text.rsplit("</style>", maxsplit=1)
        assert len(parts) == 2, "No </style> tag found in response"
        after_style = parts[1].strip()
        assert after_style == "<script>applyStoredGroupedView();</script>", (
            f"Expected only bootstrap script after </style>, got: {after_style!r}"
        )


class TestJavaScriptDetectionChains:
    """C6/C7: repo_categories.js function bodies must reference .repo-access-table."""

    def _read_js(self):
        return _JS_FILE.read_text()

    def test_get_grouped_storage_key_links_repo_access_table_to_dedicated_key(self):
        """_getGroupedStorageKey function body links .repo-access-table to cidx-groups-repo-access-grouped."""
        js = self._read_js()
        body = _extract_function_body(js, "_getGroupedStorageKey")

        assert ".repo-access-table" in body, (
            "_getGroupedStorageKey does not reference .repo-access-table"
        )
        assert "cidx-groups-repo-access-grouped" in body, (
            "_getGroupedStorageKey does not return 'cidx-groups-repo-access-grouped'"
        )

    def test_toggle_grouped_view_detects_repo_access_table(self):
        """toggleGroupedView function body includes .repo-access-table in table detection."""
        js = self._read_js()
        body = _extract_function_body(js, "toggleGroupedView")

        assert ".repo-access-table" in body, (
            "toggleGroupedView does not reference .repo-access-table"
        )

    def test_apply_stored_grouped_view_detects_repo_access_table(self):
        """applyStoredGroupedView function body includes .repo-access-table in table detection."""
        js = self._read_js()
        body = _extract_function_body(js, "applyStoredGroupedView")

        assert ".repo-access-table" in body, (
            "applyStoredGroupedView does not reference .repo-access-table"
        )
