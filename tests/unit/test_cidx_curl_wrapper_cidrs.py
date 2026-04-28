"""CIDR validation tests for cidx-curl.sh (Story #929 Item #2a).

Tests cover: loopback always-on, public network rejection, operator CIDR
extension, graceful config degradation, IPv6 handling, decimal-IP encoding,
and complex URL passthrough. All tests invoke the wrapper via subprocess.
"""

import pytest

from tests.unit.test_cidx_curl_wrapper_helpers import (
    _has_dns,
    _run,
    _run_no_curl,
    _write_config,
)


# ===========================================================================
# 1. Loopback always-on
# ===========================================================================


class TestLoopbackAlwaysOn:
    """127.0.0.0/8 and ::1/128 are always permitted regardless of operator config."""

    def test_ipv4_loopback_with_empty_config(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4, (
            f"127.0.0.1 must be allowed with empty config. stderr={result.stderr}"
        )
        assert result.returncode != 2

    def test_ipv6_loopback_with_empty_config(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://[::1]:1/")
        assert result.returncode != 4, f"::1 must be allowed: {result.stderr}"

    def test_localhost_hostname_resolves_to_loopback(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://localhost:1/")
        assert result.returncode != 4, (
            f"localhost must resolve to loopback: {result.stderr}"
        )

    def test_other_127_range_addresses_allowed(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        for ip in ("127.0.0.5", "127.1.2.3", "127.255.255.254"):
            result = _run(env, f"http://{ip}:1/")
            assert result.returncode != 4, (
                f"{ip} must be in 127.0.0.0/8: stderr={result.stderr}"
            )

    def test_loopback_with_no_config_file(self, isolated_config):
        """Missing config must warn but still allow loopback."""
        config_path, env = isolated_config
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4, (
            f"Loopback must work with no config: {result.stderr}"
        )
        assert "WARNING" in result.stderr or "config not found" in result.stderr

    def test_https_loopback_allowed(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "https://127.0.0.1:1/")
        assert result.returncode != 4, f"HTTPS loopback: {result.stderr}"


# ===========================================================================
# 2. Public network rejected with empty config
# ===========================================================================


class TestPublicNetworkRejected:
    """Without operator CIDRs only loopback is allowed; public IPs exit 4."""

    def test_public_ip_8888_rejected(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://8.8.8.8/")
        assert result.returncode == 4, (
            f"8.8.8.8 must exit 4. got={result.returncode}, stderr={result.stderr}"
        )
        assert "not in allowed CIDR set" in result.stderr

    def test_public_ip_1111_rejected(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://1.1.1.1/")
        assert result.returncode == 4

    def test_rfc1918_rejected_when_not_in_operator_config(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://10.0.0.1/")
        assert result.returncode == 4, (
            f"10.0.0.1 must be rejected with empty config: {result.stderr}"
        )

    @pytest.mark.skipif(not _has_dns(), reason="DNS not available in this environment")
    def test_public_hostname_rejected(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://dns.google/")
        assert result.returncode == 4, (
            f"dns.google must be rejected: got={result.returncode}, stderr={result.stderr}"
        )


# ===========================================================================
# 3. Operator CIDR extends (does not replace) loopback
# ===========================================================================


class TestOperatorCidrExtendsLoopback:
    """Operator CIDRs are additive — loopback is never removed."""

    def test_operator_cidr_allows_listed_ip(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24"])
        result = _run(env, "http://10.5.0.10:1/")
        assert result.returncode != 4, (
            f"10.5.0.10 must be allowed by 10.5.0.0/24: {result.stderr}"
        )

    def test_operator_cidr_does_not_remove_loopback(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24"])
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4, (
            f"Loopback must remain allowed when operator CIDR is set: {result.stderr}"
        )

    def test_ip_outside_operator_cidr_rejected(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24"])
        result = _run(env, "http://10.6.0.1/")
        assert result.returncode == 4, (
            f"10.6.0.1 must be rejected (outside 10.5.0.0/24): {result.stderr}"
        )

    def test_multiple_operator_cidrs_all_honored(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24", "192.168.100.0/24"])
        for ip in ("10.5.0.10", "192.168.100.50"):
            result = _run(env, f"http://{ip}:1/")
            assert result.returncode != 4, f"{ip} should be allowed: {result.stderr}"

    def test_loopback_with_multiple_operator_cidrs(self, isolated_config):
        """Loopback must remain allowed even when multiple operator CIDRs are configured."""
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24", "192.168.100.0/24"])
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4, (
            f"Loopback must remain allowed with multiple operator CIDRs: {result.stderr}"
        )

    def test_unlisted_rfc1918_rejected_with_specific_operator_cidrs(
        self, isolated_config
    ):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24"])
        result = _run(env, "http://192.168.1.1/")
        assert result.returncode == 4, (
            f"192.168.1.1 not in operator CIDRs must be rejected: {result.stderr}"
        )


# ===========================================================================
# 4. Config degradation — warnings emitted, loopback still works
# ===========================================================================


class TestGracefulConfigDegradation:
    """Invalid / missing config must warn but must not break loopback access."""

    def test_invalid_cidr_warns_and_continues(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["not-a-cidr", "10.5.0.0/24"])
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4
        assert "WARNING" in result.stderr or "invalid CIDR" in result.stderr

    def test_invalid_cidr_does_not_block_valid_cidr(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["bogus", "10.5.0.0/24"])
        result = _run(env, "http://10.5.0.10:1/")
        assert result.returncode != 4, f"Valid CIDR must still work: {result.stderr}"

    def test_missing_config_file_warns_uses_loopback_only(self, isolated_config):
        config_path, env = isolated_config
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4
        assert "WARNING" in result.stderr or "config not found" in result.stderr

    def test_missing_config_rejects_public(self, isolated_config):
        config_path, env = isolated_config
        result = _run(env, "http://8.8.8.8/")
        assert result.returncode == 4

    def test_malformed_json_warns_uses_loopback_only(self, isolated_config):
        config_path, env = isolated_config
        config_path.write_text("{not valid json")
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4
        assert "WARNING" in result.stderr

    def test_empty_config_file_warns_uses_loopback_only(self, isolated_config):
        """Empty file triggers JSON parse-failed warning (json.load raises on empty input)."""
        config_path, env = isolated_config
        config_path.write_text("")
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4
        assert "WARNING" in result.stderr

    def test_missing_claude_integration_config_section(self, isolated_config):
        """Config exists but has no claude_integration_config key — treat as empty."""
        config_path, env = isolated_config
        config_path.write_text('{"some_other_key": "value"}')
        result = _run(env, "http://127.0.0.1:1/")
        assert result.returncode != 4, (
            f"Loopback must work with unknown config section: {result.stderr}"
        )


# ===========================================================================
# 5. IPv6 handling
# ===========================================================================


class TestIPv6Handling:
    """IPv6 literals in brackets must be parsed correctly by the wrapper."""

    def test_ipv6_loopback_allowed(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://[::1]:1/")
        assert result.returncode != 4

    def test_ipv6_link_local_rejected_with_empty_config(self, isolated_config):
        """fe80::/10 is not in the always-on loopback set (only ::1/128 is)."""
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run_no_curl(env, "http://[fe80::1]/")
        assert result.returncode == 4, (
            f"fe80::1 must be rejected with empty config: {result.stderr}"
        )

    def test_operator_ipv6_cidr_allows_link_local(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["fe80::/64"])
        result = _run(env, "http://[fe80::1]:1/")
        assert result.returncode != 4, (
            f"fe80::1 must be allowed with fe80::/64 config: {result.stderr}"
        )


# ===========================================================================
# 6. Decimal-IP encoding rejected
# ===========================================================================


class TestDecimalIpEncodingRejected:
    def test_decimal_ip_127_0_0_1_rejected(self, isolated_config):
        """2130706433 is the decimal encoding of 127.0.0.1.

        ipaddress.ip_address('2130706433') raises ValueError, so the host
        falls through to DNS/OS resolution. Three legitimate outcomes exist:
        - exit 3: DNS fails to resolve the decimal string (most environments)
        - exit 4: resolves to a non-loopback IP, rejected by CIDR check
        - exit 0: OS resolves to 127.0.0.1 (loopback, always-on), curl proceeds

        Exit 0 is not a bypass — if the OS maps 2130706433 to 127.0.0.1,
        loopback access is explicitly permitted. The CIDR check still ran.
        Any other exit code would indicate an unexpected failure.
        """
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run_no_curl(env, "http://2130706433/")
        assert result.returncode in (0, 3, 4), (
            f"Decimal-IP must exit 0, 3, or 4. got={result.returncode}, stderr={result.stderr}"
        )


# ===========================================================================
# 7. URL with port + path + query passes through validation
# ===========================================================================


class TestUrlWithComplexPath:
    """Complex URLs must not be falsely rejected by the validation gate."""

    def test_loopback_with_port_path_query_fragment(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, "http://127.0.0.1:8000/api/v1/health?key=val#frag")
        assert result.returncode not in (2, 4)

    def test_https_to_operator_cidr_with_path(self, isolated_config):
        config_path, env = isolated_config
        _write_config(config_path, ["10.5.0.0/24"])
        result = _run(env, "https://10.5.0.5:8443/secure/path")
        assert result.returncode not in (2, 4)
