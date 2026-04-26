"""
Codex CLI Startup Integration (Story #846).

Initializes Codex credential management on server startup based on the
CodexIntegrationConfig credential_mode:

  none        -- No-op; assumes machine-level credentials or no Codex usage.
  api_key     -- Writes OPENAI_API_KEY env var; sets CODEX_HOME; does not
                 write auth.json (Codex reads the key from the environment).
  subscription -- Checks out an OpenAI-vendor lease from llm-creds-provider,
                 writes auth.json under CODEX_HOME, and returns a shutdown
                 callable that returns the lease and removes auth.json.
                 If the lease acquisition fails, the startup succeeds but
                 Codex will run without managed credentials (AC3: job skipped,
                 not errored; failure logged at WARNING level).

CODEX_HOME is always set to <server_data_dir>/codex-home for api_key and
subscription modes so the Codex CLI finds its expected directory structure.
Uses CIDX_SERVER_DATA_DIR for path resolution (Bug #879 pattern).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
from code_indexer.server.services.codex_credentials_file_manager import (
    CodexCredentialsFileManager,
)
from code_indexer.server.services.codex_lease_loop import (
    CodexLeaseLoop,
    _CODEX_STATE_FILENAME,
)
from code_indexer.server.services.llm_creds_client import LlmCredsClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_transport() -> httpx.BaseTransport:
    """Return the default httpx transport (overridable in tests)."""
    return httpx.HTTPTransport()


def _normalize_server_dir(server_data_dir: str) -> Path:
    """
    Strip, expand, and resolve server_data_dir into an absolute Path.

    Args:
        server_data_dir: Raw server data directory string from caller.

    Returns:
        Normalized absolute Path ready for sub-path construction.

    Raises:
        ValueError: If server_data_dir is None, empty, or whitespace-only.
    """
    if not server_data_dir or not server_data_dir.strip():
        raise ValueError("server_data_dir must not be empty")
    return Path(server_data_dir.strip()).expanduser().resolve()


def _ensure_codex_home(base_dir: Path) -> Path:
    """
    Ensure codex-home directory exists under base_dir and set CODEX_HOME.

    os.environ mutation is intentional here: setting CODEX_HOME in the process
    environment ensures all child subprocesses (including ``codex mcp add`` and
    any subsequently spawned Codex CLI calls) inherit the correct home directory
    without requiring callers to pass it explicitly on every subprocess invocation.

    Args:
        base_dir: Normalized absolute server data directory Path.

    Returns:
        The codex-home Path (created if absent).
    """
    codex_home = base_dir / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_HOME"] = str(codex_home)
    logger.debug("CODEX_HOME set to %s", codex_home)
    return codex_home


def _login_codex_with_api_key(
    codex_home: Path,
    api_key: str,
    timeout_seconds: int = 30,
) -> bool:
    """Run `codex login --with-api-key` to populate auth.json non-interactively.

    Codex owns the auth.json schema for api_key mode. Reading it via stdin
    avoids leaking the key on the process command line. Returns True on
    successful login, False on any failure (logged at WARNING).

    Args:
        codex_home: Path to the CODEX_HOME directory.
        api_key: OpenAI API key to pass via stdin.
        timeout_seconds: Subprocess timeout. Defaults to 30.

    Returns:
        True on success (exit code 0), False on any failure.
    """
    if not api_key or not api_key.strip():
        logger.warning("Cannot login Codex with empty api_key")
        return False
    api_key = api_key.strip()
    cmd = ["codex", "login", "--with-api-key"]
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    try:
        result = subprocess.run(
            cmd,
            input=api_key.encode("utf-8"),
            env=env,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace")[
                :_MAX_STDERR_LOG_CHARS
            ]
            logger.warning(
                "`codex login --with-api-key` failed (exit %d): %s",
                result.returncode,
                stderr_text,
            )
            return False
        logger.info("Codex login (api_key mode) completed successfully")
        return True
    except subprocess.TimeoutExpired:
        logger.warning(
            "`codex login --with-api-key` timed out — auth.json may not be populated"
        )
        return False
    except FileNotFoundError:
        logger.warning(
            "codex binary not found on PATH — Codex feature effectively disabled"
        )
        return False
    except Exception as exc:
        logger.warning("Unexpected error running `codex login --with-api-key`: %s", exc)
        return False


def _handle_api_key_mode(api_key: str, base_dir: Path) -> None:
    """
    Set OPENAI_API_KEY and CODEX_HOME; delegate auth.json to codex login.

    Calls `codex login --with-api-key` (stdin) so codex owns the auth.json
    schema for api_key mode. Also sets OPENAI_API_KEY as belt-and-suspenders
    fallback for codex code paths that read the env var directly.

    Args:
        api_key: Non-empty, non-whitespace OpenAI API key from config.
        base_dir: Normalized absolute server data directory Path.

    Raises:
        ValueError: If api_key is empty or whitespace-only.
    """
    if not api_key or not api_key.strip():
        raise ValueError("api_key must not be empty in api_key credential_mode")
    codex_home = _ensure_codex_home(base_dir)
    os.environ["OPENAI_API_KEY"] = api_key.strip()
    login_succeeded = _login_codex_with_api_key(
        codex_home=codex_home, api_key=api_key.strip()
    )
    if not login_succeeded:
        logger.warning(
            "Codex login via api_key mode failed; continuing with OPENAI_API_KEY env var fallback"
        )
    logger.info("Codex api_key mode: OPENAI_API_KEY configured, CODEX_HOME set")


def _handle_subscription_mode(
    lcp_url: str,
    lcp_api_key: str,
    base_dir: Path,
    return_shutdown_hook: bool,
) -> Optional[Callable[[], None]]:
    """
    Acquire an OpenAI-vendor lease and write auth.json.

    Per AC3 of Story #846: if lease acquisition fails, the function returns
    None (job skipped, not errored) and logs at WARNING level.

    Args:
        lcp_url: Non-empty llm-creds-provider URL.
        lcp_api_key: Non-empty API key for the provider.
        base_dir: Normalized absolute server data directory Path.
        return_shutdown_hook: When True and lease acquired, return a callable
            that returns the lease and removes auth.json on server shutdown.

    Returns:
        Shutdown callable when ``return_shutdown_hook`` is True and the
        lease was acquired successfully.  ``None`` otherwise.

    Raises:
        ValueError: If lcp_url or lcp_api_key is empty or whitespace-only.
    """
    if not lcp_url or not lcp_url.strip():
        raise ValueError("lcp_url must not be empty in subscription credential_mode")
    if not lcp_api_key or not lcp_api_key.strip():
        raise ValueError("api_key must not be empty in subscription credential_mode")

    codex_home = _ensure_codex_home(base_dir)
    transport = _make_transport()
    client = LlmCredsClient(
        provider_url=lcp_url.strip(),
        api_key=lcp_api_key.strip(),
        transport=transport,
    )
    state_mgr = LlmLeaseStateManager(
        server_dir_path=str(base_dir),
        state_filename=_CODEX_STATE_FILENAME,
    )
    creds_mgr = CodexCredentialsFileManager(auth_json_path=codex_home / "auth.json")
    loop = CodexLeaseLoop(
        client=client,
        state_manager=state_mgr,
        credentials_manager=creds_mgr,
    )
    ok = loop.start(consumer_id="cidx-server")
    if not ok:
        # AC3: lease failure => job skipped (not errored), WARNING logged.
        logger.warning(
            "Codex subscription mode: lease acquisition failed -- "
            "Codex CLI will run without managed credentials (job will be skipped)"
        )
        return None

    logger.info("Codex subscription mode: lease acquired, auth.json written")

    if return_shutdown_hook:

        def shutdown() -> None:
            loop.stop()
            logger.info("Codex subscription lease returned on shutdown")

        return shutdown
    return None


# ---------------------------------------------------------------------------
# Story #848: MCP registration helper
# ---------------------------------------------------------------------------

# FIXME (Story #848 follow-up): The default cidx-local MCP launcher command is
# not yet implemented. Codex-cli's `mcp add ... -- <stdio command>` requires a
# real stdio-mode launcher, but cidx currently only exposes its MCP via HTTP
# transport (see MCPSelfRegistrationService for the pattern Claude uses). A
# follow-up story is needed to either:
#   (a) implement `cidx mcp serve` as a stdio launcher in cli.py, OR
#   (b) verify codex-cli `mcp add` supports HTTP transport with custom auth
#       headers, then use the existing HTTP+Basic-auth pattern from
#       MCPSelfRegistrationService.
# Until then, this default produces no registration call (empty command is
# detected and skipped with an INFO log). Operators can override via the
# optional `cidx_mcp_command` parameter in `_ensure_codex_mcp_registered`.
_DEFAULT_CIDX_MCP_COMMAND = ""  # See FIXME above

# Default subprocess timeout for `codex mcp add`.  Callers may override via
# _ensure_codex_mcp_registered's timeout parameter.
_DEFAULT_CODEX_MCP_ADD_TIMEOUT_SECONDS = 30

# Maximum number of characters from subprocess stderr included in WARNING logs.
# Truncates long error streams to keep log lines readable.
_MAX_STDERR_LOG_CHARS = 500


def _ensure_codex_mcp_registered(
    codex_home: Path,
    cidx_mcp_command: str,
    timeout: int = _DEFAULT_CODEX_MCP_ADD_TIMEOUT_SECONDS,
) -> None:
    """
    Idempotently register the cidx-local MCP server in CODEX_HOME.

    Runs once at CIDX server startup.  ``codex mcp add`` is idempotent —
    re-registration with the same name is a no-op.

    Non-zero exit codes and TimeoutExpired are both logged at WARNING level
    and do not propagate as exceptions (registration failure is non-fatal).

    Args:
        codex_home: Path to the CODEX_HOME directory.
        cidx_mcp_command: Shell command string used to launch the CIDX MCP
            server. Parsed with ``shlex.split`` so quoted arguments and
            escaped spaces are handled correctly.
        timeout: Subprocess timeout seconds. Defaults to
            _DEFAULT_CODEX_MCP_ADD_TIMEOUT_SECONDS.
    """
    if not cidx_mcp_command or not cidx_mcp_command.strip():
        logger.info(
            "cidx-local MCP registration skipped — no launcher command configured "
            "(see _DEFAULT_CIDX_MCP_COMMAND FIXME for follow-up story)"
        )
        return

    cmd_parts = shlex.split(cidx_mcp_command)
    cmd = ["codex", "mcp", "add", "cidx-local", "--"] + cmd_parts
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            stderr_text = proc.stderr.decode(errors="replace")[:_MAX_STDERR_LOG_CHARS]
            logger.warning(
                "codex mcp add returned non-zero exit %d — cidx-local may not be registered; "
                "stderr: %s",
                proc.returncode,
                stderr_text,
            )
    except subprocess.TimeoutExpired:
        logger.warning(
            "codex mcp add timed out — cidx-local may not be registered in CODEX_HOME"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def initialize_codex_manager_on_startup(
    server_config: Any,
    server_data_dir: str,
    return_shutdown_hook: bool = False,
) -> Optional[Callable[[], None]]:
    """
    Initialize Codex credential management during server startup.

    Args:
        server_config: The ServerConfig instance exposing
            ``codex_integration_config``.
        server_data_dir: Absolute path to the server data directory (used to
            derive CODEX_HOME = <server_data_dir>/codex-home).
        return_shutdown_hook: When True and subscription mode succeeds,
            return a callable that returns the lease and removes auth.json.

    Returns:
        A shutdown callable when subscription mode is active and
        ``return_shutdown_hook`` is True; ``None`` otherwise.

    Raises:
        ValueError: If ``server_config`` is None, ``server_data_dir`` is
            empty/whitespace, ``api_key`` is empty in api_key mode, ``lcp_url``
            or ``api_key`` is empty in subscription mode, or ``credential_mode``
            is not one of the supported values (``none``, ``api_key``,
            ``subscription``).
    """
    if server_config is None:
        raise ValueError("server_config must not be None")

    base_dir = _normalize_server_dir(server_data_dir)

    codex_config = getattr(server_config, "codex_integration_config", None)
    if codex_config is None or not codex_config.enabled:
        logger.debug("Codex integration disabled -- skipping startup")
        return None

    credential_mode: str = codex_config.credential_mode

    if credential_mode == "none":
        logger.info("Codex credential_mode=none -- no credential management")
        return None

    # Collect shutdown hook from whichever credential mode applies, then
    # perform MCP registration once in the shared path below.
    shutdown_hook: Optional[Callable[[], None]] = None

    if credential_mode == "api_key":
        _handle_api_key_mode(
            api_key=codex_config.api_key or "",
            base_dir=base_dir,
        )

    elif credential_mode == "subscription":
        shutdown_hook = _handle_subscription_mode(
            lcp_url=codex_config.lcp_url or "",
            lcp_api_key=codex_config.api_key or "",
            base_dir=base_dir,
            return_shutdown_hook=return_shutdown_hook,
        )

    else:
        raise ValueError(
            f"Unsupported Codex credential_mode: {credential_mode!r}. "
            "Expected one of: 'none', 'api_key', 'subscription'."
        )

    # Story #848: register cidx-local MCP server in CODEX_HOME after credentials
    # are in place (applies to both api_key and subscription modes).
    codex_home = base_dir / "codex-home"
    _ensure_codex_mcp_registered(
        codex_home=codex_home,
        cidx_mcp_command=_DEFAULT_CIDX_MCP_COMMAND,
    )
    return shutdown_hook
