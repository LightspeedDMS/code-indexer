"""Bug #1529 review finding #4: the temporal root's repo_alias is unsanitized.

``server_temporal_index_root()`` appends ``repo_alias`` directly beneath
``{golden_repos_dir}/.temporal/`` after stripping at most one trailing
``-global`` suffix. Nothing rejects a separator, a ``..`` segment, or an
absolute path -- so an alias carrying any of those escapes the container the
whole fixed-path design is defined in terms of. Since this one function is
"the ONE place the physical location is decided" for BOTH the read and write
sides, an escape here silently relocates real temporal data (write side) or
reads from outside the root entirely (read side).

The alias must be a single safe path component, and the resolved path must
provably stay beneath ``.temporal``.

Real path arithmetic only -- nothing mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.services.temporal.temporal_server_paths import (
    SERVER_TEMPORAL_ROOT_DIR_NAME,
    server_temporal_index_root,
)

GOLDEN_REPOS_DIR = Path("/srv/cidx/data/golden-repos")

#: Every one of these must be REFUSED. Each is a genuinely different escape
#: shape, not a restatement of one rule.
UNSAFE_ALIASES = [
    "..",
    ".",
    "../evolution",
    "evolution/../..",
    "nested/alias",
    "/absolute/alias",
    "/",
    "trailing/",
    "-global",  # normalizes to the empty string
    "..-global",  # normalizes to ".."
]


@pytest.mark.parametrize("alias", UNSAFE_ALIASES)
def test_unsafe_alias_is_refused(alias: str) -> None:
    with pytest.raises(ValueError):
        server_temporal_index_root(GOLDEN_REPOS_DIR, alias)


@pytest.mark.parametrize(
    "alias",
    ["evolution", "evolution-global", "my-repo_2024.v1", "a"],
)
def test_safe_alias_still_resolves_beneath_the_temporal_root(alias: str) -> None:
    """Legitimate aliases must be UNAFFECTED -- this is a guard, not a rename."""
    resolved = server_temporal_index_root(GOLDEN_REPOS_DIR, alias)

    container = GOLDEN_REPOS_DIR / SERVER_TEMPORAL_ROOT_DIR_NAME
    # Proves containment structurally, not by string prefix (which "/a/../b"
    # would satisfy while pointing elsewhere).
    assert resolved.parent == container
    assert resolved.resolve().is_relative_to(container.resolve())


def test_backslash_is_refused_even_though_posix_allows_it() -> None:
    """A backslash is a legal POSIX filename char but a separator elsewhere.

    Allowing it means the same alias denotes one directory on the server and a
    nested path on any other consumer -- exactly the kind of two-locations
    divergence Bug #1529 exists to eliminate.
    """
    with pytest.raises(ValueError):
        server_temporal_index_root(GOLDEN_REPOS_DIR, "evo\\lution")
