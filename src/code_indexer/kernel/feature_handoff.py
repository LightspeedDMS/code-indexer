"""The switch between the core's own wiring of a feature and the feature's hooks.

A feature that moved into its own distribution (X-Ray: ``cidx_xray``) declares
its contributions through the ``cidx_sdk`` Feature hooks. Until the core
CONSUMES those hooks (modular plan phase 4a), it keeps registering the
feature's MCP tools, REST routes and CLI group itself, exactly as before the
move. Every such registration site is guarded by :func:`core_wires`, so the
handoff is one edit here plus deleting the guarded sites, and
``grep core_wires`` lists everything the core still wires for a feature.

Remove a name from :data:`CORE_WIRED_FEATURES` only in the change that makes
the core consume that feature's hooks AND deletes the guarded wiring: with the
name gone and nothing consuming the hooks, the feature's tools, routes and CLI
commands disappear. No third-party or server imports: the CLI reads this at
startup.

X-Ray was handed off in modular plan phase 4c: the core consumes every hook of
the cidx-xray feature and wires nothing of it itself, so the set is empty.
"""

from __future__ import annotations

from typing import FrozenSet

#: Features whose contributions the core still registers itself.
CORE_WIRED_FEATURES: FrozenSet[str] = frozenset()


def core_wires(feature_name: str) -> bool:
    """Whether the core registers *feature_name*'s contributions itself."""
    return feature_name in CORE_WIRED_FEATURES
