"""
Factory for Authorization header closures used by CodexInvoker for cidx-local MCP.

v9.23.10: Codex MCP authentication now uses persistent MCPCredentialManager-issued
Basic auth credentials — the SAME path Claude uses via MCPSelfRegistrationService.
No JWT, no TTL, no expiration. A 30-min Pass 2 run never sees a mid-flow 401.

The closure returned by build_codex_mcp_auth_header_provider() produces the literal
value of the Authorization header: 'Basic <base64(client_id:client_secret)>'.
Codex reads this from the CIDX_MCP_AUTH_HEADER env var (set by CodexInvoker) via
config.toml entry: env_http_headers = { Authorization = "CIDX_MCP_AUTH_HEADER" }.

Two successive calls to the closure return the SAME string — credentials are
persistent (stored by MCPCredentialManager, not minted per-call).

Control flow inside the closure:
  1. Call MCPSelfRegistrationService.get_cached_auth_header_value() — fast path.
     Returns the cached 'Basic <b64>' string set during register_in_claude_code()
     (i.e. after Claude registration ran in this process).
  2. If None (cache miss — Claude not yet registered in this process), call
     build_auth_header_from_creds() which triggers ensure_registered() and
     returns the cached value once registration completes.
  3. If both return None, raise RuntimeError (Foundation 13: fail fast, no silent
     failure — a broken credential state must surface immediately).

Reference: https://developers.openai.com/codex/mcp
"""

from __future__ import annotations

import logging
from typing import Callable

from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)

logger = logging.getLogger(__name__)


def build_codex_mcp_auth_header_provider() -> Callable[[], str]:
    """Return a closure that produces the Authorization header value for cidx-local MCP.

    The closure delegates entirely to MCPSelfRegistrationService — no credential
    assembly occurs in this module. The service's cached header value (populated
    by register_in_claude_code() during Claude registration) is returned directly.

    Returns:
        A zero-argument callable returning 'Basic <base64(client_id:client_secret)>'.
        The same string is returned on every call (persistent credentials, no TTL).

    The returned closure raises RuntimeError in two cases (Foundation 13: fail fast):
      1. The MCPSelfRegistrationService singleton is None (not yet initialized).
      2. Both get_cached_auth_header_value() and build_auth_header_from_creds()
         return None (credential creation failed).
    """

    def _provider() -> str:
        service = MCPSelfRegistrationService.get_instance()
        if service is None:
            raise RuntimeError(
                "build_codex_mcp_auth_header_provider: MCPSelfRegistrationService singleton "
                "not set — cannot obtain MCP credentials for Codex auth"
            )

        # Fast path: use cached header from Claude registration (already done).
        header = service.get_cached_auth_header_value()
        if header is not None:
            return str(header)

        # Cache miss: trigger ensure_registered() which runs register_in_claude_code()
        # and populates the cache, then return the freshly cached value.
        header = service.build_auth_header_from_creds()
        if header is not None:
            return str(header)

        # Third fallback (bug #937): read stored credentials directly from config
        # without requiring Claude CLI. Works when Claude registration never ran
        # in this process (e.g. Claude CLI absent on staging server).
        header = service.build_header_from_stored_credentials()
        if header is not None:
            return str(header)

        # All three paths exhausted — credential state is broken.
        # Log ERROR before raising so the root cause is identifiable in monitoring
        # dashboards (MESSI Rule 13: Anti-Silent-Failure). The WARNING at the
        # CodexInvoker call site is insufficient — operators need ERROR here.
        logger.error(
            "build_codex_mcp_auth_header_provider: cidx-local MCP credentials missing "
            "from credential store — MCPSelfRegistrationService has no cached header, "
            "ensure_registered() returned None, and no stored credentials found in "
            "config.mcp_self_registration. Codex cannot authenticate with cidx-local MCP."
        )
        raise RuntimeError(
            "build_codex_mcp_auth_header_provider: unable to obtain Authorization header "
            "for Codex MCP — all three paths exhausted: cached header is None, "
            "build_auth_header_from_creds() returned None, and "
            "build_header_from_stored_credentials() returned None"
        )

    return _provider
