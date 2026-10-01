"""Unit tests for the CIDX_XRAY_CLI_PATH override (xray-cli binary location).

Why this exists: every xray-cli path in ``rust_backend`` used to be derived
from ``_find_project_root()``, which looks for a ``rust/`` directory above
``src/code_indexer/``. A packaged install (pip into site-packages, which is
what the container image does) has no such directory, so the walk fell
through to its ``parent^4`` fallback and every scan targeted a path that
could not exist. The override lets whoever PACKAGED the install point cidx
at the prebuilt binary they shipped.

Contract pinned here (see ``resolve_xray_cli_path``'s docstring):
  1. ``$CIDX_XRAY_CLI_PATH`` wins when set to a non-blank value -- whether or
     not the file it names exists.
  2. Otherwise the source-tree build output,
     ``<project root>/rust/target/release/xray-cli``.
  3. No fallback from (1) to (2): a broken override must report ITSELF.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_indexer.xray.rust_backend import (
    _XRAY_CLI_PATH_ENV_VAR,
    _XRAY_CLI_SOURCE_TREE_PATH,
    RustNativeBackend,
    resolve_xray_cli_path,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Start every test from a known-unset override.

    Without this, a developer (or CI image) that legitimately exports
    CIDX_XRAY_CLI_PATH would flip the "unset" cases into the "set" branch and
    the suite would quietly stop testing what it claims to test.
    """
    monkeypatch.delenv(_XRAY_CLI_PATH_ENV_VAR, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Override set and the file exists
# ---------------------------------------------------------------------------


def test_override_pointing_at_existing_file_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CIDX_XRAY_CLI_PATH naming a real file is used verbatim."""
    binary = tmp_path / "bin" / "xray-cli"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(binary))

    assert resolve_xray_cli_path() == binary
    assert RustNativeBackend()._xray_cli_path == binary
    # The whole point of the override: this path EXISTS, so run_batch's
    # binary pre-check passes and the scan proceeds to invoke it.
    assert RustNativeBackend()._xray_cli_path.exists()


def test_override_is_read_at_construction_not_frozen_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporting the variable AFTER importing rust_backend still takes effect.

    rust_backend is imported at the top of this module, i.e. before the
    monkeypatch below -- the import-time snapshot cannot see it. A backend
    constructed now must, or an entrypoint that configures its environment
    after importing cidx would silently get the stale source-tree path.
    """
    first = RustNativeBackend()._xray_cli_path
    assert first == _XRAY_CLI_SOURCE_TREE_PATH

    binary = tmp_path / "late-xray-cli"
    binary.write_text("")
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(binary))

    assert RustNativeBackend()._xray_cli_path == binary


def test_override_expands_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``~``-prefixed override is expanded rather than taken literally."""
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, "~/xray-cli")

    resolved = resolve_xray_cli_path()
    assert "~" not in str(resolved)
    assert resolved == Path(os.path.expanduser("~")) / "xray-cli"


# ---------------------------------------------------------------------------
# 2. Override set but the file is missing
# ---------------------------------------------------------------------------


def test_override_pointing_at_missing_file_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken override resolves to ITSELF, never to the source-tree path.

    Falling back would hide an operator's typo behind a BinaryNotFound naming
    a path nobody configured -- the misdiagnosis this override exists to end.
    """
    missing = tmp_path / "does-not-exist" / "xray-cli"
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(missing))

    assert not missing.exists()
    assert resolve_xray_cli_path() == missing
    assert resolve_xray_cli_path() != _XRAY_CLI_SOURCE_TREE_PATH
    assert RustNativeBackend()._xray_cli_path == missing


def test_missing_override_surfaces_binary_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_batch reports BinaryNotFound when the override points nowhere.

    Only the error TYPE is asserted here; the shape of that error (one
    batch-level tuple with file_path=="") is a separate contract, pinned in
    tests/unit/xray/test_rust_backend.py and in the handler-level tests.
    """
    missing = tmp_path / "nope" / "xray-cli"
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(missing))

    results = RustNativeBackend().run_batch(
        evaluator_code=(
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"
        ),
        file_specs=[
            {
                "file_path": "a.py",
                "source": "x = 1\n",
                "lang": "python",
                "match_positions": [],
            }
        ],
    )

    all_errors = [e for _m, errs, _meta in results for e in errs]
    assert all_errors, "a missing binary must produce an error, not an empty result"
    assert all(e["error_type"] == "BinaryNotFound" for e in all_errors)


# ---------------------------------------------------------------------------
# 3. Override unset -- today's behaviour, unchanged
# ---------------------------------------------------------------------------


def test_unset_override_uses_source_tree_build_output() -> None:
    """With no override, resolution is the pre-existing source-tree path."""
    assert resolve_xray_cli_path() == _XRAY_CLI_SOURCE_TREE_PATH
    assert RustNativeBackend()._xray_cli_path == _XRAY_CLI_SOURCE_TREE_PATH
    assert _XRAY_CLI_SOURCE_TREE_PATH.name == "xray-cli"
    assert _XRAY_CLI_SOURCE_TREE_PATH.parent.parts[-3:] == (
        "rust",
        "target",
        "release",
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_override_is_treated_as_unset(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported-but-empty variable must not resolve to Path('') / cwd.

    ``Path("")`` is ``Path(".")``, which silently turns "operator cleared the
    variable" into "look for a binary named '.' in the working directory".
    """
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, blank)

    assert resolve_xray_cli_path() == _XRAY_CLI_SOURCE_TREE_PATH
    assert RustNativeBackend()._xray_cli_path == _XRAY_CLI_SOURCE_TREE_PATH
