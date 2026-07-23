"""
Unit tests for jobs.html Status filter dropdown - Bug #1452.

The Jobs panel's Status filter dropdown offered `value="queued"`, but jobs in
the `background_jobs` table are only ever persisted with `status="pending"`
(see `JobStatus` enum in `background_jobs.py`, which has no "queued" member
at all -- that string belongs to a completely separate `SyncJobManager` /
`sync_jobs` subsystem). The filter therefore did an exact-string comparison
against a value that could structurally never match any row, so selecting
"Queued" always returned zero results even when the dashboard's job-count
tile reported pending jobs.

Fix: the dropdown option is changed to `value="pending"` (label "Pending"),
matching the real `JobStatus.PENDING` enum value written to the database;
the dead `value="queued"` option is removed.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
import pytest


@pytest.fixture
def jobs_template():
    """Load the jobs.html template for testing."""
    templates_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    return env.get_template("jobs.html")


@pytest.fixture
def base_context():
    """Provide minimal context needed to render jobs.html."""
    return {
        "queue_status": {
            "running_count": 0,
            "queued_count": 0,
            "max_total_concurrent_jobs": 4,
            "max_concurrent_jobs_per_user": 2,
        },
        "jobs": [],
        "total_pages": 1,
        "current_page": 1,
        "status_filter": "",
        "type_filter": "",
        "search": "",
    }


def _status_select_section(rendered: str) -> str:
    """Extract the <select id="status_filter">...</select> HTML block."""
    return rendered.split('id="status_filter"')[1].split("</select>")[0]


class TestStatusFilterOffersPendingNotQueued:
    """Bug #1452: dropdown must offer a value that can actually match DB rows."""

    def test_dropdown_contains_pending_option(self, jobs_template, base_context):
        """The Status filter must offer value='pending', matching JobStatus.PENDING."""
        rendered = jobs_template.render(base_context)
        select_section = _status_select_section(rendered)

        assert 'value="pending"' in select_section, (
            "Status filter dropdown must include option with value='pending' "
            "to match the real background_jobs.status column value"
        )
        assert ">Pending<" in select_section, (
            "Status filter dropdown must include a 'Pending' label"
        )

    def test_dropdown_does_not_contain_dead_queued_option(
        self, jobs_template, base_context
    ):
        """The dead value='queued' option must be removed -- it can never match."""
        rendered = jobs_template.render(base_context)
        select_section = _status_select_section(rendered)

        assert 'value="queued"' not in select_section, (
            "Status filter dropdown must NOT offer value='queued' -- no "
            "background_jobs row can ever have that status (JobStatus enum "
            "has no QUEUED member); this value can structurally never match"
        )

    def test_pending_option_selected_when_status_filter_is_pending(
        self, jobs_template, base_context
    ):
        """Selecting status_filter='pending' must mark the Pending option selected."""
        context = {**base_context, "status_filter": "pending"}
        rendered = jobs_template.render(context)
        select_section = _status_select_section(rendered)

        option_line = [
            line for line in select_section.split("\n") if 'value="pending"' in line
        ]
        assert option_line, "Could not find the Pending option in the dropdown"
        assert "selected" in option_line[0], (
            "Pending option must be marked selected when status_filter='pending'"
        )

    def test_other_status_options_still_present(self, jobs_template, base_context):
        """Running/Completed/Failed/Cancelled options must be unaffected by the fix."""
        rendered = jobs_template.render(base_context)
        select_section = _status_select_section(rendered)

        for value, label in [
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ]:
            assert f'value="{value}"' in select_section, (
                f"Status filter dropdown must still include value='{value}'"
            )
            assert f">{label}<" in select_section, (
                f"Status filter dropdown must still include '{label}' label"
            )

    def test_all_status_option_still_present(self, jobs_template, base_context):
        """The empty 'All Status' option must be unaffected by the fix."""
        rendered = jobs_template.render(base_context)
        select_section = _status_select_section(rendered)

        assert 'value=""' in select_section
        assert ">All Status<" in select_section
