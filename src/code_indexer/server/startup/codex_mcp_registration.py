"""
Codex MCP HTTP registration helpers (Story #848 HTTP follow-up).

Provides idempotent registration of the cidx-local MCP endpoint via HTTP
transport. Extracted from codex_cli_startup.py to keep that module under the
500-line MESSI rule 6 soft cap.

Public entry point consumed by codex_cli_startup:
    _ensure_codex_mcp_http_registered(codex_home, port, host, timeout)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (relocated from codex_cli_startup.py — values unchanged)
# ---------------------------------------------------------------------------

# Default subprocess timeout for `codex mcp add` / `codex mcp get`.
_DEFAULT_CODEX_MCP_ADD_TIMEOUT_SECONDS = 30

# Maximum number of characters from subprocess stderr included in WARNING logs.
_MAX_STDERR_LOG_CHARS = 500

# Name used when registering with codex mcp add/get.
_CIDX_MCP_NAME = "cidx-local"

# MCP endpoint path served by the CIDX HTTP server.
_MCP_PATH = "/mcp"

# Environment variable that codex reads to supply the Bearer token.
_MCP_BEARER_ENV_VAR = "CIDX_MCP_BEARER_TOKEN"

# Valid TCP port range.
_PORT_MIN = 1
_PORT_MAX = 65535

# `codex mcp get` exit code when the named MCP is absent (POSIX "not found").
_CODEX_MCP_GET_NOT_FOUND_RC = 1

# Default CIDX server port used as fallback when server_config does not expose port.
_DEFAULT_CIDX_SERVER_PORT = 8000


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def _validate_mcp_host_port(host: str, port: int) -> bool:
    """Return True when host and port are valid for MCP URL construction, else log + return False."""
    if not isinstance(host, str) or not host.strip():
        logger.warning(
            "cidx-local MCP HTTP registration skipped — invalid host %r (must be non-empty string)",
            host,
        )
        return False
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not (_PORT_MIN <= port <= _PORT_MAX)
    ):
        logger.warning(
            "cidx-local MCP HTTP registration skipped — invalid port %r (must be int 1..65535)",
            port,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Shared subprocess argument validation
# ---------------------------------------------------------------------------


def _validate_subprocess_env_and_timeout(op_name: str, env: dict, timeout: int) -> bool:
    """Return True when env and timeout are valid for a subprocess call, else log + return False.

    Args:
        op_name: Short name of the calling operation (e.g. "codex mcp get") for log messages.
        env: Environment dict to pass to subprocess.run.
        timeout: Subprocess timeout in seconds.

    Returns:
        True when both arguments are valid. False (after logging WARNING) otherwise.
    """
    if not isinstance(env, dict):
        logger.warning(
            "%s skipped — env must be a dict, got %r", op_name, type(env).__name__
        )
        return False
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        logger.warning(
            "%s skipped — invalid timeout %r (must be int > 0)", op_name, timeout
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def _codex_mcp_registration_matches(
    stdout_text: str, port: int, bearer_env_var: str
) -> bool:
    """Return True when stdout from `codex mcp get` confirms a matching HTTP registration.

    A registration matches when the stdout contains both the expected MCP URL
    (http://...:{port}/mcp) and the expected bearer-token env var name.

    Args:
        stdout_text: Decoded stdout string from `codex mcp get cidx-local`.
        port: Expected TCP port in the MCP URL.
        bearer_env_var: Expected bearer-token env var name.

    Returns:
        True when both the URL port marker and the bearer env var are found in stdout_text.
    """
    port_marker = f":{port}{_MCP_PATH}"
    return port_marker in stdout_text and bearer_env_var in stdout_text


# ---------------------------------------------------------------------------
# Idempotency pre-check
# ---------------------------------------------------------------------------


def _codex_mcp_is_already_registered(
    env: dict, timeout: int, port: int, bearer_env_var: str
) -> Optional[bool]:
    """Run `codex mcp get cidx-local`. Return True=current, False=absent/stale, None=error/skip.

    Exit 0 = present; stdout is inspected via _codex_mcp_registration_matches to
    distinguish a current registration (True) from a stale one (False).
    Exit 1 = absent (POSIX "not found") — returns False.
    Any other non-zero exit returns None so the caller skips the add call.

    Args:
        env: Environment dict for the subprocess (must contain CODEX_HOME).
        timeout: Subprocess timeout in seconds.
        port: Expected TCP port in the MCP URL (used for staleness check).
        bearer_env_var: Expected bearer-token env var name (used for staleness check).
    """
    if not _validate_subprocess_env_and_timeout("codex mcp get", env, timeout):
        return None
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not (_PORT_MIN <= port <= _PORT_MAX)
    ):
        logger.warning(
            "codex mcp get skipped — invalid port %r (must be int 1..65535)", port
        )
        return None
    if not isinstance(bearer_env_var, str) or not bearer_env_var.strip():
        logger.warning(
            "codex mcp get skipped — bearer_env_var must be a non-empty string, got %r",
            bearer_env_var,
        )
        return None
    get_cmd = ["codex", "mcp", "get", _CIDX_MCP_NAME]
    try:
        result = subprocess.run(
            get_cmd, env=env, check=False, capture_output=True, timeout=timeout
        )
        if result.returncode == 0:
            stdout_text = result.stdout.decode(errors="replace")
            if _codex_mcp_registration_matches(stdout_text, port, bearer_env_var):
                return True
            logger.debug(
                "codex mcp get cidx-local rc=0 but registration is stale — will remove and re-add"
            )
            return False
        if result.returncode == _CODEX_MCP_GET_NOT_FOUND_RC:
            logger.debug(
                "codex mcp get cidx-local rc=%d — not yet registered", result.returncode
            )
            return False
        stderr_text = result.stderr.decode(errors="replace")[:_MAX_STDERR_LOG_CHARS]
        logger.warning(
            "codex mcp get returned unexpected exit %d — skipping HTTP registration; stderr: %s",
            result.returncode,
            stderr_text,
        )
        return None
    except FileNotFoundError:
        logger.warning(
            "codex binary not found — cidx-local MCP HTTP registration skipped"
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "codex mcp get timed out — skipping cidx-local MCP HTTP registration"
        )
        return None


# ---------------------------------------------------------------------------
# Add and orchestration helpers
# ---------------------------------------------------------------------------


def _codex_mcp_remove(env: dict, timeout: int) -> None:
    """Run `codex mcp remove cidx-local`. Non-fatal on all errors.

    Called before re-adding a stale registration so codex mcp add does not fail
    with a "name already exists" error. All exceptions are caught and logged so
    the stale-registration recovery path always continues.

    Args:
        env: Environment dict for the subprocess (must contain CODEX_HOME).
        timeout: Subprocess timeout in seconds.
    """
    if not _validate_subprocess_env_and_timeout("codex mcp remove", env, timeout):
        return
    remove_cmd = ["codex", "mcp", "remove", _CIDX_MCP_NAME]
    try:
        result = subprocess.run(
            remove_cmd, env=env, check=False, capture_output=True, timeout=timeout
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="replace")[:_MAX_STDERR_LOG_CHARS]
            logger.warning(
                "codex mcp remove returned non-zero exit %d — "
                "stale cidx-local entry may persist; stderr: %s",
                result.returncode,
                stderr_text,
            )
        else:
            logger.debug("codex mcp remove cidx-local completed (exit 0)")
    except subprocess.TimeoutExpired:
        logger.warning(
            "codex mcp remove timed out — stale cidx-local entry may persist"
        )
    except FileNotFoundError:
        logger.warning("codex binary not found — codex mcp remove skipped")
    except Exception as exc:
        logger.warning(
            "Unexpected error running codex mcp remove — re-registration will proceed: %s",
            exc,
        )


def _run_codex_mcp_http_add(mcp_url: str, env: dict, timeout: int) -> None:
    """Run `codex mcp add cidx-local --url <url> --bearer-token-env-var ...`. Non-fatal."""
    add_cmd = [
        "codex",
        "mcp",
        "add",
        _CIDX_MCP_NAME,
        "--url",
        mcp_url,
        "--bearer-token-env-var",
        _MCP_BEARER_ENV_VAR,
    ]
    try:
        result = subprocess.run(
            add_cmd, env=env, check=False, capture_output=True, timeout=timeout
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="replace")[:_MAX_STDERR_LOG_CHARS]
            logger.warning(
                "codex mcp add (HTTP) returned non-zero exit %d — "
                "cidx-local may not be registered; stderr: %s",
                result.returncode,
                stderr_text,
            )
        else:
            logger.info("cidx-local MCP registered via HTTP transport at %s", mcp_url)
    except subprocess.TimeoutExpired:
        logger.warning(
            "codex mcp add (HTTP) timed out — cidx-local may not be registered in CODEX_HOME"
        )
    except FileNotFoundError:
        logger.warning("codex binary not found — cidx-local MCP HTTP add skipped")


def _ensure_codex_mcp_http_registered(
    codex_home: Path,
    port: int,
    host: str,
    timeout: int = _DEFAULT_CODEX_MCP_ADD_TIMEOUT_SECONDS,
) -> None:
    """Idempotently register cidx-local MCP via HTTP transport. Non-fatal on all errors.

    Args:
        codex_home: Path to the CODEX_HOME directory.
        port: TCP port the CIDX server listens on (1..65535).
        host: Hostname the CIDX server binds to. Must be supplied by the caller —
            typically derived from server_config.host with bind-all addresses
            (0.0.0.0, "") normalised to "localhost" before this call.
        timeout: Subprocess timeout seconds for get and add calls.
    """
    if not _validate_mcp_host_port(host, port):
        return
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    registered = _codex_mcp_is_already_registered(
        env, timeout, port, _MCP_BEARER_ENV_VAR
    )
    if registered is None:
        return  # error already logged by helper
    if registered:
        logger.info("cidx-local MCP already registered in CODEX_HOME — skipping add")
        return
    # registered is False: either absent or stale. Remove first (non-fatal no-op when absent),
    # then add with the current URL and bearer env var.
    _codex_mcp_remove(env, timeout)
    mcp_url = f"http://{host.strip()}:{port}{_MCP_PATH}"
    _run_codex_mcp_http_add(mcp_url, env, timeout)
