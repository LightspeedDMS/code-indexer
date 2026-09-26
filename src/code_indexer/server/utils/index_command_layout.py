"""Server-side `cidx index` new-collection layout stamping (Story #1488)
and resume-state-distrust stamping.

Story #1488 makes the CLI/daemon default new-collection chunk-storage
layout SHARDED_JSON. The server, by contrast, states the layout EXPLICITLY:
every server-context `cidx index` child subprocess must build brand-new
collections as the consolidated CHUNKS_DB layout, regardless of the child's
own env defaults.

This module is the SINGLE authority for that stamp. Every server-side
`cidx index` command-list construction routes through
``append_server_layout_args`` so a future spawn site cannot silently
regress to the CLI default -- enforced by an AST-based spawn-site guard
test (``test_index_command_layout_spawn_site_guard_1488.py``), mirroring
the ``build_temporal_child_env`` guard (Story #1457).

The flag governs BRAND-NEW collections only; an existing collection's
committed on-disk discriminator always wins (resolved downstream by
``resolve_chunk_layout`` / ``_is_chunks_db_collection``). It is therefore
harmless to stamp it onto FTS-only or rebuild commands -- uniformity beats
per-command special-casing.

Resume state (``.code-indexer/metadata-<provider>.json``)
lives in a tenant/committer-writable tree, so server-spawned indexing must
never trust it. ``--ignore-resume-state`` discards that trust without
forcing a full re-embed (unlike ``--clear``), stamped in this same helper
so every wrapped call site inherits it automatically.
"""

from __future__ import annotations

from typing import List

#: The explicit server-context layout arg. Single `--opt=value` token so it
#: is a single, greppable list element that Click parses identically to the
#: two-token form.
SERVER_NEW_COLLECTION_LAYOUT_ARG = "--new-collection-layout=chunks_db"

#: Never trust repo-authored resume state on a
#: server-spawned `cidx index` run.
SERVER_IGNORE_RESUME_STATE_ARG = "--ignore-resume-state"


def append_server_layout_args(cmd: List[str]) -> List[str]:
    """Return a NEW command list with the explicit CHUNKS_DB new-collection
    layout arg AND the resume-state-distrust arg appended.

    Args:
        cmd: A server-context ``cidx index`` command list (e.g.
            ``["cidx", "index", "--fts", "--progress-json"]``).

    Returns:
        A new list equal to ``cmd`` with ``--new-collection-layout=chunks_db``
        and ``--ignore-resume-state`` appended, in that order. The input
        list is not mutated.
    """
    return [*cmd, SERVER_NEW_COLLECTION_LAYOUT_ARG, SERVER_IGNORE_RESUME_STATE_ARG]
