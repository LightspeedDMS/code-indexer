"""TOTP step-up elevation support for CIDX CLI remote mode (Story #980).

Provides:
- ElevationRequiredError: raised when an admin endpoint returns 403 elevation_required
- ElevationFailedError: raised when POST /auth/elevate returns 401 elevation_failed
- elevate(): calls POST /auth/elevate and returns True on success
- with_elevation_retry(): wraps an API call with automatic elevation + single retry

Algorithm (from story #980 spec):
  try:
    return fn()
  except ElevationRequiredError as err:
    if err.error_code == "totp_setup_required":
      print(setup message)
      sys.exit(1)
    totp_code = prompt_totp()
    try:
      elevate(session, server_url, token, totp_code)
    except ElevationFailedError:
      print(error message)
      sys.exit(1)
    return fn()   # single retry
"""

import sys
from typing import Any, Callable, Optional, TypeVar

from .base_client import APIClientError

T = TypeVar("T")


class ElevationRequiredError(Exception):
    """Raised when an admin endpoint returns 403 with an elevation error code.

    Attributes:
        error_code: Either "elevation_required" or "totp_setup_required".
        setup_url: URL for TOTP setup (only set when error_code == "totp_setup_required").
    """

    def __init__(
        self,
        error_code: str,
        setup_url: Optional[str] = None,
        message: str = "",
    ) -> None:
        super().__init__(message or f"Elevation required: {error_code}")
        self.error_code = error_code
        self.setup_url = setup_url


class ElevationFailedError(Exception):
    """Raised when POST /auth/elevate returns 401 (wrong TOTP code or replay)."""

    pass


def elevate(
    session: Any,
    server_url: str,
    token: str,
    totp_code: str,
) -> bool:
    """Call POST /auth/elevate with the given TOTP code.

    Args:
        session: httpx.Client session (or compatible object).
        server_url: Base URL of the CIDX server.
        token: Bearer token for the current admin session.
        totp_code: 6-digit TOTP code from the authenticator app.

    Returns:
        True on success (HTTP 200).

    Raises:
        ElevationFailedError: When server returns 401 (wrong code / replay).
        APIClientError: When server returns any other non-200 status.
    """
    url = f"{server_url.rstrip('/')}/auth/elevate"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"totp_code": totp_code}

    response = session.post(url, json=payload, headers=headers)

    if response.status_code == 200:
        return True

    if response.status_code == 401:
        raise ElevationFailedError("Invalid TOTP code. Elevation failed.")

    # Any other unexpected status
    try:
        detail = response.json().get("detail", f"HTTP {response.status_code}")
    except Exception:
        detail = f"HTTP {response.status_code}"

    raise APIClientError(f"Elevation request failed: {detail}", response.status_code)


def with_elevation_retry(
    fn: Callable[[], T],
    session: Any,
    server_url: str,
    token: str,
    prompt_totp: Callable[[], str],
) -> T:
    """Wrap an API call with automatic TOTP elevation + single retry.

    AC1: On elevation_required, prompts for TOTP, elevates, then retries once.
    AC2: On elevation_failed (wrong code), prints clear error and sys.exit(1).
    AC3: On totp_setup_required, prints actionable message with setup URL and sys.exit(1).

    Args:
        fn: Zero-argument callable that performs the API request.
        session: httpx.Client session used to call /auth/elevate.
        server_url: Base URL of the CIDX server.
        token: Bearer token for the current admin session.
        prompt_totp: Callable that returns the TOTP code entered by the user.

    Returns:
        Result of fn() on success.

    Raises:
        SystemExit(1): On totp_setup_required or elevation_failed.
        Any other exception from fn() is re-raised unchanged.
    """
    try:
        return fn()
    except ElevationRequiredError as err:
        if err.error_code == "totp_setup_required":
            setup_url = err.setup_url or "/admin/mfa/setup"
            print(
                f"TOTP setup required. Visit {setup_url} to configure your authenticator.",
                file=sys.stderr,
            )
            sys.exit(1)

        # elevation_required: prompt for TOTP code and elevate
        totp_code = prompt_totp()

        try:
            elevate(
                session=session,
                server_url=server_url,
                token=token,
                totp_code=totp_code,
            )
        except ElevationFailedError:
            print(
                "Invalid TOTP code. Elevation failed.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Single retry after successful elevation
        return fn()
