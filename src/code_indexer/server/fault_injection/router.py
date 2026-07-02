"""
Fault injection admin REST router.

Story #746 — Phase D.

Exposes 11 admin endpoints for controlling the fault injection harness:

  GET    /admin/fault-injection/status
  GET    /admin/fault-injection/profiles
  GET    /admin/fault-injection/profiles/{target}
  PUT    /admin/fault-injection/profiles/{target}
  PATCH  /admin/fault-injection/profiles/{target}
  DELETE /admin/fault-injection/profiles/{target}
  DELETE /admin/fault-injection/profiles
  POST   /admin/fault-injection/reset
  POST   /admin/fault-injection/preview
  GET    /admin/fault-injection/history
  POST   /admin/fault-injection/seed

Guard rules (Scenarios 1, 5):
  - The FaultInjectionService is stored on FastAPI app.state as
    ``app.state.fault_injection_service``.
  - When that attribute is absent or None → 404.  The 404 hides the feature
    from callers when the bootstrap master switch is off.
  - Caller role != admin → 403 (handled by get_current_admin_user_hybrid).

Initialization:
  During startup, call ``set_service_on_app_state(app, svc)`` to register the
  active FaultInjectionService on app.state.  Tests create a bare FastAPI app
  and call the same helper; no module-level mutable state is involved.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_profile import (
    FaultProfile,
)

logger = logging.getLogger(__name__)

DOCS_URL = "/docs/fault-injection-operator-guide.md"

router = APIRouter(prefix="/admin/fault-injection", tags=["admin-fault-injection"])


# ---------------------------------------------------------------------------
# Startup wiring helper (no module-level mutable state)
# ---------------------------------------------------------------------------


def set_service_on_app_state(
    app: FastAPI, svc: Optional[FaultInjectionService]
) -> None:
    """Register a FaultInjectionService on FastAPI app.state for request handlers.

    Call once during startup wiring.  Tests call this on a bare FastAPI test
    instance to exercise the router without a real server lifecycle.

    When *svc* is None, request handlers will return 404 (harness inactive).

    Raises AssertionError if called a second time with a different (non-None)
    service instance than the one already registered — double-wire guard (N3).
    Calling with the same instance is idempotent and allowed.
    """
    existing = getattr(app.state, "fault_injection_service", None)
    if existing is not None and existing is not svc:
        raise AssertionError(
            f"double-wire detected: fault_injection_service is already set to "
            f"{existing!r} and cannot be overwritten with a different instance {svc!r}"
        )
    app.state.fault_injection_service = svc


# ---------------------------------------------------------------------------
# Per-request guard
# ---------------------------------------------------------------------------


def _require_service(request: Request) -> FaultInjectionService:
    """Return the active FaultInjectionService or raise HTTP 404.

    Reads exclusively from ``request.app.state.fault_injection_service``.
    A 404 is used (not 503) to hide the feature when the harness is inactive.
    """
    svc: Optional[FaultInjectionService] = getattr(
        request.app.state, "fault_injection_service", None
    )
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fault injection harness is not active on this server.",
        )
    return svc


# ---------------------------------------------------------------------------
# Pydantic request schemas
# ---------------------------------------------------------------------------


def _default_retry_after() -> List[int]:
    return [1, 5]


def _default_truncate_after() -> List[int]:
    return [50, 200]


def _default_latency_ms() -> List[int]:
    return [100, 500]


def _default_slow_tail_ms() -> List[int]:
    return [1000, 5000]


class FaultProfileRequest(BaseModel):
    """Body for PUT (full upsert) of a fault profile.

    All list fields use Field(default_factory) to avoid mutable default hazards.
    """

    target: str
    enabled: bool = True
    error_rate: float = 0.0
    error_codes: List[int] = Field(default_factory=list)
    retry_after_sec_range: List[int] = Field(default_factory=_default_retry_after)
    connect_timeout_rate: float = 0.0
    read_timeout_rate: float = 0.0
    write_timeout_rate: float = 0.0
    pool_timeout_rate: float = 0.0
    connect_error_rate: float = 0.0
    dns_failure_rate: float = 0.0
    tls_error_rate: float = 0.0
    malformed_rate: float = 0.0
    corruption_modes: List[str] = Field(default_factory=list)
    stream_disconnect_rate: float = 0.0
    truncate_after_bytes_range: List[int] = Field(
        default_factory=_default_truncate_after
    )
    redirect_loop_rate: float = 0.0
    latency_rate: float = 0.0
    latency_ms_range: List[int] = Field(default_factory=_default_latency_ms)
    slow_tail_rate: float = 0.0
    slow_tail_ms_range: List[int] = Field(default_factory=_default_slow_tail_ms)


class FaultProfilePatchRequest(BaseModel):
    """Body for PATCH (partial update) of an existing fault profile.

    All fields are Optional.  None means "not included in this PATCH request"
    (i.e., preserve existing value).  Non-None means "update to this value."

    For list fields: None = no change; an actual list (even empty) = replace.
    Field(default=None) is used rather than a bare = None to be explicit about
    the sentinel semantics and satisfy Pydantic v2 field declaration best practices.
    """

    enabled: Optional[bool] = Field(default=None)
    error_rate: Optional[float] = Field(default=None)
    error_codes: Optional[List[int]] = Field(default=None)
    retry_after_sec_range: Optional[List[int]] = Field(default=None)
    connect_timeout_rate: Optional[float] = Field(default=None)
    read_timeout_rate: Optional[float] = Field(default=None)
    write_timeout_rate: Optional[float] = Field(default=None)
    pool_timeout_rate: Optional[float] = Field(default=None)
    connect_error_rate: Optional[float] = Field(default=None)
    dns_failure_rate: Optional[float] = Field(default=None)
    tls_error_rate: Optional[float] = Field(default=None)
    malformed_rate: Optional[float] = Field(default=None)
    corruption_modes: Optional[List[str]] = Field(default=None)
    stream_disconnect_rate: Optional[float] = Field(default=None)
    truncate_after_bytes_range: Optional[List[int]] = Field(default=None)
    redirect_loop_rate: Optional[float] = Field(default=None)
    latency_rate: Optional[float] = Field(default=None)
    latency_ms_range: Optional[List[int]] = Field(default=None)
    slow_tail_rate: Optional[float] = Field(default=None)
    slow_tail_ms_range: Optional[List[int]] = Field(default=None)


class PreviewRequest(BaseModel):
    """Body for POST /preview."""

    url: str


class SeedRequest(BaseModel):
    """Body for POST /seed."""

    seed: int


# ---------------------------------------------------------------------------
# Internal converters — explicit typed helpers, no type: ignore escapes
# ---------------------------------------------------------------------------


def _to_int_pair(values: List[int]) -> Tuple[int, int]:
    """Convert a 2-element list to an (int, int) typed pair.

    Raises ValueError when the list does not have exactly two elements.
    """
    if len(values) != 2:
        raise ValueError(f"Expected a [min, max] pair of two integers, got {values!r}")
    return (values[0], values[1])


def _profile_to_dict(target: str, profile: FaultProfile) -> Dict[str, Any]:
    return {
        "target": target,
        "enabled": profile.enabled,
        "error_rate": profile.error_rate,
        "error_codes": list(profile.error_codes),
        "retry_after_sec_range": list(profile.retry_after_sec_range),
        "connect_timeout_rate": profile.connect_timeout_rate,
        "read_timeout_rate": profile.read_timeout_rate,
        "write_timeout_rate": profile.write_timeout_rate,
        "pool_timeout_rate": profile.pool_timeout_rate,
        "connect_error_rate": profile.connect_error_rate,
        "dns_failure_rate": profile.dns_failure_rate,
        "tls_error_rate": profile.tls_error_rate,
        "malformed_rate": profile.malformed_rate,
        "corruption_modes": list(profile.corruption_modes),
        "stream_disconnect_rate": profile.stream_disconnect_rate,
        "truncate_after_bytes_range": list(profile.truncate_after_bytes_range),
        "redirect_loop_rate": profile.redirect_loop_rate,
        "latency_rate": profile.latency_rate,
        "latency_ms_range": list(profile.latency_ms_range),
        "slow_tail_rate": profile.slow_tail_rate,
        "slow_tail_ms_range": list(profile.slow_tail_ms_range),
    }


def _build_profile_from_request(req: FaultProfileRequest) -> FaultProfile:
    """Convert a FaultProfileRequest into a validated FaultProfile."""
    return FaultProfile(
        target=req.target,
        enabled=req.enabled,
        error_rate=req.error_rate,
        error_codes=list(req.error_codes),
        retry_after_sec_range=_to_int_pair(req.retry_after_sec_range),
        connect_timeout_rate=req.connect_timeout_rate,
        read_timeout_rate=req.read_timeout_rate,
        write_timeout_rate=req.write_timeout_rate,
        pool_timeout_rate=req.pool_timeout_rate,
        connect_error_rate=req.connect_error_rate,
        dns_failure_rate=req.dns_failure_rate,
        tls_error_rate=req.tls_error_rate,
        malformed_rate=req.malformed_rate,
        corruption_modes=list(req.corruption_modes),
        stream_disconnect_rate=req.stream_disconnect_rate,
        truncate_after_bytes_range=_to_int_pair(req.truncate_after_bytes_range),
        redirect_loop_rate=req.redirect_loop_rate,
        latency_rate=req.latency_rate,
        latency_ms_range=_to_int_pair(req.latency_ms_range),
        slow_tail_rate=req.slow_tail_rate,
        slow_tail_ms_range=_to_int_pair(req.slow_tail_ms_range),
    )


def _apply_patch(
    existing: FaultProfile, patch: FaultProfilePatchRequest
) -> FaultProfile:
    """Return a new FaultProfile with patched fields merged over existing values."""

    def _pair(
        new_val: Optional[List[int]], old_val: Tuple[int, int]
    ) -> Tuple[int, int]:
        if new_val is None:
            return old_val
        return _to_int_pair(new_val)

    return FaultProfile(
        target=existing.target,
        enabled=patch.enabled if patch.enabled is not None else existing.enabled,
        error_rate=(
            patch.error_rate if patch.error_rate is not None else existing.error_rate
        ),
        error_codes=(
            list(patch.error_codes)
            if patch.error_codes is not None
            else list(existing.error_codes)
        ),
        retry_after_sec_range=_pair(
            patch.retry_after_sec_range, existing.retry_after_sec_range
        ),
        connect_timeout_rate=(
            patch.connect_timeout_rate
            if patch.connect_timeout_rate is not None
            else existing.connect_timeout_rate
        ),
        read_timeout_rate=(
            patch.read_timeout_rate
            if patch.read_timeout_rate is not None
            else existing.read_timeout_rate
        ),
        write_timeout_rate=(
            patch.write_timeout_rate
            if patch.write_timeout_rate is not None
            else existing.write_timeout_rate
        ),
        pool_timeout_rate=(
            patch.pool_timeout_rate
            if patch.pool_timeout_rate is not None
            else existing.pool_timeout_rate
        ),
        connect_error_rate=(
            patch.connect_error_rate
            if patch.connect_error_rate is not None
            else existing.connect_error_rate
        ),
        dns_failure_rate=(
            patch.dns_failure_rate
            if patch.dns_failure_rate is not None
            else existing.dns_failure_rate
        ),
        tls_error_rate=(
            patch.tls_error_rate
            if patch.tls_error_rate is not None
            else existing.tls_error_rate
        ),
        malformed_rate=(
            patch.malformed_rate
            if patch.malformed_rate is not None
            else existing.malformed_rate
        ),
        corruption_modes=(
            list(patch.corruption_modes)
            if patch.corruption_modes is not None
            else list(existing.corruption_modes)
        ),
        stream_disconnect_rate=(
            patch.stream_disconnect_rate
            if patch.stream_disconnect_rate is not None
            else existing.stream_disconnect_rate
        ),
        truncate_after_bytes_range=_pair(
            patch.truncate_after_bytes_range, existing.truncate_after_bytes_range
        ),
        redirect_loop_rate=(
            patch.redirect_loop_rate
            if patch.redirect_loop_rate is not None
            else existing.redirect_loop_rate
        ),
        latency_rate=(
            patch.latency_rate
            if patch.latency_rate is not None
            else existing.latency_rate
        ),
        latency_ms_range=_pair(patch.latency_ms_range, existing.latency_ms_range),
        slow_tail_rate=(
            patch.slow_tail_rate
            if patch.slow_tail_rate is not None
            else existing.slow_tail_rate
        ),
        slow_tail_ms_range=_pair(patch.slow_tail_ms_range, existing.slow_tail_ms_range),
    )


def _extract_hostname_or_raise(url: str) -> str:
    """Extract the hostname from *url*, raising HTTP 400 on any failure."""
    try:
        hostname = urlparse(url).hostname
    except ValueError as exc:
        logger.warning("fault_injection preview: malformed URL %r: %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid URL: {url!r}",
        ) from exc
    if not hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL has no recognisable hostname: {url!r}",
        )
    return hostname


def _validate_target_or_raise(target: str) -> None:
    """Raise HTTP 400 if *target* contains characters that are invalid in a hostname.

    Valid targets are hostnames (e.g. api.voyageai.com) or wildcard patterns
    (e.g. *.voyageai.com).  Slashes indicate an accidentally-passed URL path
    component and must be rejected.
    """
    if "/" in target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid target {target!r}: targets must be hostnames, not URL paths. "
                "Slashes are not allowed."
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
def get_status(
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """
    Return harness status: enabled flag, profile count, counters summary, docs URL.

    Returns HTTP 404 when the harness is not active (bootstrap switch off).
    """
    svc = _require_service(request)
    counters_raw = svc.get_counters()
    counters_serializable = {
        f"{target}:{fault_type}": count
        for (target, fault_type), count in counters_raw.items()
    }
    return {
        "enabled": svc.enabled,
        "profile_count": len(svc.get_all_profiles()),
        "counters": counters_serializable,
        "docs_url": DOCS_URL,
    }


@router.get("/profiles")
def list_profiles(
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return all registered fault profiles."""
    svc = _require_service(request)
    profiles = svc.get_all_profiles()
    return {"profiles": [_profile_to_dict(target, p) for target, p in profiles.items()]}


@router.get("/profiles/{target}")
def get_profile(
    target: str,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return a single fault profile by target. Returns 404 if not found."""
    _validate_target_or_raise(target)
    svc = _require_service(request)
    profile = svc.get_profile(target)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No fault profile found for target '{target}'.",
        )
    return _profile_to_dict(target, profile)


@router.put("/profiles/{target}")
def upsert_profile(
    target: str,
    body: FaultProfileRequest,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Full upsert: create or replace a fault profile for the given target."""
    _validate_target_or_raise(target)
    svc = _require_service(request)
    adjusted_data = body.model_dump()
    adjusted_data["target"] = target
    adjusted = FaultProfileRequest(**adjusted_data)
    try:
        profile = _build_profile_from_request(adjusted)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    svc.register_profile(target, profile)
    logger.info(
        "Fault injection profile upserted for target=%r",
        target,
        extra={"target": target},
    )
    return _profile_to_dict(target, profile)


@router.patch("/profiles/{target}")
def patch_profile(
    target: str,
    body: FaultProfilePatchRequest,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Partial update: merge supplied fields into existing profile."""
    _validate_target_or_raise(target)
    svc = _require_service(request)
    existing = svc.get_profile(target)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No fault profile found for target '{target}'.",
        )
    try:
        updated = _apply_patch(existing, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    svc.register_profile(target, updated)
    logger.info(
        "Fault injection profile patched for target=%r",
        target,
        extra={"target": target},
    )
    return _profile_to_dict(target, updated)


@router.delete("/profiles/{target}")
def delete_profile(
    target: str,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Remove a single fault profile by target name."""
    _validate_target_or_raise(target)
    svc = _require_service(request)
    svc.remove_profile(target)
    return {"deleted": target}


@router.delete("/profiles")
def clear_profiles(
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Remove all registered fault profiles (does not reset counters or history)."""
    svc = _require_service(request)
    profiles = svc.get_all_profiles()
    count = len(profiles)
    for t in list(profiles.keys()):
        svc.remove_profile(t)
    return {"cleared": count}


@router.post("/reset")
def reset_harness(
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Clear all profiles, counters, and history atomically (Scenario 16)."""
    svc = _require_service(request)
    svc.reset()
    logger.info("Fault injection harness reset (profiles + counters + history cleared)")
    return {"reset": True}


@router.post("/preview")
def preview_match(
    body: PreviewRequest,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """
    Dry-run: return the profile that would match the given URL (no event recorded).

    Returns {matched: null} when no registered profile applies to the URL.
    Raises HTTP 400 when the URL has no recognisable hostname.
    """
    svc = _require_service(request)
    _extract_hostname_or_raise(body.url)
    profile = svc.match_profile_snapshot(body.url)
    if profile is None:
        return {"matched": None}
    # Use the snapshot's own target — avoids mismatch when disabled profiles share
    # the same hostname as an enabled wildcard profile (M2 fix).
    return {"matched": _profile_to_dict(profile.target, profile)}


@router.get("/history")
def get_history(
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return the bounded ring buffer of recent injection events."""
    svc = _require_service(request)
    events = svc.get_history()
    return {
        "history": [
            {
                "target": e.target,
                "fault_type": e.fault_type,
                "correlation_id": e.correlation_id,
            }
            for e in events
        ]
    }


@router.post("/seed")
def seed_rng(
    body: SeedRequest,
    request: Request,
    _current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Re-seed the internal RNG for deterministic injection sequences (Scenario 14)."""
    svc = _require_service(request)
    svc.set_seed(body.seed)
    return {"seeded": body.seed}
