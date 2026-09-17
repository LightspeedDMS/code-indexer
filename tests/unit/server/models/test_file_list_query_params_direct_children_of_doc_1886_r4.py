"""
Bug #1886 (R4, item 3): FileListQueryParams.direct_children_of's Field
description only named browse_directory, but compute_direct_children_of is
also wired into MCP list_files (Bug #1886, R3) -- the description should
say so.
"""

from code_indexer.server.models.api_models import FileListQueryParams


def test_direct_children_of_description_mentions_both_mcp_handlers():
    description = FileListQueryParams.model_fields["direct_children_of"].description
    assert description is not None
    assert "browse_directory" in description
    assert "list_files" in description
