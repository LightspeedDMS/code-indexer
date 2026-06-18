"""Tests for dashboard_stats.html thead / tbody column alignment.

Root-cause fix for the column-misalignment bug where:
- dashboard_stats.html <thead> had 4 columns
- dashboard_recent_jobs.html (HTMX refresh partial) renders 5 or 6 columns

After the fix:
- dashboard_stats.html <thead> has 5 columns (+ conditional 6th "Providers")
- Initial render uses the same partial via {% include %} (single source of truth)
- Column counts MUST match between <thead> and body rows for both provider/non-provider cases
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates"
)
PARTIALS_DIR = TEMPLATES_DIR / "partials"
DASHBOARD_STATS_TEMPLATE_NAME = "partials/dashboard_stats.html"
DASHBOARD_STATS_PATH = PARTIALS_DIR / "dashboard_stats.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_th(html: str) -> int:
    """Count <th> elements in rendered HTML (case-insensitive)."""
    return len(re.findall(r"<th[\s>]", html, re.IGNORECASE))


def _count_td_in_row(row_html: str) -> int:
    """Count <td> elements within a single table row."""
    return len(re.findall(r"<td[\s>]", row_html, re.IGNORECASE))


def _extract_colspan(html: str) -> Optional[int]:
    """Extract the first colspan value found in the HTML, or None."""
    match = re.search(r'colspan="(\d+)"', html)
    return int(match.group(1)) if match else None


def _make_job(
    job_id: str = "job1",
    repo_name: str = "my-repo",
    job_type: str = "full_index",
    completion_time: str = "2024-01-15T10:30:00Z",
    status: str = "completed",
    username: Optional[str] = None,
    actor_username: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "repo_name": repo_name,
        "job_type": job_type,
        "completion_time": completion_time,
        "status": status,
        "username": username,
        "actor_username": actor_username,
        "result": result if result is not None else {},
    }


def _make_provider_results() -> Dict[str, Any]:
    return {
        "voyage-ai": {"status": "success", "files_indexed": 100},
        "cohere": {"status": "failed", "files_indexed": 0, "error": "bad key"},
    }


@pytest.fixture()
def jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )


@pytest.fixture()
def stats_template(jinja_env: Environment):
    return jinja_env.get_template(DASHBOARD_STATS_TEMPLATE_NAME)


def _render_stats(
    stats_template,
    recent_jobs: Optional[List[Dict[str, Any]]] = None,
    has_provider_results: bool = False,
    **kwargs: Any,
) -> str:
    """Render dashboard_stats.html with minimal required context."""
    return str(
        stats_template.render(
            recent_jobs=recent_jobs or [],
            has_provider_results=has_provider_results,
            job_counts=None,
            time_filter="24h",
            api_filter="24h",
            cache_metrics=None,
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# Tests: thead column counts
# ---------------------------------------------------------------------------


class TestDashboardStatsTheadColumns:
    """<thead> in dashboard_stats.html must have 5 or 6 <th> elements."""

    def test_thead_has_five_columns_without_providers(self, stats_template):
        """Without provider_results, thead must contain exactly 5 <th> elements:
        Repository | Job Type | Completed | User | Status
        """
        html = _render_stats(stats_template, recent_jobs=[], has_provider_results=False)
        # Extract only the thead section to count headers
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        assert thead_match is not None, "No <thead> found in dashboard_stats.html"
        thead_html = thead_match.group(0)
        count = _count_th(thead_html)
        assert count == 5, (
            f"Expected 5 <th> elements in thead (no providers), got {count}. "
            f"thead HTML: {thead_html}"
        )

    def test_thead_has_six_columns_with_providers(self, stats_template):
        """With has_provider_results=True, thead must contain exactly 6 <th> elements:
        Repository | Job Type | Completed | User | Status | Providers
        """
        html = _render_stats(stats_template, recent_jobs=[], has_provider_results=True)
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        assert thead_match is not None, "No <thead> found in dashboard_stats.html"
        thead_html = thead_match.group(0)
        count = _count_th(thead_html)
        assert count == 6, (
            f"Expected 6 <th> elements in thead (with providers), got {count}. "
            f"thead HTML: {thead_html}"
        )

    def test_thead_has_six_columns_when_jobs_have_provider_results(
        self, stats_template
    ):
        """When recent_jobs contains provider_results, thead must have 6 <th> elements."""
        jobs = [_make_job(result={"provider_results": _make_provider_results()})]
        html = _render_stats(
            stats_template, recent_jobs=jobs, has_provider_results=False
        )
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        assert thead_match is not None
        thead_html = thead_match.group(0)
        count = _count_th(thead_html)
        assert count == 6, (
            f"Expected 6 <th> in thead when jobs have provider_results, got {count}"
        )

    def test_thead_contains_user_header(self, stats_template):
        """thead must contain a 'User' column header (AC12: actor/username column)."""
        html = _render_stats(stats_template)
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        assert thead_match is not None
        thead_html = thead_match.group(0)
        assert "User" in thead_html, (
            f"Expected 'User' header in thead. thead HTML: {thead_html}"
        )

    def test_thead_contains_providers_header_when_active(self, stats_template):
        """thead must contain 'Providers' <th> only when providers column is active."""
        html_with = _render_stats(
            stats_template, recent_jobs=[], has_provider_results=True
        )
        html_without = _render_stats(
            stats_template, recent_jobs=[], has_provider_results=False
        )
        thead_with = re.search(
            r"<thead.*?</thead>", html_with, re.DOTALL | re.IGNORECASE
        )
        thead_without = re.search(
            r"<thead.*?</thead>", html_without, re.DOTALL | re.IGNORECASE
        )
        assert thead_with is not None
        assert thead_without is not None
        assert "Providers" in thead_with.group(0), (
            "Expected 'Providers' <th> in thead when has_provider_results=True"
        )
        assert "Providers" not in thead_without.group(0), (
            "Did not expect 'Providers' <th> in thead when has_provider_results=False"
        )


# ---------------------------------------------------------------------------
# Tests: body rows align with header
# ---------------------------------------------------------------------------


class TestDashboardStatsBodyAlignment:
    """Body rows rendered via include must have <td> count matching <th> count."""

    def test_body_rows_have_five_td_without_providers(self, stats_template):
        """Without provider_results, each body row must have exactly 5 <td> elements."""
        jobs = [
            _make_job(job_id="j1", username=None),
            _make_job(job_id="j2", username="alice"),
        ]
        html = _render_stats(
            stats_template, recent_jobs=jobs, has_provider_results=False
        )
        # Extract tbody content
        tbody_match = re.search(
            r"<tbody[^>]*>.*?</tbody>", html, re.DOTALL | re.IGNORECASE
        )
        assert tbody_match is not None, "No <tbody> found"
        tbody_html = tbody_match.group(0)
        rows = re.findall(r"<tr[^>]*>.*?</tr>", tbody_html, re.DOTALL | re.IGNORECASE)
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
        for row in rows:
            td_count = _count_td_in_row(row)
            assert td_count == 5, (
                f"Expected 5 <td> per row (no providers), got {td_count}. Row: {row[:200]}"
            )

    def test_body_rows_have_six_td_with_providers(self, stats_template):
        """With provider_results, each body row must have exactly 6 <td> elements."""
        jobs = [
            _make_job(
                job_id="j1", result={"provider_results": _make_provider_results()}
            ),
            _make_job(job_id="j2", result={}),
        ]
        html = _render_stats(
            stats_template, recent_jobs=jobs, has_provider_results=False
        )
        tbody_match = re.search(
            r"<tbody[^>]*>.*?</tbody>", html, re.DOTALL | re.IGNORECASE
        )
        assert tbody_match is not None
        tbody_html = tbody_match.group(0)
        rows = re.findall(r"<tr[^>]*>.*?</tr>", tbody_html, re.DOTALL | re.IGNORECASE)
        assert len(rows) == 2
        for row in rows:
            td_count = _count_td_in_row(row)
            assert td_count == 6, (
                f"Expected 6 <td> per row (with providers), got {td_count}. Row: {row[:200]}"
            )

    def test_thead_th_count_matches_body_td_count_no_providers(self, stats_template):
        """<th> count in thead must equal <td> count per row (no providers case)."""
        jobs = [_make_job(username="alice")]
        html = _render_stats(
            stats_template, recent_jobs=jobs, has_provider_results=False
        )
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        tbody_match = re.search(
            r"<tbody[^>]*>.*?</tbody>", html, re.DOTALL | re.IGNORECASE
        )
        assert thead_match and tbody_match
        th_count = _count_th(thead_match.group(0))
        rows = re.findall(
            r"<tr[^>]*>.*?</tr>", tbody_match.group(0), re.DOTALL | re.IGNORECASE
        )
        assert len(rows) == 1
        td_count = _count_td_in_row(rows[0])
        assert th_count == td_count, (
            f"thead has {th_count} <th> but body row has {td_count} <td> (no providers)"
        )

    def test_thead_th_count_matches_body_td_count_with_providers(self, stats_template):
        """<th> count in thead must equal <td> count per row (providers case)."""
        jobs = [_make_job(result={"provider_results": _make_provider_results()})]
        html = _render_stats(
            stats_template, recent_jobs=jobs, has_provider_results=False
        )
        thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL | re.IGNORECASE)
        tbody_match = re.search(
            r"<tbody[^>]*>.*?</tbody>", html, re.DOTALL | re.IGNORECASE
        )
        assert thead_match and tbody_match
        th_count = _count_th(thead_match.group(0))
        rows = re.findall(
            r"<tr[^>]*>.*?</tr>", tbody_match.group(0), re.DOTALL | re.IGNORECASE
        )
        assert len(rows) == 1
        td_count = _count_td_in_row(rows[0])
        assert th_count == td_count, (
            f"thead has {th_count} <th> but body row has {td_count} <td> (with providers)"
        )


# ---------------------------------------------------------------------------
# Tests: empty-state colspan alignment
# ---------------------------------------------------------------------------


class TestDashboardStatsEmptyStateColspan:
    """Empty-state colspan must match column count."""

    def test_empty_state_colspan_is_five_without_providers(self, stats_template):
        """Empty-state row colspan must be 5 when no providers (matches 5-column thead)."""
        html = _render_stats(stats_template, recent_jobs=[], has_provider_results=False)
        colspan = _extract_colspan(html)
        assert colspan == 5, (
            f"Expected empty-state colspan=5 (no providers), got {colspan}"
        )

    def test_empty_state_colspan_is_six_with_providers(self, stats_template):
        """Empty-state row colspan must be 6 when providers active (matches 6-column thead)."""
        html = _render_stats(stats_template, recent_jobs=[], has_provider_results=True)
        colspan = _extract_colspan(html)
        assert colspan == 6, (
            f"Expected empty-state colspan=6 (with providers), got {colspan}"
        )


# ---------------------------------------------------------------------------
# Tests: User/Actor column renders correctly (AC12)
# ---------------------------------------------------------------------------


class TestDashboardStatsUserColumn:
    """AC12: User/Actor column in initial render must behave like the partial."""

    def test_system_job_with_no_username_shows_na(self, stats_template):
        """System jobs (username=None) must show 'N/A' UNDER the User header."""
        jobs = [_make_job(username=None, actor_username=None)]
        html = _render_stats(stats_template, recent_jobs=jobs)
        assert "N/A" in html, (
            "System job with no username should show 'N/A' in User column"
        )

    def test_user_job_shows_username(self, stats_template):
        """Jobs with a username must display the username in the User column."""
        jobs = [_make_job(username="alice")]
        html = _render_stats(stats_template, recent_jobs=jobs)
        assert "alice" in html, "Job username 'alice' should appear in rendered HTML"

    def test_actor_shows_actor_arrow_owner(self, stats_template):
        """AC12: actor → owner format rendered when actor differs from owner."""
        jobs = [_make_job(username="owner", actor_username="actor")]
        html = _render_stats(stats_template, recent_jobs=jobs)
        assert "actor" in html
        assert "owner" in html


# ---------------------------------------------------------------------------
# Tests: no stale inline rows in dashboard_stats.html source
# ---------------------------------------------------------------------------


class TestDashboardStatsNoStalInlineRows:
    """dashboard_stats.html must not contain the stale 4-column inline tbody rows."""

    def test_dashboard_stats_has_no_stale_colspan_4(self):
        """The stale colspan='4' must not exist in dashboard_stats.html source."""
        source = DASHBOARD_STATS_PATH.read_text()
        assert 'colspan="4"' not in source, (
            "Stale colspan='4' found in dashboard_stats.html — "
            "the empty-state row must be removed (replaced by include)"
        )

    def test_dashboard_stats_includes_recent_jobs_partial(self):
        """dashboard_stats.html must include the recent_jobs partial (single source of truth)."""
        source = DASHBOARD_STATS_PATH.read_text()
        assert "dashboard_recent_jobs.html" in source, (
            "dashboard_stats.html must include 'partials/dashboard_recent_jobs.html' "
            "to ensure initial render and HTMX refresh use identical row markup"
        )
