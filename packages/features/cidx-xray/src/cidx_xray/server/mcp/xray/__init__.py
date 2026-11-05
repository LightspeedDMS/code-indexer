"""xray_search MCP handler.

Thin shim: validate inputs, pre-flight check evaluator, submit background job.
Heavy lifting lives in XRaySearchEngine (cidx_xray/search_engine.py).

Modular plan phase 4c: this package moved from the core's
``code_indexer.server.mcp.handlers.xray`` into the cidx-xray feature; the
feature contributes its handlers through ``tools()``. ``cidx_fetch_cached_payload``
and ``cancel_job`` (generic tools that lived here) stayed in the core
(``handlers/cached_payload.py``, ``handlers/cancel_job.py``).

Story #972: synchronous single-threaded XRaySearchEngine baseline.
Story #978: will add ThreadPoolExecutor parallelism and job-level timeout.

Issue #1935 Part 2: this module used to be a single 2,228-line file. It is
now a package split by concern -- shared infrastructure (timeout
resolution, evaluator-code resolution, app-singleton/executor/limiter/
job-tracker accessors, result truncation) lives in `_infra.py`, and one
submodule per MCP-tool handler: store_xray_pattern (`_store_pattern.py`),
xray_search (`_search.py`), xray_explore (`_explore.py`) and xray_dump_ast
(`_dump_ast.py`). Every submodule imports its real
dependencies directly (from `._infra` or the external module that owns
them) rather than through this package. This `__init__.py` is a pure
re-export facade: every name re-exported below keeps every existing
`from ...xray import X` import working unchanged.

`dir(handlers.xray)` is NOT byte-identical to the flat module: 18 names
are absent. 17 are incidental import-scoping artifacts the flat module
exposed only as a side effect of its own `import`/`from typing import`
statements (`asyncio`, `uuid`, `logging`, `Path`, `cast`, `Callable`,
`NamedTuple`, `Optional`, `TYPE_CHECKING`, `ThreadPoolExecutor`, `User`,
`UserRole`, `XRaySearchEngine`, `check_query_admission`,
`memory_pressure_mcp_payload`, `get_config_service`,
`_parse_and_collapse_repo_alias`) -- never intentional API, and an
exhaustive grep across `src/` and `tests/` found zero dependents on any of
them via the `handlers.xray.<name>` path. The 18th, `_seeds_ensured`, WAS
re-exported here at one point but was deliberately dropped: it is a
mutable module-level flag that lives in `_infra.py` and is rebound there
via `global _seeds_ensured`, so a copy imported into this facade would
have been a permanently stale `False` snapshot that never tracked
`_infra`'s own mutation -- keeping it would have been actively misleading.
"""

from __future__ import annotations

from typing import Any, Dict

# validate_rust_evaluator: re-exported here (not called directly in this
# file) purely to keep `from ...xray import validate_rust_evaluator` and
# `handlers.xray.validate_rust_evaluator` working for existing callers --
# the submodules that actually call it (_search.py, _explore.py) import it
# directly from cidx_xray.sandbox themselves.
from cidx_xray.sandbox import validate_rust_evaluator  # noqa: F401

from .._common import _mcp_response  # noqa: F401

from ._infra import (  # noqa: F401
    logger,
    _get_cidx_meta_path,
    _TIMEOUT_MIN,
    _TIMEOUT_MAX,
    _DUMP_AST_MAX_NODES_DEFAULT,
    _DUMP_AST_MAX_NODES_MIN,
    _DUMP_AST_MAX_NODES_MAX,
    _DUMP_AST_MAX_FILE_SIZE_BYTES,
    _DEFAULT_EVALUATOR_CODE,
    _DEFAULT_TIMEOUT_SECONDS,
    _record_xray_timeout_config_read_failure_metric,
    _TimeoutResolution,
    _resolve_default_xray_timeout_seconds_detailed,
    _resolve_default_xray_timeout_seconds,
    _resolve_effective_timeout,
    _pattern_scope_alias,
    _resolve_evaluator_code,
    _resolve_evaluator_code_off_loop,
    _AWAIT_SECONDS_MIN,
    _AWAIT_SECONDS_MAX,
    _AWAIT_SECONDS_WARN_THRESHOLD,
    _AWAIT_POLL_INTERVAL,
    _lazy_singleton_app_or_none,
    set_xray_executor,
    _get_xray_executor,
    set_xray_cell_limiter,
    _get_xray_cell_limiter,
    _get_job_tracker,
    _resolve_repo_path,
    _get_background_job_manager,
    _await_xray_future,
    _validate_xray_search_patterns,
    _truncate_xray_result,
)

# Issue #1935 Part 2: re-exports of every handler moved to this
# package's submodules, so every existing `from ...xray import X` import
# keeps working unchanged.
from ._store_pattern import handle_store_xray_pattern  # noqa: F401
from ._search import handle_xray_search  # noqa: F401
from ._explore import (  # noqa: F401
    handle_xray_explore,
    _MAX_DEBUG_NODES_DEFAULT,
    _MAX_DEBUG_NODES_MIN,
    _MAX_DEBUG_NODES_MAX,
    _make_explore_done_cb_omni,
    _make_xray_explore_job_fn,
    _submit_one_explore_omni,
    _submit_xray_explore_omni,
)
from ._dump_ast import handle_xray_dump_ast  # noqa: F401


def _register(registry: Dict[str, Any]) -> None:
    """Add this package's handlers to *registry* (tool name -> handler)."""
    registry["xray_search"] = handle_xray_search
    registry["xray_explore"] = handle_xray_explore
    registry["xray_dump_ast"] = handle_xray_dump_ast
    registry["store_xray_pattern"] = handle_store_xray_pattern
