"""
Codex MCP HTTP registration helpers (v9.23.10 TOML rewrite).

Registers the cidx-local MCP endpoint by writing $CODEX_HOME/config.toml
directly. Codex 0.125 `codex mcp add` has no --http-headers / --env-http-headers
flags, so direct TOML editing is required.

Public entry point consumed by codex_cli_startup:
    _ensure_codex_mcp_http_registered(codex_home, port, host)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Name used when registering the MCP server in config.toml.
_CIDX_MCP_NAME = "cidx-local"

# MCP endpoint path served by the CIDX HTTP server.
_MCP_PATH = "/mcp"

# Environment variable that codex reads and injects verbatim as the
# Authorization header value on every MCP HTTP request.
_MCP_AUTH_HEADER_ENV_VAR = "CIDX_MCP_AUTH_HEADER"

# Valid TCP port range — used for input validation in the entry point.
_PORT_MIN = 1
_PORT_MAX = 65535


# ---------------------------------------------------------------------------
# TOML read helper
# ---------------------------------------------------------------------------


def _read_toml(config_toml: Path) -> Tuple[Optional[Dict], Optional[str]]:
    """Read and parse config_toml. Returns (data_dict, None) on success.

    When config_toml does not exist, returns ({}, None) so the caller can
    proceed to create the file.

    When the file exists but contains invalid TOML, returns (None, error_str)
    so the caller can log and skip the write (preserving the broken file for
    operator inspection).

    IOErrors from an existing file propagate to the caller unchanged.
    """
    if not config_toml.exists():
        return {}, None
    try:
        import tomli

        with open(config_toml, "rb") as fh:
            return tomli.load(fh), None
    except Exception as exc:
        import tomli

        if isinstance(exc, tomli.TOMLDecodeError):
            return None, f"TOML parse error in {config_toml}: {exc}"
        raise


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


def _is_already_registered(data: Dict, url: str) -> bool:
    """Return True when config.toml already has a current cidx-local section.

    A registration is current when:
      - data["mcp_servers"]["cidx-local"]["url"] == url
      - data["mcp_servers"]["cidx-local"]["env_http_headers"]["Authorization"]
            == _MCP_AUTH_HEADER_ENV_VAR

    Any missing key or value mismatch returns False (stale or absent).
    """
    section = data.get("mcp_servers", {}).get(_CIDX_MCP_NAME, {})
    if not section:
        return False
    if section.get("url") != url:
        return False
    env_headers = section.get("env_http_headers", {})
    return env_headers.get("Authorization") == _MCP_AUTH_HEADER_ENV_VAR


# ---------------------------------------------------------------------------
# Section text builder
# ---------------------------------------------------------------------------


def _build_mcp_section_text(url: str) -> str:
    """Return the TOML text for the [mcp_servers.cidx-local] section."""
    return (
        f"[mcp_servers.{_CIDX_MCP_NAME}]\n"
        f'url = "{url}"\n'
        f"[mcp_servers.{_CIDX_MCP_NAME}.env_http_headers]\n"
        f'Authorization = "{_MCP_AUTH_HEADER_ENV_VAR}"\n'
    )


# ---------------------------------------------------------------------------
# Atomic TOML write
# ---------------------------------------------------------------------------


def _write_toml_atomic(config_toml: Path, url: str, existing_text: str) -> None:
    """Write config.toml atomically, replacing any existing cidx-local section.

    Uses a .tmp file + Path.replace() for cross-platform atomic overwrite
    semantics. The cidx-local section (and its env_http_headers sub-section)
    is removed from the existing text using multiline-anchored regex, then
    the new section is appended.

    v9.23.10: creates parent dirs on fresh CODEX_HOME deploys, and preserves
    existing file mode (defaulting to 0o600 for new files) so the renamed
    file does not inherit the process umask (typically 0644).
    """
    import os
    import stat

    pattern = re.compile(
        r"^\[mcp_servers\." + re.escape(_CIDX_MCP_NAME) + r"(?:\.[^\]]+)?\]"
        r".*?"
        r"(?=^\[(?!mcp_servers\." + re.escape(_CIDX_MCP_NAME) + r")|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    stripped = pattern.sub("", existing_text).rstrip("\n")
    separator = "\n\n" if stripped else ""
    new_text = stripped + separator + _build_mcp_section_text(url)

    config_toml.parent.mkdir(parents=True, exist_ok=True)

    if config_toml.exists():
        mode = stat.S_IMODE(config_toml.stat().st_mode)
    else:
        mode = 0o600

    tmp_file = config_toml.with_suffix(".toml.tmp")
    tmp_file.write_text(new_text, encoding="utf-8")
    os.chmod(tmp_file, mode)
    tmp_file.replace(config_toml)
    os.chmod(config_toml, mode)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _ensure_codex_mcp_http_registered(
    codex_home: Path,
    port: int,
    host: str,
) -> None:
    """Idempotently register cidx-local MCP in $CODEX_HOME/config.toml.

    Writes (or updates) the [mcp_servers.cidx-local] section so that codex
    injects CIDX_MCP_AUTH_HEADER as the Authorization header on every MCP
    HTTP request. Non-fatal: all errors are logged as WARNING and do not
    propagate.

    Args:
        codex_home: Path to the CODEX_HOME directory (must be Path or os.PathLike).
        port: TCP port the CIDX server listens on (1..65535).
        host: Hostname the CIDX server binds to (non-empty string).
    """
    import os

    if not isinstance(codex_home, (Path, os.PathLike)):
        logger.warning(
            "cidx-local MCP registration skipped — codex_home must be a Path, got %r",
            type(codex_home).__name__,
        )
        return
    if not isinstance(host, str) or not host.strip():
        logger.warning(
            "cidx-local MCP registration skipped — invalid host %r (must be non-empty string)",
            host,
        )
        return
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not (_PORT_MIN <= port <= _PORT_MAX)
    ):
        logger.warning(
            "cidx-local MCP registration skipped — invalid port %r (must be int %d..%d)",
            port,
            _PORT_MIN,
            _PORT_MAX,
        )
        return

    config_toml = Path(codex_home) / "config.toml"
    url = f"http://{host}:{port}{_MCP_PATH}"

    try:
        data, parse_error = _read_toml(config_toml)
        if parse_error is not None:
            logger.warning("cidx-local MCP registration skipped — %s", parse_error)
            return

        if _is_already_registered(data, url):
            logger.info(
                "cidx-local MCP already registered in %s — skipping write", config_toml
            )
            return

        existing_text = (
            config_toml.read_text(encoding="utf-8") if config_toml.exists() else ""
        )
        _write_toml_atomic(config_toml, url, existing_text)
        logger.info("cidx-local MCP registered in %s at %s", config_toml, url)

    except Exception as exc:
        logger.warning(
            "cidx-local MCP registration failed — %s: %s",
            type(exc).__name__,
            exc,
        )
