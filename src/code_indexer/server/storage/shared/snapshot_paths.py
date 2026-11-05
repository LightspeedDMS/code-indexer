"""Re-export of :mod:`code_indexer.utils.snapshot_paths`.

The canonical versioned-snapshot predicates are pure path rules with no server
dependency, so they moved out of the server (Phase 2 of
plans/designs/modular-packaging-addons.md) for the packages that must not
import it. This path keeps every existing import working.
"""

from code_indexer.utils.snapshot_paths import (
    is_versioned_snapshot as is_versioned_snapshot,
    resolve_versioned_snapshot_root as resolve_versioned_snapshot_root,
)
