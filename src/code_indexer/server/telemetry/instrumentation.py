"""
FastAPI Auto-Instrumentation for OTEL Tracing (Story #697, fixed Bug #1679).

This module provides functions to instrument FastAPI applications with
OpenTelemetry tracing. It uses the official FastAPIInstrumentor for
automatic span creation on HTTP requests.

Bug #1679: FastAPIInstrumentor.instrument_app() works by monkey-patching
the app instance's `build_middleware_stack` method. Starlette builds that
stack LAZILY on the very first ASGI message the app receives -- which is
the "lifespan" startup message itself (`Starlette.__call__` checks
`self.middleware_stack is None` and builds it BEFORE dispatching to the
lifespan() context manager). So instrument_fastapi() MUST be called
immediately after `FastAPI(...)` is constructed (see
startup/app_wiring.py's create_fastapi_app()) -- calling it from inside
the lifespan() body (the pre-#1679 call site) is a structural no-op: no
exception is raised, but the patched build_middleware_stack never
executes and zero HTTP request spans are ever produced.

Because app construction happens before a TelemetryManager exists (its
config is resolved from the DB at lifespan time -- Story #1676 AC1), this
function does NOT take a TelemetryManager. FastAPIInstrumentor is called
with tracer_provider=None, so it resolves OTEL's global
ProxyTracerProvider at call time; the resulting ProxyTracer re-resolves
the REAL TracerProvider dynamically on every span start once
TelemetryManager calls trace.set_tracer_provider() later during lifespan
(telemetry/manager.py's _initialize_otel). When telemetry is disabled
entirely, no TracerProvider is ever set globally, so every span-start
call keeps resolving to OTEL's built-in NoOpTracer -- zero export
overhead, safe to call this function unconditionally regardless of
config.

Usage:
    from src.code_indexer.server.telemetry.instrumentation import instrument_fastapi

    # Instrument the app immediately after construction, in
    # create_fastapi_app() -- NEVER inside lifespan().
    instrument_fastapi(app)
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, List, Optional

from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Default endpoints to exclude from tracing (health checks, metrics)
DEFAULT_EXCLUDED_URLS: List[str] = [
    "health",
    "healthz",
    "ready",
    "readiness",
    "live",
    "liveness",
    "metrics",
    "favicon.ico",
]

# Tracks whether ANY app was ever successfully instrumented in this
# process -- used only by uninstrument_fastapi()/reset_instrumentation_state()
# as a convenience flag. instrument_fastapi() itself does NOT gate on
# this: the per-app-instance attribute
# `app._is_instrumented_by_opentelemetry` (set by FastAPIInstrumentor
# itself) is the correct re-instrumentation guard, since a process can
# legitimately construct more than one FastAPI app (e.g. repeated
# create_app() calls in tests) and each one must be instrumented
# independently. Guarded by _state_lock since app startup and test
# teardown can run this from different threads.
_is_instrumented: bool = False
_state_lock = threading.Lock()


def _validate_excluded_urls(excluded_urls: Optional[List[str]]) -> List[str]:
    """Validate and resolve the exclusion list, defaulting when omitted."""
    if excluded_urls is None:
        return DEFAULT_EXCLUDED_URLS
    if not isinstance(excluded_urls, list) or not all(
        isinstance(u, str) and u for u in excluded_urls
    ):
        raise ValueError(
            f"excluded_urls must be a list of non-empty strings, got: {excluded_urls!r}"
        )
    return excluded_urls


def _apply_instrumentation(app: "FastAPI", urls_to_exclude: List[str]) -> None:
    """Call the real FastAPIInstrumentor with the resolved exclusion list."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    exclude_pattern = "|".join(urls_to_exclude)

    # tracer_provider=None defers to OTEL's global tracer-provider
    # resolution (see module docstring) -- this call does not need
    # telemetry to be configured or even enabled yet.
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=exclude_pattern,
        tracer_provider=None,
    )


def instrument_fastapi(
    app: "FastAPI",
    excluded_urls: Optional[List[str]] = None,
) -> bool:
    """
    Structurally instrument a FastAPI application with OTEL tracing.

    Bug #1679: MUST be called immediately after `FastAPI(...)` is
    constructed (from create_fastapi_app()), never from inside the
    lifespan() async context manager -- see the module docstring above
    for the full rationale.

    Args:
        app: FastAPI application instance (required, must not be None)
        excluded_urls: Optional list of non-empty URL patterns to exclude
            from tracing

    Returns:
        True if instrumentation was applied (or was already present on
        this app instance), False if the OTEL FastAPI instrumentation
        package is unavailable or patching failed.

    Raises:
        ValueError: if `app` is None, or `excluded_urls` is supplied but
            is not a list of non-empty strings.
    """
    if app is None:
        raise ValueError("instrument_fastapi() requires a FastAPI app, got None")
    urls_to_exclude = _validate_excluded_urls(excluded_urls)

    # Per-app-instance guard, not a module-level global: FastAPIInstrumentor
    # stamps this attribute on the instance it patches, so re-running this
    # function against the SAME app is a safe no-op, while a DIFFERENT app
    # instance (e.g. a fresh create_app() in tests) still gets instrumented.
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        logger.debug("FastAPI app already instrumented, skipping")
        return True

    try:
        _apply_instrumentation(app, urls_to_exclude)
        with _state_lock:
            global _is_instrumented
            _is_instrumented = True
        logger.info(
            f"FastAPI instrumented with OTEL tracing "
            f"(excluded: {len(urls_to_exclude)} URL patterns)"
        )
        return True

    except ImportError as e:
        logger.warning(
            format_error_log(
                "QUERY-GENERAL-014",
                f"FastAPI instrumentation unavailable: {e}. "
                "Install opentelemetry-instrumentation-fastapi for auto-tracing.",
            )
        )
        return False
    except Exception as e:
        logger.error(
            format_error_log("QUERY-GENERAL-015", f"Failed to instrument FastAPI: {e}")
        )
        return False


def uninstrument_fastapi() -> bool:
    """
    Remove OTEL instrumentation from FastAPI.

    Returns:
        True if uninstrumentation was performed, False if not instrumented
    """
    with _state_lock:
        global _is_instrumented
        if not _is_instrumented:
            return False

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.uninstrument()
        with _state_lock:
            _is_instrumented = False
        logger.info("FastAPI OTEL instrumentation removed")
        return True

    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-016", f"Failed to uninstrument FastAPI: {e}"
            )
        )
        return False


def reset_instrumentation_state() -> None:
    """Reset the instrumentation state (for testing)."""
    with _state_lock:
        global _is_instrumented
        _is_instrumented = False
