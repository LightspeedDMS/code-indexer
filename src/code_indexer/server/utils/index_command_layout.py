"""Server-side `cidx index` new-collection layout stamping (Story #1488).

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
"""

from __future__ import annotations

from typing import List

#: The explicit server-context layout arg. Single `--opt=value` token so it
#: is a single, greppable list element that Click parses identically to the
#: two-token form.
SERVER_NEW_COLLECTION_LAYOUT_ARG = "--new-collection-layout=chunks_db"


def append_server_layout_args(cmd: List[str]) -> List[str]:
    """Return a NEW command list with the explicit CHUNKS_DB new-collection
    layout arg appended.

    Args:
        cmd: A server-context ``cidx index`` command list (e.g.
            ``["cidx", "index", "--fts", "--progress-json"]``).

    Returns:
        A new list equal to ``cmd`` with
        ``--new-collection-layout=chunks_db`` appended. The input list is
        not mutated.
    """
    return [*cmd, SERVER_NEW_COLLECTION_LAYOUT_ARG]
