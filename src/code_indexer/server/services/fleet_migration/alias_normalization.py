"""Codex review Finding F7: ONE centralized golden-alias normalization
helper, shared by every fleet-migration module that reads or writes a
`golden_alias`-keyed row (quarantine.py, dedup_state.py) so "foo" and
"foo-global" always resolve to the SAME logical repo/row.

Mirrors the bare/`-global` normalization convention this codebase
already established in Bug #1373's `_set_enable_temporal_flag`
(server/mcp/handlers/repos.py): strips exactly ONE trailing "-global"
suffix if present; a bare alias is returned unchanged. Deliberately a
tiny, dependency-free module (no imports beyond stdlib) so it can be
imported from anywhere in `fleet_migration/` without adding to any
import-budget concern (Bug #1468's lazy-load discipline).

`orchestrator.py`'s own `_bare_alias()` predates this module and is
kept as a thin, byte-identical local alias for minimal diff -- new
code should import `normalize_golden_alias` directly from here rather
than reimplementing the strip a third time (Messi Rule #4).
"""

from __future__ import annotations

#: The suffix Story #1039's "Global Repo Alias Fallback" mechanism
#: appends to a bare alias for the globally-activated form.
GLOBAL_SUFFIX = "-global"


def normalize_golden_alias(golden_alias: str) -> str:
    """Return `golden_alias` in its NORMALIZED (bare, no trailing
    "-global" suffix) form. Strips EXACTLY ONE trailing occurrence --
    never repeated/greedy, and never a `-global` substring that is not
    the trailing suffix (e.g. "global-services" is returned unchanged).
    A bare alias (no suffix) is returned unchanged.

    Raises:
        TypeError: golden_alias is not a str.
    """
    if not isinstance(golden_alias, str):
        raise TypeError(f"golden_alias must be a str, got {type(golden_alias)!r}")
    if golden_alias.endswith(GLOBAL_SUFFIX):
        return golden_alias[: -len(GLOBAL_SUFFIX)]
    return golden_alias
