"""
Shared pure helpers for FaultInjectingTransport and FaultInjectingSyncTransport.

Story #746 — de-duplication of response-builder logic (STRONGLY-PREFERRED 1).

All functions here are pure (no I/O, no async, no sync-sleep) so they can be
imported by both transport variants without pulling in asyncio or time.

Only the transport-specific concerns remain in each transport module:
  - Sleep primitive (asyncio.sleep vs time.sleep)
  - CancelledError handling (async-only)
  - Dispatch method name (handle_async_request vs handle_request)
  - Stream base class (AsyncByteStream vs SyncByteStream)
"""

from __future__ import annotations

import json
import random

import httpx

from code_indexer.server.fault_injection.fault_profile import (
    FaultProfile,
    jitter_uniform,
)


def build_error_response(
    profile: FaultProfile,
    rng: random.Random,
    request: httpx.Request,
) -> httpx.Response:
    """Return a synthetic error response for the http_error outcome.

    Picks a random status code from profile.error_codes and adds a
    Retry-After header sampled from profile.retry_after_sec_range.
    """
    status_code = rng.choice(profile.error_codes)
    lo, hi = profile.retry_after_sec_range
    retry_after = jitter_uniform(lo, hi, rng)
    return httpx.Response(
        status_code=status_code,
        headers={"retry-after": str(retry_after)},
        content=b"",
        request=request,
    )


def _corrupt_json_payload(mode: str, rng: random.Random) -> bytes:
    """Return corrupted bytes for the given corruption mode.

    Supported modes:
      truncate      — valid JSON prefix, truncated mid-token
      invalid_utf8  — bytes that are not valid UTF-8
      wrong_schema  — a valid JSON object with unexpected structure
      empty         — empty body (not valid JSON)

    Raises:
        ValueError: If mode is not one of the four supported values.
    """
    if mode == "truncate":
        full = json.dumps({"corrupted": True}).encode()
        return full[: max(1, len(full) // 2)]
    if mode == "invalid_utf8":
        return b"\xff\xfe" + b"not valid utf-8 \x80\x81"
    if mode == "wrong_schema":
        return json.dumps({"unexpected_key": rng.randint(1, 9999)}).encode()
    if mode == "empty":
        return b""
    raise ValueError(
        f"unknown corruption mode: {mode!r}. "
        "Allowed: truncate, invalid_utf8, wrong_schema, empty"
    )


def build_corrupted_json_response(
    profile: FaultProfile,
    rng: random.Random,
    request: httpx.Request,
) -> httpx.Response:
    """Return a 200 response with a corrupted JSON body for the malformed_json outcome.

    Supported corruption_modes: truncate, invalid_utf8, wrong_schema, empty.
    Raises ValueError if profile.corruption_modes contains an unknown mode.
    """
    mode = rng.choice(profile.corruption_modes)
    body = _corrupt_json_payload(mode, rng)
    return httpx.Response(
        status_code=200,
        content=body,
        request=request,
    )


def build_redirect_302(request: httpx.Request) -> httpx.Response:
    """Return a 302 whose Location header points at the original request URL."""
    return httpx.Response(
        status_code=302,
        headers={"location": str(request.url)},
        content=b"",
        request=request,
    )
