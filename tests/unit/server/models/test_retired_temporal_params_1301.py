"""Tests for Bug #1301 part 2: retirement of show_evolution, evolution_limit,
and include_removed from the REST query model.

Approved fix (per product-owner decision, Bug #1301 / Epic #1289): these
three temporal parameters are permanent no-ops on the per-commit temporal
index (per-file diff timelines belong to the existing git tools
git_file_history/git_log/git_blame/git_diff, not the semantic temporal
path -- anti-duplication). Rather than implement them, they are REMOVED
from the advertised REST/MCP surface so the API never again advertises a
parameter it silently ignores (Messi #13 Anti-Silent-Failure).
"""

from code_indexer.server.models.query import SemanticQueryRequest


class TestRetiredTemporalParamsRemovedFromModel:
    """SemanticQueryRequest must no longer advertise the retired fields."""

    def test_show_evolution_not_in_model_fields(self):
        assert "show_evolution" not in SemanticQueryRequest.model_fields

    def test_evolution_limit_not_in_model_fields(self):
        assert "evolution_limit" not in SemanticQueryRequest.model_fields

    def test_include_removed_not_in_model_fields(self):
        assert "include_removed" not in SemanticQueryRequest.model_fields

    def test_at_commit_still_present(self):
        """at_commit is the param being IMPLEMENTED (not retired) -- must
        remain advertised."""
        assert "at_commit" in SemanticQueryRequest.model_fields

    def test_parsed_request_has_no_show_evolution_attribute(self):
        request = SemanticQueryRequest(
            query_text="find auth code",
            show_evolution=True,  # extra/unknown field in the raw payload
        )
        assert not hasattr(request, "show_evolution")

    def test_parsed_request_has_no_evolution_limit_attribute(self):
        request = SemanticQueryRequest(
            query_text="find auth code",
            evolution_limit=5,
        )
        assert not hasattr(request, "evolution_limit")

    def test_parsed_request_has_no_include_removed_attribute(self):
        request = SemanticQueryRequest(
            query_text="find auth code",
            include_removed=True,
        )
        assert not hasattr(request, "include_removed")
