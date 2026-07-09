"""Regression guard for Issue #1327: pytest assertion-rewriting secret leakage.

`assert KEY not in env_dict` (and `assert KEY in env_dict`) trigger pytest's
assertion rewriting, which reprs the ENTIRE right-hand container -- keys AND
values -- into the failure output. When `env_dict` is `os.environ`-derived
(or a subprocess env built from it), every live secret (API keys, tokens)
gets dumped into test logs / terminal / trace output on failure, even when
the assertion carries a custom message (the message is *additional*, not a
replacement -- pytest still reprs the compared objects).

`tests/utils/env_assertions.py` provides membership helpers that convert the
dict-membership check into a boolean comparison BEFORE the assert, so pytest
only ever reprs a bool -- never the container.

These tests prove the whole class of leak is closed: sentinel "secret"
values placed in dicts fed to the helpers must never appear in the raised
AssertionError text.
"""

from __future__ import annotations

import pytest

from tests.utils.env_assertions import (
    assert_env_absent,
    assert_env_present,
    assert_env_value,
)

_SENTINEL_SECRET = "sk-SENTINEL-super-secret-12345"
_OTHER_SENTINEL_SECRET = "ghp_SENTINEL-other-secret-67890"


class TestAssertEnvAbsent:
    """Behavior and leak-safety of assert_env_absent."""

    def test_assert_env_absent_passes_when_key_missing(self):
        """No AssertionError when the key is not present in the env dict."""
        env = {"HOME": "/home/user", "PATH": "/usr/bin"}

        assert_env_absent(env, "CLAUDECODE")

    def test_assert_env_absent_raises_when_key_present(self):
        """AssertionError raised when the key IS present in the env dict."""
        env = {"CLAUDECODE": "1"}

        with pytest.raises(AssertionError):
            assert_env_absent(env, "CLAUDECODE")

    def test_assert_env_absent_failure_does_not_leak_secret_values(self):
        """REGRESSION GUARD (Issue #1327): the raised AssertionError text must
        NEVER contain the secret VALUES from the env dict -- only the key
        name / a bool. This is the exact failure mode proven in the bug
        report: `assert 'K' not in {'FAKE_API_KEY': 'sk-...'}` leaks the
        value via pytest's assertion rewriting.
        """
        env = {
            "FAKE_API_KEY": _SENTINEL_SECRET,
            "OTHER_TOKEN": _OTHER_SENTINEL_SECRET,
        }

        with pytest.raises(AssertionError) as exc_info:
            assert_env_absent(env, "FAKE_API_KEY")

        failure_text = str(exc_info.value)
        assert _SENTINEL_SECRET not in failure_text, (
            "assert_env_absent leaked the secret value it was checking for "
            "presence of -- the whole point of this helper is to prevent "
            "that leak"
        )
        assert _OTHER_SENTINEL_SECRET not in failure_text, (
            "assert_env_absent leaked an UNRELATED secret value from the "
            "same env dict -- proves the whole dict was NOT reprd"
        )

    def test_assert_env_absent_custom_message_used(self):
        """A custom message is used verbatim when provided."""
        env = {"CLAUDECODE": "1"}

        with pytest.raises(AssertionError, match="custom failure text"):
            assert_env_absent(env, "CLAUDECODE", msg="custom failure text")


class TestAssertEnvPresent:
    """Behavior and leak-safety of assert_env_present."""

    def test_assert_env_present_passes_when_key_present(self):
        """No AssertionError when the key IS present in the env dict."""
        env = {"CIDX_META_BASE": "/opt/cidx-meta"}

        assert_env_present(env, "CIDX_META_BASE")

    def test_assert_env_present_raises_when_key_missing(self):
        """AssertionError raised when the key is NOT present in the env dict."""
        env = {"HOME": "/home/user"}

        with pytest.raises(AssertionError):
            assert_env_present(env, "CIDX_META_BASE")

    def test_assert_env_present_failure_does_not_leak_secret_values(self):
        """REGRESSION GUARD (Issue #1327): failure text must never contain
        secret values from OTHER keys in the env dict being checked.
        """
        env = {
            "OTHER_TOKEN": _OTHER_SENTINEL_SECRET,
        }

        with pytest.raises(AssertionError) as exc_info:
            assert_env_present(env, "CIDX_META_BASE")

        failure_text = str(exc_info.value)
        assert _OTHER_SENTINEL_SECRET not in failure_text, (
            "assert_env_present leaked a secret value from the env dict -- "
            "proves the whole dict was NOT reprd"
        )

    def test_assert_env_present_custom_message_used(self):
        """A custom message is used verbatim when provided."""
        env: dict = {}

        with pytest.raises(AssertionError, match="custom failure text"):
            assert_env_present(env, "MISSING", msg="custom failure text")


class TestAssertEnvValue:
    """Behavior and leak-safety of assert_env_value for non-secret values."""

    def test_assert_env_value_passes_when_matches(self):
        """No AssertionError when env[key] == expected."""
        env = {"PYTHONPATH": "/abs/path/src"}

        assert_env_value(env, "PYTHONPATH", "/abs/path/src")

    def test_assert_env_value_raises_when_mismatched(self):
        """AssertionError raised when env[key] != expected."""
        env = {"PYTHONPATH": "/wrong/path"}

        with pytest.raises(AssertionError):
            assert_env_value(env, "PYTHONPATH", "/abs/path/src")

    def test_assert_env_value_failure_does_not_leak_unrelated_secrets(self):
        """REGRESSION GUARD (Issue #1327): failure text must never contain
        secret values from OTHER keys in the env dict -- only the target
        key's own (non-secret) actual/expected values are reprd.
        """
        env = {
            "PYTHONPATH": "/wrong/path",
            "OTHER_TOKEN": _OTHER_SENTINEL_SECRET,
        }

        with pytest.raises(AssertionError) as exc_info:
            assert_env_value(env, "PYTHONPATH", "/abs/path/src")

        failure_text = str(exc_info.value)
        assert _OTHER_SENTINEL_SECRET not in failure_text, (
            "assert_env_value leaked an unrelated secret value from the "
            "env dict -- proves the whole dict was NOT reprd"
        )
