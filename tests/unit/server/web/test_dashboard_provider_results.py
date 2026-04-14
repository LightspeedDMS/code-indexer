"""Tests for AC1 and AC2: Dashboard provider_results rendering.

AC1: Job detail (jobs_list.html) shows per-provider breakdown table when
     provider_results present in job.result.
AC2: dashboard_recent_jobs.html partial shows Providers column with colored
     CSS-class dots.

Bug #679 Part 2.
"""

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates"
)
PARTIALS_DIR = TEMPLATES_DIR / "partials"
RECENT_JOBS_TEMPLATE_NAME = "partials/dashboard_recent_jobs.html"
RECENT_JOBS_TEMPLATE_PATH = PARTIALS_DIR / "dashboard_recent_jobs.html"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_colspan(html: str) -> int:
    """Extract colspan value from the first empty-state table cell found."""
    match = re.search(r'colspan="(\d+)"', html)
    assert match is not None, "No colspan attribute found in rendered HTML"
    return int(match.group(1))


def _count_th_elements(html: str) -> int:
    """Count <th> header elements in rendered HTML."""
    return len(re.findall(r"<th[>\s]", html, re.IGNORECASE))


@pytest.fixture()
def jinja_env():
    """Jinja2 environment pointing at the real templates directory."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )


@pytest.fixture()
def recent_jobs_template(jinja_env):
    """Load the dashboard_recent_jobs partial template."""
    return jinja_env.get_template(RECENT_JOBS_TEMPLATE_NAME)


def _make_job(
    job_id: str = "abc123",
    repo_name: str = "my-repo",
    job_type: str = "full_index",
    completion_time: str = "2024-01-15T10:30:00Z",
    status: str = "completed",
    result: dict = None,
) -> dict:
    """Build a minimal job dict for template rendering."""
    return {
        "job_id": job_id,
        "repo_name": repo_name,
        "job_type": job_type,
        "completion_time": completion_time,
        "status": status,
        "result": result if result is not None else {},
    }


def _make_provider_results() -> dict:
    """Return a typical provider_results dict with all four statuses."""
    return {
        "voyage-ai": {
            "status": "success",
            "files_indexed": 4821,
            "chunks_indexed": 12340,
            "latency_ms": 312,
            "error": None,
        },
        "cohere": {
            "status": "failed",
            "files_indexed": 0,
            "chunks_indexed": 0,
            "latency_ms": 45,
            "error": "API key invalid",
        },
        "openai": {
            "status": "skipped",
            "files_indexed": 0,
            "chunks_indexed": 0,
            "latency_ms": None,
            "error": None,
        },
        "azure": {
            "status": "not_configured",
            "files_indexed": 0,
            "chunks_indexed": 0,
            "latency_ms": None,
            "error": None,
        },
    }


def _make_jobs_list_job(status: str = "completed", result: dict = None) -> dict:
    """Build a full job dict compatible with jobs_list.html partial."""
    return {
        "job_id": "abc123",
        "job_type": "full_index",
        "repository_name": "my-repo",
        "status": status,
        "progress": 100,
        "started_at": "2024-01-15T10:00:00",
        "completed_at": "2024-01-15T10:30:00",
        "duration_seconds": 1800,
        "error_message": None,
        "repository_url": None,
        "result": result if result is not None else {},
    }


# ---------------------------------------------------------------------------
# AC0: Template files exist
# ---------------------------------------------------------------------------


class TestTemplatesExist:
    """Verify required template files exist."""

    def test_recent_jobs_template_exists(self):
        """dashboard_recent_jobs.html must exist."""
        assert RECENT_JOBS_TEMPLATE_PATH.exists(), (
            f"Template not found: {RECENT_JOBS_TEMPLATE_PATH}"
        )

    def test_jobs_list_partial_exists(self):
        """jobs_list.html partial must exist."""
        assert (PARTIALS_DIR / "jobs_list.html").exists()


# ---------------------------------------------------------------------------
# AC2: dashboard_recent_jobs.html — backward compatibility (no provider_results)
# ---------------------------------------------------------------------------


class TestPartialBackwardCompatNoProviderResults:
    """When no job has provider_results, render legacy 4-column table."""

    def test_providers_column_absent_without_provider_results(
        self, recent_jobs_template
    ):
        """Jobs without provider_results do not render a Providers column header."""
        html = recent_jobs_template.render(recent_jobs=[_make_job()])
        assert "Providers" not in html

    def test_th_count_is_four_without_provider_results(self, recent_jobs_template):
        """Without provider_results, exactly 4 <th> column headers are rendered."""
        jobs = [_make_job(), _make_job(job_id="xyz999", repo_name="other")]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "Providers" not in html
        assert _count_th_elements(html) == 4

    def test_empty_state_colspan_is_four_without_provider_results(
        self, recent_jobs_template
    ):
        """Empty-state colspan equals 4 when no job has provider_results."""
        html = recent_jobs_template.render(recent_jobs=[])
        assert _extract_colspan(html) == 4

    def test_empty_state_renders_no_activity_message(self, recent_jobs_template):
        """Empty recent_jobs renders the no-activity fallback row."""
        html = recent_jobs_template.render(recent_jobs=[])
        assert "No recent activity" in html


# ---------------------------------------------------------------------------
# AC2: empty-state colspan adapts to Providers column
# ---------------------------------------------------------------------------


class TestEmptyStateColspan:
    """colspan must increase to 5 when Providers column is active."""

    def test_th_count_is_five_when_provider_results_present(self, recent_jobs_template):
        """With provider_results in a job, exactly 5 <th> headers are rendered."""
        jobs = [_make_job(result={"provider_results": _make_provider_results()})]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "Providers" in html
        assert _count_th_elements(html) == 5

    def test_empty_state_colspan_is_five_when_provider_results_context_set(
        self, recent_jobs_template
    ):
        """Empty-state colspan equals 5 when has_provider_results=True context var passed."""
        html = recent_jobs_template.render(recent_jobs=[], has_provider_results=True)
        assert _extract_colspan(html) == 5


# ---------------------------------------------------------------------------
# AC2: provider dots with CSS classes
# ---------------------------------------------------------------------------


class TestPartialRendersProviderDots:
    """When any job has provider_results, Providers column shows CSS-class dots."""

    def test_providers_column_shown_when_any_job_has_provider_results(
        self, recent_jobs_template
    ):
        """Providers column header appears when at least one job has provider_results."""
        jobs = [
            _make_job(result={"provider_results": _make_provider_results()}),
            _make_job(job_id="old-job", result={}),
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "Providers" in html

    def test_success_provider_dot_uses_success_css_class(self, recent_jobs_template):
        """Success provider renders span with provider-dot-success CSS class."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "voyage-ai": {"status": "success", "files_indexed": 100}
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "provider-dot-success" in html

    def test_failed_provider_dot_uses_failed_css_class(self, recent_jobs_template):
        """Failed provider renders span with provider-dot-failed CSS class."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "cohere": {
                            "status": "failed",
                            "files_indexed": 0,
                            "error": "key invalid",
                        }
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "provider-dot-failed" in html

    def test_skipped_provider_dot_uses_skipped_css_class(self, recent_jobs_template):
        """Skipped provider renders span with provider-dot-skipped CSS class."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "openai": {"status": "skipped", "files_indexed": 0}
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "provider-dot-skipped" in html

    def test_not_configured_provider_dot_uses_not_configured_css_class(
        self, recent_jobs_template
    ):
        """Not-configured provider renders span with provider-dot-not_configured CSS class."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "azure": {"status": "not_configured", "files_indexed": 0}
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "provider-dot-not_configured" in html

    def test_job_without_provider_results_shows_dash(self, recent_jobs_template):
        """Job row without provider_results shows em-dash placeholder in Providers cell."""
        jobs = [
            _make_job(result={"provider_results": _make_provider_results()}),
            _make_job(job_id="old-job", result={}),
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert (
            "—" in html or "&#8212;" in html or "&#x2014;" in html or "&mdash;" in html
        )

    def test_provider_dot_title_contains_provider_name_and_status(
        self, recent_jobs_template
    ):
        """Dot title attribute contains provider name and status for hover text."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "voyage-ai": {"status": "success", "files_indexed": 4821}
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "voyage-ai" in html
        assert "success" in html

    def test_provider_dot_title_contains_file_count(self, recent_jobs_template):
        """Dot title attribute includes files_indexed count for hover text."""
        jobs = [
            _make_job(
                result={
                    "provider_results": {
                        "voyage-ai": {"status": "success", "files_indexed": 4821}
                    }
                }
            )
        ]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "4821" in html

    def test_all_four_provider_statuses_render_distinct_dots(
        self, recent_jobs_template
    ):
        """All four provider statuses render distinct CSS-class dots in one row."""
        jobs = [_make_job(result={"provider_results": _make_provider_results()})]
        html = recent_jobs_template.render(recent_jobs=jobs)
        assert "provider-dot-success" in html
        assert "provider-dot-failed" in html
        assert "provider-dot-skipped" in html
        assert "provider-dot-not_configured" in html


# ---------------------------------------------------------------------------
# AC1: COMPLETED_PARTIAL badge styling in recent_jobs partial
# ---------------------------------------------------------------------------


class TestCompletedPartialBadgeStyling:
    """COMPLETED_PARTIAL status badge uses distinct warning CSS styling."""

    def test_completed_partial_badge_uses_status_completed_partial_class(
        self, recent_jobs_template
    ):
        """COMPLETED_PARTIAL badge has 'status-completed_partial' CSS class exactly."""
        html = recent_jobs_template.render(
            recent_jobs=[_make_job(status="completed_partial")]
        )
        assert "status-completed_partial" in html

    def test_completed_partial_badge_differs_from_completed(self, recent_jobs_template):
        """COMPLETED_PARTIAL renders different HTML from 'completed'."""
        html_partial = recent_jobs_template.render(
            recent_jobs=[_make_job(status="completed_partial")]
        )
        html_complete = recent_jobs_template.render(
            recent_jobs=[_make_job(status="completed")]
        )
        assert html_partial != html_complete

    def test_completed_partial_renders_without_error(self, recent_jobs_template):
        """COMPLETED_PARTIAL status does not crash template rendering."""
        html = recent_jobs_template.render(
            recent_jobs=[_make_job(status="completed_partial")]
        )
        assert html
        assert "my-repo" in html


# ---------------------------------------------------------------------------
# AC1: jobs_list.html per-provider breakdown table
# ---------------------------------------------------------------------------


class TestJobDetailProviderTable:
    """AC1: jobs_list.html renders per-provider breakdown table."""

    def test_provider_table_rendered_when_provider_results_present(self, jinja_env):
        """jobs_list.html shows provider names and file counts when provider_results set."""
        template = jinja_env.get_template("partials/jobs_list.html")
        jobs = [
            _make_jobs_list_job(
                status="completed",
                result={
                    "provider_results": {
                        "voyage-ai": {
                            "status": "success",
                            "files_indexed": 4821,
                            "chunks_indexed": 12340,
                            "latency_ms": 312,
                            "error": None,
                        },
                        "cohere": {
                            "status": "failed",
                            "files_indexed": 0,
                            "chunks_indexed": 0,
                            "latency_ms": 45,
                            "error": "API key invalid",
                        },
                    }
                },
            )
        ]
        html = template.render(jobs=jobs)
        assert "voyage-ai" in html
        assert "cohere" in html
        assert "4821" in html

    def test_provider_table_absent_for_legacy_job(self, jinja_env):
        """jobs_list.html does not render provider table for jobs without provider_results."""
        template = jinja_env.get_template("partials/jobs_list.html")
        jobs = [_make_jobs_list_job(result={})]
        html = template.render(jobs=jobs)
        assert "provider-breakdown" not in html
        assert "provider-dot-success" not in html

    def test_error_text_truncated_to_200_chars_in_provider_table(self, jinja_env):
        """Error column truncates error text to 200 characters with ellipsis."""
        template = jinja_env.get_template("partials/jobs_list.html")
        long_error = "E" * 500
        jobs = [
            _make_jobs_list_job(
                status="completed_partial",
                result={
                    "provider_results": {
                        "cohere": {
                            "status": "failed",
                            "files_indexed": 0,
                            "chunks_indexed": 0,
                            "latency_ms": 10,
                            "error": long_error,
                        }
                    }
                },
            )
        ]
        html = template.render(jobs=jobs)
        # The span renders: title="<full error>" with visible text truncated to 200 chars + "..."
        # Full error MUST appear in the title attribute (tooltip); visible text MUST be truncated.
        span_match = re.search(r'<span\s+title="([^"]*)">(.*?)</span>', html, re.DOTALL)
        assert span_match is not None, (
            "No error span with title attribute found in rendered HTML"
        )
        tooltip_text = span_match.group(1)
        visible_text = span_match.group(2)
        assert tooltip_text == long_error, (
            f"Title attribute must contain the full error string (got {len(tooltip_text)} chars)"
        )
        expected_visible = "E" * 200 + "..."
        assert visible_text == expected_visible, (
            f"Visible text must be exactly 200 chars + '...' (got: {visible_text!r})"
        )

    def test_completed_partial_status_in_jobs_list_uses_exact_css_class(
        self, jinja_env
    ):
        """jobs_list.html uses 'status-completed_partial' CSS class for COMPLETED_PARTIAL."""
        template = jinja_env.get_template("partials/jobs_list.html")
        jobs = [_make_jobs_list_job(status="completed_partial")]
        html = template.render(jobs=jobs)
        assert "status-completed_partial" in html

    def test_provider_table_has_expected_columns(self, jinja_env):
        """Provider breakdown table contains Provider, Status, and Files Indexed columns."""
        template = jinja_env.get_template("partials/jobs_list.html")
        jobs = [
            _make_jobs_list_job(
                result={
                    "provider_results": {
                        "voyage-ai": {
                            "status": "success",
                            "files_indexed": 100,
                            "chunks_indexed": 500,
                            "latency_ms": 200,
                            "error": None,
                        }
                    }
                }
            )
        ]
        html = template.render(jobs=jobs)
        assert "Provider" in html
        assert "Status" in html
        assert "Files" in html or "Files Indexed" in html
