"""
Unit tests for dashboard tile / pagination hrefs -- Bug #1452 (continuation).

The first fix for #1452 corrected jobs.html's Status filter dropdown to offer
`value="pending"` instead of the dead `value="queued"`. A subsequent manual
E2E test proved the *reported* symptom -- "click the dashboard's Queued
job-count tile, see zero results even though pending jobs exist" -- was
STILL broken, for a second, independent reason: the dashboard tiles link to
`/admin/jobs?status=pending` (query param NAME `status`), but the
`jobs_page` / `jobs_list_partial` routes in `web/routes.py` only bind a
query parameter named `status_filter`. FastAPI silently ignores the
unrecognized `status` param, so the filtered URL renders byte-identical to
the unfiltered `/admin/jobs` page.

Fix: every `/admin/jobs?...status=<value>...` href is renamed to use
`status_filter=<value>`, the SAME param name the route already reads and
the SAME param name jobs.html's own (already-working) filter dropdown
submits. No second `status` alias is added to the route -- one name only.

While grepping for every `/admin/jobs?status=` occurrence (as instructed),
the identical param-name bug was also found in `jobs_list.html`'s
pagination links (Previous/page-number/Next), which build
`&status={{ status_filter }}` -- so paginating while a status filter is
active would also silently drop the filter. Fixed as part of this same
root cause.
"""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)


@pytest.fixture
def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


# ---------------------------------------------------------------------------
# dashboard_job_counts.html tiles
# ---------------------------------------------------------------------------


class TestDashboardJobCountsTilesUseStatusFilterParam:
    """The 4 job-count tiles must link with status_filter=, not status=."""

    def _render(self, jinja_env: Environment) -> str:
        template = jinja_env.get_template("partials/dashboard_job_counts.html")
        return template.render(
            job_counts={
                "running": 1,
                "queued": 2,
                "completed_24h": 3,
                "failed_24h": 4,
            },
            time_filter="24h",
        )

    def test_no_tile_uses_bare_status_param(self, jinja_env):
        """None of the tiles may use the dead '?status=' query param name."""
        rendered = self._render(jinja_env)
        assert "/admin/jobs?status=" not in rendered, (
            "dashboard_job_counts.html must not link with '?status=' -- "
            "the jobs_page route only binds 'status_filter', so this param "
            "name is silently ignored by FastAPI"
        )

    @pytest.mark.parametrize("value", ["running", "pending", "completed", "failed"])
    def test_tile_links_with_status_filter_param(self, jinja_env, value):
        rendered = self._render(jinja_env)
        assert f"/admin/jobs?status_filter={value}" in rendered, (
            f"Expected tile href '/admin/jobs?status_filter={value}' in "
            f"dashboard_job_counts.html rendered output"
        )


# ---------------------------------------------------------------------------
# dashboard_stats.html tiles (initial dashboard render, includes same 4
# tiles plus the "Active Deactivations" tile with a compound query string)
# ---------------------------------------------------------------------------


class TestDashboardStatsTilesUseStatusFilterParam:
    def _render(self, jinja_env: Environment) -> str:
        template = jinja_env.get_template("partials/dashboard_stats.html")
        return template.render(
            job_counts={
                "running": 1,
                "queued": 2,
                "completed_24h": 3,
                "failed_24h": 4,
            },
            repo_counts=None,
            api_metrics=None,
            recent_jobs=[],
            has_provider_results=False,
            time_filter="24h",
            api_filter=86400,
            cache_window=86400,
            active_deactivations=0,
        )

    def test_no_tile_uses_bare_status_param(self, jinja_env):
        rendered = self._render(jinja_env)
        assert "/admin/jobs?status=" not in rendered, (
            "dashboard_stats.html must not link with '?status=' -- the "
            "jobs_page route only binds 'status_filter'"
        )
        assert "&status=" not in rendered, (
            "dashboard_stats.html must not link with '&status=' in a "
            "compound query string (e.g. the Active Deactivations tile)"
        )

    @pytest.mark.parametrize("value", ["running", "pending", "completed", "failed"])
    def test_tile_links_with_status_filter_param(self, jinja_env, value):
        rendered = self._render(jinja_env)
        assert f"/admin/jobs?status_filter={value}" in rendered, (
            f"Expected tile href '/admin/jobs?status_filter={value}' in "
            f"dashboard_stats.html rendered output"
        )

    def test_active_deactivations_tile_uses_status_filter_param(self, jinja_env):
        """The compound-query 'Active Deactivations' tile must also use
        status_filter=, not status=, alongside job_type=."""
        rendered = self._render(jinja_env)
        assert (
            "/admin/jobs?job_type=deactivate_repository&status_filter=running"
            in rendered
        ), (
            "Expected Active Deactivations tile href to use "
            "'job_type=deactivate_repository&status_filter=running'"
        )


# ---------------------------------------------------------------------------
# jobs_list.html pagination links (same root cause, found via the mandated
# grep for every '/admin/jobs?status=' occurrence)
# ---------------------------------------------------------------------------


class TestJobsListPaginationLinksUseStatusFilterParam:
    def _render(self, jinja_env: Environment, **overrides) -> str:
        template = jinja_env.get_template("partials/jobs_list.html")
        context = {
            "jobs": [],
            "total_pages": 3,
            "page": 2,
            "status_filter": "pending",
            "type_filter": "",
            "search": "",
        }
        context.update(overrides)
        return template.render(**context)

    def test_pagination_links_do_not_use_bare_status_param(self, jinja_env):
        rendered = self._render(jinja_env)
        assert "&status=pending" not in rendered, (
            "jobs_list.html pagination links must not use '&status=' -- "
            "the jobs_page/jobs_list_partial routes only bind "
            "'status_filter', so an active status filter would be silently "
            "dropped when paginating"
        )

    def test_pagination_links_use_status_filter_param(self, jinja_env):
        rendered = self._render(jinja_env)
        assert "&status_filter=pending" in rendered, (
            "Expected pagination links to preserve the active filter via "
            "'&status_filter=pending'"
        )
        # Previous, page-number, and Next links should all carry it.
        assert rendered.count("&status_filter=pending") >= 3, (
            "Expected the Previous, page-number(s), and Next links to all "
            "carry '&status_filter=pending'"
        )
