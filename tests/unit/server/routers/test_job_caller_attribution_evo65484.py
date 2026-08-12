"""EVO-65484: X-Cidx-Caller -> job submitter label (attribution, not authz)."""

from code_indexer.server.routers.inline_admin_ops import _resolve_job_submitter


def test_valid_caller_used_as_submitter():
    assert _resolve_job_submitter("seeder", "admin") == "seeder"
    assert _resolve_job_submitter("workspace-mcp", "admin") == "workspace-mcp"


def test_absent_header_falls_back_to_auth_user():
    assert _resolve_job_submitter(None, "admin") == "admin"
    assert _resolve_job_submitter("", "jonathan") == "jonathan"


def test_malformed_caller_rejected_falls_back():
    # uppercase, leading digit/dash, spaces, injection chars, over-length -> ignored
    for bad in [
        "Admin",
        "1seeder",
        "-seeder",
        "work space",
        "a b",
        "seeder\n",
        "x" * 40,
        "sql'--",
    ]:
        assert _resolve_job_submitter(bad, "admin") == "admin"
