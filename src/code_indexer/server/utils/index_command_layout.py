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
never trust repository-authored resume state. ``--ignore-resume-state``
makes the child trust only resume state carrying a server-held seal
(written by a previous server-spawned run, see
``services/resume_state_seal.py``) without forcing a full re-embed (unlike
``--clear``), stamped in this same helper so every wrapped call site
inherits it automatically.

A repository's ``.code-indexer/config.json`` is likewise tenant/committer
writable, so server-spawned indexing must never let it choose where the
embedding-provider request goes, or force daemon delegation.
``--server-managed-provider-settings`` tells the child to use only
server-managed provider settings (the embedding-provider endpoint and
daemon-mode selection), never a repository-authored override, stamped in
this same helper for the same reason.

The content seam's backend selection travels the same way. The child cannot
read the server's config, and must never take the choice from the repository's
own config (tenant-writable, as above), so when the server is configured with
``content_backend='delegated'`` this helper stamps ``--content-backend=delegated``
and the child builds the same backend the server did. ``local`` stamps nothing,
so the default command line is unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from code_indexer.server.startup.content_backend_wiring import (
    configured_content_backend_name,
)
from code_indexer.server.startup.git_backend_wiring import (
    configured_git_backend_name,
)

#: The explicit server-context layout arg. Single `--opt=value` token so it
#: is a single, greppable list element that Click parses identically to the
#: two-token form.
SERVER_NEW_COLLECTION_LAYOUT_ARG = "--new-collection-layout=chunks_db"

#: Never trust repo-authored resume state on a
#: server-spawned `cidx index` run.
SERVER_IGNORE_RESUME_STATE_ARG = "--ignore-resume-state"

#: Server-spawned indexing uses server-managed provider settings -- never a
#: repository-authored embedding-provider endpoint or daemon-mode override.
SERVER_MANAGED_PROVIDER_SETTINGS_ARG = "--server-managed-provider-settings"

#: Hand the server's delegated content-backend selection to the child. Only
#: ever stamped for ``delegated``; ``local`` is the child's own default.
SERVER_DELEGATED_CONTENT_BACKEND_ARG = "--content-backend=delegated"

#: The same for git: a child on a pod that does not hold the repository must
#: ask git-mcp for HEAD's commit and branch, or it records "unknown" -- and
#: refresh then sees a stale index every cycle and re-indexes from scratch.
SERVER_DELEGATED_GIT_BACKEND_ARG = "--git-backend=delegated"


#: The meta repository cidx writes itself (descriptions, dependency maps).
CIDX_META_ALIASES = ("cidx-meta", "cidx-meta-global")

#: The registry URL cidx-meta carries once migrated to a local:// repository.
CIDX_META_REPO_URL = "local://cidx-meta"


def is_cidx_meta_repo(repo_url: Optional[str], alias: Optional[str] = None) -> bool:
    """Whether a golden repo is cidx-meta, by alias or by its local:// URL."""
    return alias in CIDX_META_ALIASES or repo_url == CIDX_META_REPO_URL


def is_pod_local_repo(repo_url: Optional[str], alias: Optional[str] = None) -> bool:
    """Whether a golden repo's files live on THIS pod, whatever the backends.

    ``local://`` repositories -- cidx-meta among them -- are created by cidx
    in its own data directory and written by it (descriptions, memories,
    uploads). A delegated GIT backend never holds them. Their CONTENT is on
    this pod's disk too, except cidx-meta's under a delegated content backend,
    which lives with workspace-mcp (see :func:`repo_layout_kwargs`).
    """
    return bool(repo_url and repo_url.startswith("local://")) or is_cidx_meta_repo(
        repo_url, alias
    )


def repo_layout_kwargs(
    repo_url: Optional[str],
    alias: Optional[str] = None,
    *,
    local_content: bool = False,
) -> Dict[str, Any]:
    """The :func:`append_server_layout_args` keywords for one golden repo.

    * cidx-meta: its content is wherever the server's content backend says
      (workspace-mcp under ``delegated``, where every server-side writer puts
      it), but it is not a git repository any git backend holds, so git stays
      local. Under the local default this stamps exactly what
      ``local_content=True`` did.
    * any other ``local://`` repository, or one the caller already knows is
      on this pod (``local_content``): entirely on this pod.
    * everything else: the configured backends.
    """
    if is_cidx_meta_repo(repo_url, alias):
        return {"git_backend": "local"}
    if local_content or is_pod_local_repo(repo_url, alias):
        return {"local_content": True}
    return {}


def append_server_layout_args(
    cmd: List[str],
    *,
    content_backend: Optional[str] = None,
    git_backend: Optional[str] = None,
    local_content: bool = False,
) -> List[str]:
    """Return a NEW command list with the explicit CHUNKS_DB new-collection
    layout arg, the resume-state-distrust arg, the
    server-managed-provider-settings arg and, when delegated, the
    content-backend arg appended.

    Args:
        cmd: A server-context ``cidx index`` command list (e.g.
            ``["cidx", "index", "--fts", "--progress-json"]``).
        content_backend: The server's content-backend selection. ``None``
            (every production caller) reads it from the server config, so a
            spawn site cannot forget to pass it.
        git_backend: The server's git-backend selection, read the same way.
        local_content: the repository's files are on this pod
            (``is_pod_local_repo``); neither backend flag is stamped, so the
            child reads the pod's disk and runs its own git, as it always has.

    Returns:
        A new list equal to ``cmd`` with ``--new-collection-layout=chunks_db``,
        ``--ignore-resume-state``, ``--server-managed-provider-settings`` and,
        for ``delegated`` only, ``--content-backend=delegated`` and
        ``--git-backend=delegated`` appended, in that order. The input list is
        not mutated.
    """
    if local_content:
        content_backend, git_backend = "local", "local"
    if content_backend is None:
        content_backend = configured_content_backend_name()
    if git_backend is None:
        git_backend = configured_git_backend_name()
    stamped = [
        *cmd,
        SERVER_NEW_COLLECTION_LAYOUT_ARG,
        SERVER_IGNORE_RESUME_STATE_ARG,
        SERVER_MANAGED_PROVIDER_SETTINGS_ARG,
    ]
    if content_backend == "delegated":
        stamped.append(SERVER_DELEGATED_CONTENT_BACKEND_ARG)
    if git_backend == "delegated":
        stamped.append(SERVER_DELEGATED_GIT_BACKEND_ARG)
    return stamped
