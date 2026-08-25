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

This function does NOT take a TelemetryManager -- only the MANAGER
instance is unavailable at app-construction time (it wraps the real OTEL
SDK TracerProvider/exporter setup, which create_fastapi_app() defers to
lifespan()); the telemetry CONFIG itself (server_config.telemetry_config)
is already available at construction time in app_wiring.py, from the
same config_service.get_config() call lifespan() uses, and that is what
the CALLER gates this function on (see app_wiring.py's create_fastapi_app()
-- calling instrument_fastapi() is skipped entirely when
telemetry_config.enabled is False, since OpenTelemetryMiddleware does
real per-request work -- attribute collection, context extraction,
counter/histogram calls -- even when the underlying tracer is a no-op,
so applying it unconditionally would add real overhead on every
deployment with telemetry disabled).

When the caller does instrument (telemetry enabled), FastAPIInstrumentor
is called with tracer_provider=None, so it resolves OTEL's global
ProxyTracerProvider at call time; the resulting ProxyTracer re-resolves
the REAL TracerProvider dynamically on every span start once
TelemetryManager calls trace.set_tracer_provider() later during lifespan
(telemetry/manager.py's _initialize_otel).

Usage:
    from src.code_indexer.server.telemetry.instrumentation import instrument_fastapi

    # Instrument the app immediately after construction, in
    # create_fastapi_app() -- NEVER inside lifespan() -- gated on
    # telemetry_config.enabled.
    if server_config.telemetry_config.enabled:
        instrument_fastapi(app)
"""

from __future__ import annotations

import logging
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
    # telemetry to be fully initialized yet.
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=exclude_pattern,
        tracer_provider=None,
    )


def _try_apply_instrumentation(app: "FastAPI", urls_to_exclude: List[str]) -> bool:
    """Apply instrumentation, logging the outcome. Returns success/failure."""
    try:
        _apply_instrumentation(app, urls_to_exclude)
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


def instrument_fastapi(
    app: "FastAPI",
    excluded_urls: Optional[List[str]] = None,
) -> bool:
    """
    Structurally instrument a FastAPI app with OTEL tracing.

    Bug #1679: call immediately after FastAPI(...) is constructed, never
    from inside lifespan() -- see the module docstring for the full
    rationale. Caller gates this call on telemetry_config.enabled (see
    app_wiring.py); this function performs no config-based gating itself.

    Returns True if instrumentation was applied (or already present on
    this app instance), False if the OTEL FastAPI package is unavailable
    or patching failed. Raises ValueError for a None app or invalid
    excluded_urls.
    """
    if app is None:
        raise ValueError("instrument_fastapi() requires a FastAPI app, got None")
    urls_to_exclude = _validate_excluded_urls(excluded_urls)

    # Per-app-instance guard: FastAPIInstrumentor stamps this attribute on
    # the instance it patches, so a fresh app instance always gets
    # instrumented independently (e.g. repeated create_app() in tests).
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        logger.debug("FastAPI app already instrumented, skipping")
        return True

    return _try_apply_instrumentation(app, urls_to_exclude)


def uninstrument_fastapi(app: "FastAPI") -> bool:
    """
    Remove OTEL instrumentation from a single FastAPI app instance.

    Delegates to the real, per-app FastAPIInstrumentor.uninstrument_app(app)
    static method. Deliberately NOT FastAPIInstrumentor().uninstrument():
    that instance method uninstruments EVERY app this process ever
    instrumented via the global instrument() path and unconditionally
    resets `fastapi.FastAPI = self._original_fastapi` -- None here, since
    this module only ever calls the per-app instrument_app(), never the
    global instrument() that would populate _original_fastapi. Calling
    the instance method would silently corrupt the fastapi.FastAPI class
    reference for the rest of the process.

    Returns True if uninstrumentation was performed, False if this app
    instance was not instrumented (or patching failed).
    """
    if app is None:
        raise ValueError("uninstrument_fastapi() requires a FastAPI app, got None")

    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        return False

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.uninstrument_app(app)
        logger.info("FastAPI OTEL instrumentation removed for this app instance")
        return True

    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-016", f"Failed to uninstrument FastAPI: {e}"
            )
        )
        return False
