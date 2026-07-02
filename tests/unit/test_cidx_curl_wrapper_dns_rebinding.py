"""DNS rebinding mitigation tests for cidx-curl.sh (Story #929 Item #2a).

The wrapper injects --resolve HOST:PORT:VALIDATED_IP into the curl invocation
after validation. This pins curl to the exact IP that was validated, closing
the window between validation and connection where DNS could return a different
address. Tests here intercept the exec'd curl with a fake binary that prints
its arguments, then assert the --resolve pin is present and correct.
"""

import subprocess
from pathlib import Path

from tests.unit.test_cidx_curl_wrapper_helpers import (
    WRAPPER,
    _CFG,
    _write_config,
)


def _invoke_wrapper(env: dict, config_path: Path, *args) -> subprocess.CompletedProcess:
    """Write empty-CIDR config and run the wrapper with the given args.

    Callers unpack `env, config_path = fake_curl_env` before calling this,
    keeping the fixture unpacking at the test boundary and the subprocess
    mechanics in one place.
    """
    _write_config(config_path, [])
    return subprocess.run(
        [str(WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=_CFG.curl_timeout,
    )


class TestDnsRebindingMitigation:
    """--resolve HOST:PORT:IP must be injected after successful validation."""

    def test_validated_ip_pinned_via_resolve(self, fake_curl_env):
        """Validated IPv4 loopback must produce --resolve 127.0.0.1:8080:127.0.0.1."""
        env, config_path = fake_curl_env
        result = _invoke_wrapper(env, config_path, "http://127.0.0.1:8080/health")
        assert result.returncode == 0, (
            f"Successful validation must reach fake curl: stderr={result.stderr}"
        )
        assert "ARG=--resolve" in result.stdout, (
            f"--resolve must be injected. stdout={result.stdout}"
        )
        assert "ARG=127.0.0.1:8080:127.0.0.1" in result.stdout, (
            f"--resolve pin format must be HOST:PORT:IP. stdout={result.stdout}"
        )

    def test_ipv6_resolve_pin_uses_brackets(self, fake_curl_env):
        """IPv6 validated address must appear bracketed in --resolve: [::1]."""
        env, config_path = fake_curl_env
        result = _invoke_wrapper(env, config_path, "http://[::1]:8080/x")
        assert result.returncode == 0, (
            f"IPv6 loopback must reach fake curl: stderr={result.stderr}"
        )
        assert "[::1]" in result.stdout, (
            f"IPv6 --resolve pin must use brackets. stdout={result.stdout}"
        )

    def test_original_args_passed_through(self, fake_curl_env):
        """All original curl args must be forwarded unchanged after --resolve injection."""
        env, config_path = fake_curl_env
        result = _invoke_wrapper(
            env,
            config_path,
            "-H",
            "X-Custom: foo",
            "--max-time",
            "5",
            "-s",
            "-o",
            "/dev/null",
            "http://127.0.0.1:8080/health",
        )
        assert result.returncode == 0, f"Must reach fake curl: stderr={result.stderr}"
        for expected in (
            "-H",
            "X-Custom: foo",
            "--max-time",
            "5",
            "-s",
            "-o",
            "/dev/null",
            "http://127.0.0.1:8080/health",
        ):
            assert f"ARG={expected}" in result.stdout, (
                f"Original arg {expected!r} missing: {result.stdout}"
            )
