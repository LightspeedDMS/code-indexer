"""SCIP query limits (AC31-AC34): the ranges the ``scip`` config keys and the
query front doors (MCP, REST, CLI) accept.

Bug #1625: the dependency-depth ceiling MUST NOT exceed the engine's real
ceiling for ``get_dependencies()``/``get_dependents()`` (10, in
``database/queries.py``). Raising it lets an operator save a value that only
fails, opaquely, at query time. These are the only copy: the config screen
saves the keys through the core's feature config door, which applies the
section's bounds.
"""

MIN_SCIP_REFERENCE_LIMIT = 10
MAX_SCIP_REFERENCE_LIMIT = 10000
MIN_SCIP_DEPENDENCY_DEPTH = 1
MAX_SCIP_DEPENDENCY_DEPTH = 10
MIN_SCIP_CALLCHAIN_MAX_DEPTH = 1
MAX_SCIP_CALLCHAIN_MAX_DEPTH = 50
MIN_SCIP_CALLCHAIN_LIMIT = 1
MAX_SCIP_CALLCHAIN_LIMIT = 1000
