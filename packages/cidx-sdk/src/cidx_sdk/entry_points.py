"""Entry-point group names: how the core discovers add-ons.

The feature group is new with the SDK. The seam groups are listed here so an
add-on can register without importing the core; each core resolver module
(``server/services/*_plugin.py``, ``utils/git_invoker.py``) re-exports its
seam's group as ``ENTRY_POINT_GROUP``, and a test in the core's suite pins that
the two agree.
"""

from __future__ import annotations

from typing import Dict

FEATURES_ENTRY_POINT_GROUP = "code_indexer.features"

CONTENT_BACKEND_ENTRY_POINT_GROUP = "code_indexer.content_backend"
CONTENT_WRITER_ENTRY_POINT_GROUP = "code_indexer.content_writer"
GIT_BACKEND_ENTRY_POINT_GROUP = "code_indexer.git_backend"
GIT_INVOKER_ENTRY_POINT_GROUP = "code_indexer.git_invoker"
RIPGREP_BACKEND_ENTRY_POINT_GROUP = "code_indexer.ripgrep_backend"
SEARCH_BACKEND_ENTRY_POINT_GROUP = "code_indexer.search_backend"
INDEX_ARTIFACTS_ENTRY_POINT_GROUP = "code_indexer.index_artifacts"
SCIP_BACKEND_ENTRY_POINT_GROUP = "code_indexer.scip_backend"
XRAY_BACKEND_ENTRY_POINT_GROUP = "code_indexer.xray_backend"
CLONE_BACKEND_ENTRY_POINT_GROUP = "code_indexer.clone_backend"
CLI_INVOKER_ENTRY_POINT_GROUP = "code_indexer.cli_invoker"

# Seam name -> entry-point group. The seam names are what a feature manifest
# lists in ``optional_seams``. Within a group the configured mode is chosen by
# entry name (see ``cidx_sdk.seams.selection``).
SEAM_ENTRY_POINT_GROUPS: Dict[str, str] = {
    "content": CONTENT_BACKEND_ENTRY_POINT_GROUP,
    "content_writer": CONTENT_WRITER_ENTRY_POINT_GROUP,
    "git": GIT_BACKEND_ENTRY_POINT_GROUP,
    "git_invoker": GIT_INVOKER_ENTRY_POINT_GROUP,
    "ripgrep": RIPGREP_BACKEND_ENTRY_POINT_GROUP,
    "search": SEARCH_BACKEND_ENTRY_POINT_GROUP,
    "index_artifacts": INDEX_ARTIFACTS_ENTRY_POINT_GROUP,
    "scip": SCIP_BACKEND_ENTRY_POINT_GROUP,
    "xray": XRAY_BACKEND_ENTRY_POINT_GROUP,
    "clone": CLONE_BACKEND_ENTRY_POINT_GROUP,
    "cli_invoker": CLI_INVOKER_ENTRY_POINT_GROUP,
}
