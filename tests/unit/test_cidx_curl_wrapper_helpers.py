"""Shared constants and helper functions for cidx-curl.sh wrapper tests (Story #929 Item #2a).

Fixtures (isolated_config, fake_curl_env) live in tests/unit/conftest.py so
pytest discovers them automatically across all test files. This module holds
only pure constants and helper functions that can be freely imported.

Infrastructure values are grouped in WrapperTestConfig so callers can override
them in non-standard environments rather than inheriting hardcoded assumptions.
The private _CFG instance is the single source of truth for all defaults.
"""

import json
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Project-relative path — derivation only, not an OS-specific assumption
# ---------------------------------------------------------------------------
WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "cidx-curl.sh"


# ---------------------------------------------------------------------------
# Infrastructure configuration — grouped so non-standard envs can override
# ---------------------------------------------------------------------------


@dataclass
class WrapperTestConfig:
    """All infrastructure-specific values in one overridable place.

    Tests that run in environments with different DNS tools, timeout needs,
    or null-device paths can construct a custom instance rather than being
    forced to accept hardcoded values.
    """

    dns_probe_host: str = "dns.google"
    null_device: str = "/dev/null"
    fake_curl_shebang: str = "#!/bin/bash"
    curl_timeout: int = 15  # seconds for wrapper subprocess calls
    dns_timeout: int = 5  # seconds for DNS probe subprocess call
    curl_max_time_flag: str = "--max-time"
    curl_max_time_value: str = "1"  # passed to real curl to keep connections short


# Single private source of truth — all helper defaults derive from here
_CFG = WrapperTestConfig()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _write_config(config_path: Path, cidrs: Optional[List[str]] = None) -> None:
    """Write a config.json with the given operator CIDR list (None = empty list)."""
    config_path.write_text(
        json.dumps(
            {
                "claude_integration_config": {
                    "ra_curl_allowed_cidrs": cidrs if cidrs is not None else []
                }
            }
        )
    )


def _run(
    env,
    *args,
    cfg: Optional[WrapperTestConfig] = None,
) -> subprocess.CompletedProcess:
    """Invoke the wrapper with --max-time 1 -s -o /dev/null prepended.

    Any successful validation that reaches real curl exits quickly with curl's
    own connection-failure code (7), not a validation exit code (2/3/4).
    cfg defaults to the module-level _CFG singleton; callers may pass a custom
    instance without risk of cross-call mutation contamination.
    """
    resolved = cfg if cfg is not None else _CFG
    cmd = [
        str(WRAPPER),
        resolved.curl_max_time_flag,
        resolved.curl_max_time_value,
        "-s",
        "-o",
        resolved.null_device,
        *args,
    ]
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=resolved.curl_timeout
    )


def _run_no_curl(
    env,
    *args,
    cfg: Optional[WrapperTestConfig] = None,
) -> subprocess.CompletedProcess:
    """Invoke the wrapper without any extra curl flags.

    Use for tests that expect validation to reject before curl is reached,
    or when tests supply their own fake curl via the fake_curl_env fixture.
    cfg defaults to the module-level _CFG singleton; callers may pass a custom
    instance without risk of cross-call mutation contamination.
    """
    resolved = cfg if cfg is not None else _CFG
    cmd = [str(WRAPPER), *args]
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=resolved.curl_timeout
    )


def _has_dns(dns_cmd: Optional[List[str]] = None) -> bool:
    """Return True if the given DNS probe command succeeds.

    Defaults to ["getent", "hosts", _CFG.dns_probe_host]. Pass a custom
    dns_cmd to probe a different host or use a different DNS tool. Falsey
    values (including []) fall back to the default probe command.

    On OSError or SubprocessError (DNS or process-launch failure) emits
    warnings.warn(str(exc)) and returns False so callers (skipif markers)
    treat DNS as unavailable rather than crashing. This fallback is
    intentional and approved for test-environment probe use only.
    """
    dns_cmd = dns_cmd or ["getent", "hosts", _CFG.dns_probe_host]
    try:
        result = subprocess.run(
            dns_cmd,
            capture_output=True,
            text=True,
            timeout=_CFG.dns_timeout,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.warn(str(exc))
        return False
