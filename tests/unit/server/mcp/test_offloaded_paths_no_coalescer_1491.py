"""Story #1491 review item 9: the asyncio.run()-in-a-worker-thread paths must
never touch the lifespan-built coalescer/governor.

Two entry points introduced by this story run a coroutine on a PRIVATE event
loop owned by a threadpool worker:

    handle_regex_search_sync  -> asyncio.run(handle_regex_search(...))   (AC2)
    run_all_diagnostics_sync  -> asyncio.run(run_all_diagnostics())      (AC4)
    run_category_sync         -> asyncio.run(run_category(...))          (AC4)

That is safe today, and the reason is specific rather than incidental: nothing
those coroutines await is a shared asyncio primitive.  The one shared-primitive
system in this server is the embedding coalescer / 4-lane governor
(``services/coalescer_registry.py`` + ``services/governed_call.py``), whose
``asyncio.Semaphore`` lanes are constructed ONCE in ``lifespan.py`` and are
therefore bound to the main event loop.  Awaiting one of those from a private
loop is the classic cross-loop trap: it either raises or, worse, binds to a loop
that nobody else waits on, so the concurrency bound silently stops bounding.

The trap is latent, not present -- which is exactly why it needs a guard. If a
future change routes an embedding through, say, the regex path (a reranker on
regex hits would do it), nothing would fail loudly at review time.

Mechanism: an AST scan of the specific functions on those paths, plus the whole
modules they delegate into.  It deliberately does NOT assert "coalescer_registry
is absent from sys.modules" -- ``mcp/handlers/search.py`` legitimately imports it
inside the SEMANTIC search helpers (_compute_shared_query_vector /
_compute_memory_query_vector), which are async-dispatched on the main loop and
must keep using it. The invariant is about which CODE PATHS reach it, not which
modules exist.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Callable, List, Set

from code_indexer.server.mcp.handlers import search as search_handlers
from code_indexer.server.services import diagnostics_service as ds_module

# Names whose presence on an offloaded path signals the cross-loop hazard: the
# registry itself, the governed-call façade over it, and its per-request entry
# points.
_FORBIDDEN_NAMES = frozenset(
    {
        "coalescer_registry",
        "governed_call",
        "coalesced_query_embedding",
        "get_coalescer_registry",
        "governed_embedding_call",
    }
)

# Functions that execute INSIDE the private loop (or that establish it), listed
# explicitly so this guard states its own scope rather than guessing at one.
_OFFLOADED_FUNCTIONS: List[Callable[..., object]] = [
    search_handlers.handle_regex_search_sync,
    search_handlers.handle_regex_search,
    search_handlers._omni_regex_search,
    search_handlers._execute_regex_search,
    search_handlers._validate_regex_args,
    ds_module.DiagnosticsService.run_all_diagnostics_sync,
    ds_module.DiagnosticsService.run_category_sync,
    ds_module.DiagnosticsService.run_all_diagnostics,
    ds_module.DiagnosticsService.run_category,
]

# Whole modules those paths delegate into. Scanned entirely: any reference
# anywhere in them is reachable from the offloaded work.
#
# SCOPE LIMITATION (dual-review item 8, stated explicitly rather than implied):
# this guard is only as complete as this list. The scan walks the named
# functions plus these modules -- it does NOT transitively follow calls into
# arbitrary further modules. A violation introduced in some other helper that
# an offloaded path happens to call would therefore be invisible to it.
# `mcp/handlers/_utils.py` is included below precisely because
# `_execute_regex_search` delegates into it (payload truncation, wiki
# enrichment, the shared `_mcp_response`), which the earlier revision missed.
# ANY new delegation target on an offloaded path MUST be added here.
_OFFLOADED_MODULES = [
    "code_indexer.global_repos.regex_search",
    "code_indexer.global_repos.trigram_index_manager",
    "code_indexer.server.mcp.handlers._utils",
    "code_indexer.server.services.diagnostics_service",
]


def _referenced_names(source: str) -> Set[str]:
    """Every identifier, attribute and imported module name in ``source``."""
    # textwrap.dedent, NOT inspect.cleandoc: cleandoc treats the first line
    # specially (it is written for docstrings) and mangles the source of an
    # indented method, making every parse raise IndentationError.
    tree = ast.parse(textwrap.dedent(source))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_detector_is_not_blind() -> None:
    """Positive control: the scan must really flag a coalescer user.

    Both guards below assert an ABSENCE, so a broken detector would make them
    pass forever while proving nothing. _compute_shared_query_vector is on the
    SEMANTIC search path -- async-dispatched on the main loop, and legitimately
    a coalescer user -- so it is the natural known-positive to check against.
    """
    hits = (
        _referenced_names(
            inspect.getsource(search_handlers._compute_shared_query_vector)
        )
        & _FORBIDDEN_NAMES
    )
    assert hits, (
        "the AST scan failed to detect a KNOWN coalescer user, so the two "
        "absence guards below would never catch a real violation"
    )


def test_offloaded_function_bodies_never_reference_the_coalescer() -> None:
    """No function running on a private loop may reach the shared governor."""
    offenders = {}
    for func in _OFFLOADED_FUNCTIONS:
        found = _referenced_names(inspect.getsource(func)) & _FORBIDDEN_NAMES
        if found:
            offenders[f"{func.__module__}.{func.__qualname__}"] = sorted(found)

    assert not offenders, (
        "these functions execute on a PRIVATE event loop inside a worker thread "
        "and must not touch the lifespan-built coalescer/governor, whose "
        f"asyncio.Semaphore lanes belong to the main loop: {offenders}"
    )


def test_offloaded_modules_never_reference_the_coalescer() -> None:
    """The modules those paths delegate into must be clean as well."""
    import importlib

    offenders = {}
    for module_name in _OFFLOADED_MODULES:
        module_file = importlib.import_module(module_name).__file__
        assert module_file is not None, f"{module_name} has no source file"
        found = (
            _referenced_names(Path(module_file).read_text(encoding="utf-8"))
            & _FORBIDDEN_NAMES
        )
        if found:
            offenders[module_name] = sorted(found)

    assert not offenders, (
        "these modules are reached from work running on a private event loop and "
        "must not touch the lifespan-built coalescer/governor: "
        f"{offenders}"
    )
