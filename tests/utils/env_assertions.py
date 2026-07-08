"""Leak-safe membership/value assertions for os.environ-derived test dicts.

Issue #1327: `assert KEY not in env_dict` (and `assert KEY in env_dict`)
trigger pytest's assertion rewriting, which reprs the ENTIRE right-hand
container -- keys AND values -- into the failure output. When `env_dict` is
`os.environ`-derived (or a subprocess env built from a copy of it), every
live secret in the test-runner's environment (VoyageAI/Cohere/Anthropic API
keys, GitHub/GitLab PATs, etc.) gets dumped into test logs / terminal /
trace output on failure -- even when the assertion carries a custom message,
because the message is *additional* text, not a replacement for pytest's
introspection of the compared objects.

The functions below convert the membership/equality check into a plain
`bool`/scalar comparison BEFORE the `assert` statement runs, so pytest's
rewriter only ever has a `bool` (or, for `assert_env_value`, the two
already-known string values) to repr -- never the whole container.

Use these ONLY for os.environ-derived / subprocess-env dicts that may carry
secrets. For small hand-built dicts with no secret-shaped values, a plain
`assert key in some_dict` is fine and preferred for readability.
"""

from __future__ import annotations

from typing import Mapping


def assert_env_absent(env: Mapping[str, str], key: str, msg: str = "") -> None:
    """Assert `key` is NOT present in `env` without letting pytest repr `env`.

    Args:
        env: The (possibly secret-bearing) environment mapping under test.
        key: The environment variable name expected to be absent.
        msg: Optional message prefix/override for the failure text.
    """
    present = key in env
    assert not present, msg or f"{key} unexpectedly present in subprocess env"


def assert_env_present(env: Mapping[str, str], key: str, msg: str = "") -> None:
    """Assert `key` IS present in `env` without letting pytest repr `env`.

    Args:
        env: The (possibly secret-bearing) environment mapping under test.
        key: The environment variable name expected to be present.
        msg: Optional message prefix/override for the failure text.
    """
    absent = key not in env
    assert not absent, msg or f"{key} unexpectedly missing from subprocess env"


def assert_env_value(
    env: Mapping[str, str], key: str, expected: str, msg: str = ""
) -> None:
    """Assert `env[key] == expected` for a NON-secret value.

    Only safe to use when `expected` (and the actual stored value) is not
    itself a secret -- pytest's rewriter WILL repr both `actual` and
    `expected` on failure, since they are plain strings, not the container.
    If the value under test is secret, assert a redacted property instead
    (e.g. `actual.endswith(...)`, `os.path.isabs(actual)`).

    Args:
        env: The (possibly secret-bearing) environment mapping under test.
        key: The environment variable name whose value is checked.
        expected: The expected non-secret value.
        msg: Optional message prefix/override for the failure text.
    """
    actual = env.get(key)
    assert actual == expected, msg or f"{key} expected {expected!r}, got {actual!r}"
