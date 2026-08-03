"""Story #1491 AC5: research-assistant polling endpoint (and its sibling
routes) no longer do sync DB reads and markdown rendering on the event
loop (Finding B5, MEDIUM).

Every route handler in research_assistant.py performs zero essential
`await` -- ResearchAssistantService.poll_job (sync JobTracker DB query),
get_messages (sync SQLite read), and render_markdown (python-markdown
rendering) are all synchronous. Per report Finding B5 mitigation, these
routes should be plain `def` so FastAPI dispatches them via its own
threadpool instead of directly on the shared event loop.

This test proves the mechanical fact (async def -> def) for every route
in the module; the pre-existing test_research_assistant_router*.py suite
(TestClient-driven) proves behavior is unchanged.
"""

import asyncio
import inspect

from code_indexer.server.routers import research_assistant


_ROUTE_HANDLER_NAMES = [
    "get_research_assistant_page",
    "send_message",
    "poll_job",
    "create_session",
    "rename_session",
    "delete_session",
    "load_session",
    "upload_file",
    "list_files",
    "delete_file",
    "download_file",
]


def test_no_route_handler_performs_an_await() -> None:
    """Sanity precondition: the module source contains zero `await`
    expressions, proving none of these handlers can lose functionality
    by becoming sync `def` (per AC5's "verify no essential await is being
    removed" requirement)."""
    source = inspect.getsource(research_assistant)
    assert "await " not in source, (
        "research_assistant.py must have zero await expressions for the "
        "async-to-sync conversion below to be safe"
    )


def test_all_research_assistant_routes_are_sync_def() -> None:
    """Every route handler must be a plain `def`, not `async def`, so
    FastAPI dispatches it via its own threadpool instead of directly on
    the shared event loop (Story #1491 AC5 / Finding B5)."""
    for name in _ROUTE_HANDLER_NAMES:
        handler = getattr(research_assistant, name)
        assert not asyncio.iscoroutinefunction(handler), (
            f"{name} must be sync def (async def blocks the event loop "
            "with no offload since it performs no await)"
        )
