"""Bug #1529 review findings #8 and #11.

Finding #11 -- `server/storage/postgres/temporal_child_wiring.py` re-declared
`CIDX_SERVER_REFRESH_CONTEXT_ENV` as its own string literal instead of
importing the canonical definition from `temporal_server_paths`. Two
independent literals for one wire protocol is a silent-drift hazard: the
writer (child env builder) and the reader (`is_server_refresh_context`) would
disagree the moment either is edited, and the failure mode is temporal data
silently going to the wrong location -- exactly this bug's own class of
defect. CLAUDE.md already fixes the direction: the canonical definition lives
in the dependency-free `temporal_server_paths` module and the server-side
wiring imports FROM it, never the reverse (that direction would drag the
server/psycopg import chain into the standalone CLI's temporal path, the Bug
#1468 import-budget regression).

Finding #8 -- `resolve_temporal_index_dir` returned the in-repo path silently
when server context WAS detected but the codebase_dir did not resolve to
golden coordinates. In server context that combination means temporal data is
about to be read from / written to a location the server side does not agree
with, and it produced no signal at all. Once the read seams fail loud
(finding #2), this lower-level seam must at least be visible.
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.storage.postgres import temporal_child_wiring
from code_indexer.services.temporal import temporal_server_paths
from code_indexer.services.temporal.temporal_server_paths import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
    in_repo_temporal_index_dir,
    resolve_temporal_index_dir,
)

#: The symbol name whose definition must exist in exactly one module.
ENV_MARKER_NAME = "CIDX_SERVER_REFRESH_CONTEXT_ENV"

#: Module the canonical definition must be imported FROM. Matched on the last
#: dotted segment because the real import is fully qualified
#: (code_indexer.services.temporal.temporal_server_paths); an exact
#: comparison against the bare module name would never match.
CANONICAL_MODULE_SUFFIX = "temporal_server_paths"

SERVER_CONTEXT_ENV = {CIDX_SERVER_REFRESH_CONTEXT_ENV: "1"}

#: Every assignment form that could introduce a second definition.
_ASSIGNMENT_NODES = (ast.Assign, ast.AnnAssign, ast.AugAssign)


def _target_names(target: ast.AST) -> List[str]:
    """Names bound by an assignment target, recursing into destructuring.

    A bare `ast.Name` check is not enough: `(MARKER, other) = ...` is an
    ordinary `ast.Assign` whose target is a Tuple, and a duplicate definition
    hidden that way must still be caught.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _assigned_names(node: ast.AST) -> List[str]:
    """Names bound by any assignment form."""
    if isinstance(node, ast.Assign):
        names: List[str] = []
        for target in node.targets:
            names.extend(_target_names(target))
        return names
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return _target_names(node.target)
    return []


def test_child_wiring_reuses_the_canonical_env_marker() -> None:
    """One wire protocol must have exactly one definition.

    Checked STRUCTURALLY, not by identity: Python interns identifier-like
    string literals, so `child.X is canonical.X` is True even when both
    modules declare their own separate literal -- an identity assertion here
    would pass against the very code this finding reports. Only the source
    can say whether a second definition exists.
    """
    tree = ast.parse(inspect.getsource(temporal_child_wiring))

    own_definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, _ASSIGNMENT_NODES)
        and ENV_MARKER_NAME in _assigned_names(node)
    ]
    assert own_definitions == [], (
        f"temporal_child_wiring declares its own {ENV_MARKER_NAME} instead of "
        "importing the canonical one; the env-marker writer and reader can "
        "then silently drift apart"
    )

    imports_it = any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.split(".")[-1] == CANONICAL_MODULE_SUFFIX
        and any(alias.name == ENV_MARKER_NAME for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imports_it, (
        f"temporal_child_wiring must import {ENV_MARKER_NAME} from "
        f"{CANONICAL_MODULE_SUFFIX} (never the reverse direction, which would "
        "drag the server import chain into the standalone CLI temporal path)"
    )

    # And the value the two modules agree on must still be the real marker.
    assert (
        temporal_child_wiring.CIDX_SERVER_REFRESH_CONTEXT_ENV
        == temporal_server_paths.CIDX_SERVER_REFRESH_CONTEXT_ENV
    )


def test_unresolvable_codebase_in_server_context_raises(tmp_path: Path) -> None:
    """Server context + no golden coordinates must FAIL, not fall back.

    Round-4 correction: this used to log a WARNING and return the in-repo
    path. That made the WRITE side fail OPEN on exactly the failure the READ
    side (`SemanticQueryManager._resolve_temporal_index_dir`) fails CLOSED on
    -- so for this one unrecognized-topology case writes would land in-repo
    while reads looked at the fixed root, silently recreating the
    staleness/duplication bug class Bug #1529 exists to close. The two sides
    must make the SAME choice, and failing loudly is the only safe one: no
    caller can act on data written somewhere nothing will ever read.
    """
    # Not a golden repo layout: the parent is not "golden-repos" and there is
    # no .versioned/<alias>/v_* chain, so coordinates cannot be resolved.
    codebase_dir = tmp_path / "not-a-golden-repo" / "myrepo"
    codebase_dir.mkdir(parents=True)

    with pytest.raises(ValueError):
        resolve_temporal_index_dir(codebase_dir, env=SERVER_CONTEXT_ENV)


def test_standalone_cli_stays_silent(tmp_path: Path, caplog) -> None:
    """No server context is the ordinary CLI case -- it must NOT warn.

    Warning here would emit noise on every standalone `cidx index` run, so
    this is what proves the new log is scoped to the anomalous case.
    """
    codebase_dir = tmp_path / "my-working-repo"
    codebase_dir.mkdir(parents=True)

    with caplog.at_level(logging.WARNING):
        resolved = resolve_temporal_index_dir(codebase_dir, env={})

    assert resolved == in_repo_temporal_index_dir(codebase_dir)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
