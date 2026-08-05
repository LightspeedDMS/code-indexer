"""Deterministic server-context temporal data paths (Bug #1529, Decision 1).

In SERVER context, a golden repo's temporal (git-commit-history) index must
NOT live inside the golden repo's own cloned directory tree. If it does, the
per-user CoW activation clone copies the entire temporal history into every
activation (real, measured: a full quarter-shard tree per activation), and
every activated-repo temporal query then reads that frozen-at-clone-time copy
instead of the golden repo's current data -- silent divergence the moment the
golden repo is refreshed. That is the defect Bug #1529 closes.

This module is the ONE place the physical location is decided. Both sides of
the system funnel through it, from different starting information:

  - WRITE side: the ``cidx index --index-commits`` CHILD subprocess. It knows
    only its own ``codebase_dir`` plus the ``CIDX_SERVER_REFRESH_CONTEXT``
    marker (set unconditionally, in every storage mode, by
    ``build_temporal_child_env``). It calls
    ``resolve_server_temporal_index_root_for_codebase()``.
  - READ side: the SERVER process. It knows the golden repo's alias and its
    ``golden_repos_dir``, never the child's codebase_dir. It calls
    ``server_temporal_index_root()`` directly.

Both therefore agree by construction. Nothing here versions, publishes, or
indirects: given the same ``(golden_repos_dir, repo_alias)`` the answer is
always the same directory, forever, from the moment it is first created. That
fixed-path property is also precisely why the path-derived temporal metadata
key (``sha256(str(collection_path))``) can never go stale here -- there is no
relocation for a re-keying migration to have to follow (see Bug #1529's "Why
the Postgres re-key problem disappears").

Layout
------
``{golden_repos_dir}/.temporal/{bare_repo_alias}/`` is used as a
``FilesystemVectorStore`` **index root**, so the per-namespace directory
underneath it keeps its ordinary physical collection name and the final
resting place of a quarter shard is::

    {golden_repos_dir}/.temporal/{alias}/code-indexer-temporal-{embedder}-{quarter}/chunks.db

DEVIATION FROM THE ISSUE'S ILLUSTRATIVE EXAMPLE (deliberate, flagged for
review): Bug #1529 writes the example as
``{golden_repos_dir}/{repo_alias}-temporal-{embedder}-{quarter}/chunks.db``
(the shard directories sitting directly in ``golden_repos_dir``). This module
nests them under a single dot-prefixed ``.temporal/{alias}/`` root instead,
for two concrete reasons -- the derived tuple, the determinism, and the
"outside the repo's own tree" property the issue actually specifies are all
preserved either way:

  1. It keeps ``golden_repos_dir`` free of directories that look like repo
     clones but are not. That directory is scanned/listed by real code and by
     operators; ``evolution-temporal-voyage_code_3-2024Q1`` sitting next to
     ``evolution`` invites exactly the "is this a golden repo?" confusion the
     dot-prefixed ``.versioned/`` convention already exists to avoid.
  2. Because the per-repo root is a plain index root, the ENTIRE existing
     index-root abstraction is reused unchanged -- collection naming, shard
     discovery, HNSW, projection matrices, progress metadata and reconcile
     all keep working with no temporal-specific path plumbing threaded
     through them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Tuple, Union

#: Unconditional server-context marker, set on EVERY server-spawned temporal
#: child in EVERY storage mode by ``build_temporal_child_env``. Its canonical
#: definition lives HERE, in a dependency-free module on the CLI import path,
#: and the server-side wiring imports it from here -- never the reverse. The
#: reverse direction would drag the server/psycopg/config-service import chain
#: into the standalone CLI's temporal path (the Bug #1468 import-budget
#: regression class).
CIDX_SERVER_REFRESH_CONTEXT_ENV = "CIDX_SERVER_REFRESH_CONTEXT"

#: Dot-prefixed container for every golden repo's server-owned temporal index
#: roots, a sibling of the established ``.versioned/`` convention.
SERVER_TEMPORAL_ROOT_DIR_NAME = ".temporal"

#: Structural markers of the two real on-disk golden-repo layouts. These are
#: the SAME fixed shapes this project already relies on elsewhere
#: (``VersionedSnapshotManager``, ``golden_repo_manager``): a flat clone at
#: ``<golden_repos_dir>/<alias>/`` and a versioned snapshot at
#: ``<golden_repos_dir>/.versioned/<alias>/v_*/``.
GOLDEN_REPOS_DIR_NAME = "golden-repos"
VERSIONED_DIR_NAME = ".versioned"
VERSION_DIR_PREFIX = "v_"

_GLOBAL_ALIAS_SUFFIX = "-global"


def is_server_refresh_context(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return True iff this process was spawned by the server.

    Read fresh on every call (never cached at import time) so a test or a
    caller that adjusts the environment is honored. An empty/whitespace-only
    value is NOT server context: a deliberately blanked-out inherited
    variable means standalone CLI.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return bool(source.get(CIDX_SERVER_REFRESH_CONTEXT_ENV, "").strip())


def normalize_repo_alias(repo_alias: str) -> str:
    """Strip exactly one trailing ``-global`` query-facing suffix.

    A query can arrive addressed to ``{alias}-global`` while the write side
    only ever knows the bare on-disk directory name. Both must resolve to the
    same physical root or reads silently miss written data.
    """
    return repo_alias.removesuffix(_GLOBAL_ALIAS_SUFFIX)


def resolve_golden_repo_coordinates(
    codebase_dir: Optional[Union[Path, str]],
) -> Optional[Tuple[Path, str]]:
    """Recover ``(golden_repos_dir, bare_repo_alias)`` from a codebase_dir.

    Recognizes ONLY the two established structural layouts (never a heuristic
    guess), and returns the IDENTICAL coordinates for both so a repo cannot
    resolve two different temporal roots depending on which shape indexing
    happened to run against:

      - flat:      ``<golden_repos_dir>/<alias>/``
      - versioned: ``<golden_repos_dir>/.versioned/<alias>/v_*/``

    Returns None for anything else -- most importantly for an ordinary user
    working repo, the standalone-CLI case, which must never be handed a
    sister root.
    """
    if codebase_dir is None or str(codebase_dir) == "":
        return None

    path = Path(codebase_dir)

    # Versioned snapshot: .../<golden_repos_dir>/.versioned/<alias>/v_*/
    if path.name.startswith(VERSION_DIR_PREFIX):
        alias_dir = path.parent
        versioned_dir = alias_dir.parent
        if (
            versioned_dir.name == VERSIONED_DIR_NAME
            and versioned_dir.parent.name == GOLDEN_REPOS_DIR_NAME
        ):
            return versioned_dir.parent, normalize_repo_alias(alias_dir.name)

    # Flat clone: .../<golden_repos_dir>/<alias>/
    if path.parent.name == GOLDEN_REPOS_DIR_NAME:
        return path.parent, normalize_repo_alias(path.name)

    return None


#: Path components that are never a legitimate repo alias. The empty string is
#: included because ``normalize_repo_alias`` can PRODUCE it (alias == "-global").
_UNSAFE_ALIAS_COMPONENTS = frozenset({"", ".", ".."})

#: Separator characters rejected in an alias. ``\\`` is rejected even though it
#: is a legal POSIX filename character: allowing it would make one alias denote
#: a single directory here and a nested path on any non-POSIX consumer -- the
#: same "two locations for one namespace" divergence this module exists to end.
_ALIAS_SEPARATOR_CHARS = ("/", "\\", os.sep, os.altsep)


def _validate_alias_path_component(bare_alias: str, repo_alias: str) -> None:
    """Assert the normalized alias is ONE safe path component (Bug #1529 #4).

    This module is the ONE place the physical temporal location is decided for
    both the read and the write side, so an alias that escapes ``.temporal/``
    here silently relocates real data (write) or reads from outside the root
    entirely (read). Refused loudly rather than sanitized: silently rewriting
    an alias would map two distinct aliases onto one directory, which is a
    data-mixing bug rather than a fix.
    """
    if bare_alias in _UNSAFE_ALIAS_COMPONENTS:
        raise ValueError(
            f"server_temporal_index_root: repo_alias {repo_alias!r} is not a "
            f"usable directory name (normalizes to {bare_alias!r})"
        )
    for separator in _ALIAS_SEPARATOR_CHARS:
        if separator and separator in bare_alias:
            raise ValueError(
                f"server_temporal_index_root: repo_alias {repo_alias!r} must be "
                f"a single path component, but contains {separator!r}"
            )


def server_temporal_index_root(
    golden_repos_dir: Union[Path, str], repo_alias: str
) -> Path:
    """The fixed temporal index root for one golden repo. Never versioned.

    This is the single deterministic formula both the write child and the
    read-side server derive their path from.

    Raises:
        ValueError: if ``golden_repos_dir`` is missing, or ``repo_alias`` is
            missing or is not a single safe path component (Bug #1529 #4).
    """
    if golden_repos_dir is None or str(golden_repos_dir) == "":
        raise ValueError("server_temporal_index_root: golden_repos_dir is required")
    if not repo_alias:
        raise ValueError("server_temporal_index_root: repo_alias is required")

    bare_alias = normalize_repo_alias(repo_alias)
    _validate_alias_path_component(bare_alias, repo_alias)

    container = Path(golden_repos_dir) / SERVER_TEMPORAL_ROOT_DIR_NAME
    resolved = container / bare_alias

    # Defense in depth: the component checks above are the real guard, but
    # containment is the invariant that actually matters, so assert it
    # directly rather than trusting the character blacklist is exhaustive.
    if resolved.parent != container:
        raise ValueError(
            f"server_temporal_index_root: repo_alias {repo_alias!r} does not "
            f"resolve directly beneath {container}"
        )

    return resolved


def in_repo_temporal_index_dir(codebase_dir: Union[Path, str]) -> Path:
    """The ordinary in-repo index root -- the standalone-CLI location."""
    return Path(codebase_dir) / ".code-indexer" / "index"


def resolve_temporal_index_dir(
    codebase_dir: Union[Path, str], *, env: Optional[Mapping[str, str]] = None
) -> Path:
    """THE seam: where this process must read/write temporal data.

    Server context AND a structurally-recognized golden repo clone -> the
    fixed sister root outside the repo tree. Anything else (standalone CLI,
    a user's own repo, an unrecognized layout) -> the ordinary in-repo path,
    byte-identical to pre-Bug #1529 behavior.

    Both the write child and any read-side caller that starts from a
    codebase_dir MUST funnel through this one function, so the two can never
    disagree about the location.
    """
    if is_server_refresh_context(env):
        sister_root = resolve_server_temporal_index_root_for_codebase(codebase_dir)
        if sister_root is not None:
            return sister_root
    return in_repo_temporal_index_dir(codebase_dir)


def resolve_server_temporal_index_root_for_codebase(
    codebase_dir: Optional[Union[Path, str]],
) -> Optional[Path]:
    """Write-side convenience: codebase_dir -> its fixed temporal index root.

    Returns None when codebase_dir is not structurally a golden repo clone,
    so the caller keeps its ordinary in-repo behavior unchanged.
    """
    coordinates = resolve_golden_repo_coordinates(codebase_dir)
    if coordinates is None:
        return None
    golden_repos_dir, repo_alias = coordinates
    return server_temporal_index_root(golden_repos_dir, repo_alias)
