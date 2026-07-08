"""Tests for the shared subprocess_env helper module.

Verifies that build_cidx_subprocess_env absolutizes any relative PYTHONPATH
entries so that cidx subprocesses spawned with cwd=<clone_or_repo_path> do
not have a relative PYTHONPATH entry re-anchor to the child's cwd
(Bug #1325). This module lives in the shared `code_indexer.utils` package
(Story #1328) so both `server/` and `cli/`/`proxy/` code can import it
without a layering violation.
"""

import os

from code_indexer.utils.subprocess_env import build_cidx_subprocess_env


def test_relative_pythonpath_entry_absolutized(monkeypatch):
    """A single relative PYTHONPATH entry must be resolved to an absolute path."""
    monkeypatch.setenv("PYTHONPATH", "./src")
    result = build_cidx_subprocess_env()
    assert result["PYTHONPATH"] == os.path.abspath("./src")


def test_absolute_pythonpath_entry_unchanged(monkeypatch):
    """An already-absolute PYTHONPATH entry must pass through unchanged."""
    monkeypatch.setenv("PYTHONPATH", "/abs/x")
    result = build_cidx_subprocess_env()
    assert result["PYTHONPATH"] == "/abs/x"


def test_multi_entry_pythonpath_first_absolutized_second_unchanged_order_preserved(
    monkeypatch,
):
    """Only relative entries in a multi-entry PYTHONPATH are absolutized; order is preserved."""
    monkeypatch.setenv("PYTHONPATH", f"./src{os.pathsep}/abs/x")
    result = build_cidx_subprocess_env()
    expected = os.pathsep.join([os.path.abspath("./src"), "/abs/x"])
    assert result["PYTHONPATH"] == expected


def test_missing_pythonpath_returns_dict_without_key(monkeypatch):
    """When PYTHONPATH is absent, the returned dict has no PYTHONPATH key, but all other vars are copied."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("CIDX_TEST_MARKER_ABC", "marker_value")
    result = build_cidx_subprocess_env()
    assert "PYTHONPATH" not in result
    assert result["CIDX_TEST_MARKER_ABC"] == "marker_value"


def test_empty_pythonpath_unchanged(monkeypatch):
    """An empty-string PYTHONPATH must pass through unchanged, not crash."""
    monkeypatch.setenv("PYTHONPATH", "")
    result = build_cidx_subprocess_env()
    assert result["PYTHONPATH"] == ""


def test_explicit_base_env_used_and_not_mutated():
    """An explicit base_env dict is used instead of os.environ and is never mutated."""
    base_env = {"PYTHONPATH": "./src", "OTHER_VAR": "keep_me"}
    base_env_snapshot = dict(base_env)
    result = build_cidx_subprocess_env(base_env)
    assert result["PYTHONPATH"] == os.path.abspath("./src")
    assert result["OTHER_VAR"] == "keep_me"
    assert base_env == base_env_snapshot


def test_returned_dict_not_aliased_to_os_environ(monkeypatch):
    """Mutating the returned dict must never affect os.environ (no aliasing)."""
    monkeypatch.setenv("PYTHONPATH", "./src")
    snapshot = dict(os.environ)
    result = build_cidx_subprocess_env()
    assert result is not os.environ
    result["CIDX_MUTATION_SENTINEL_1325"] = "mutated"
    assert dict(os.environ) == snapshot
    assert "CIDX_MUTATION_SENTINEL_1325" not in os.environ
