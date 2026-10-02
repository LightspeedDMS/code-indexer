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

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.xray.rust_backend import (
    _XRAY_CLI_PATH_ENV_VAR,
    _XRAY_CLI_SOURCE_TREE_PATH,
    RustNativeBackend,
    get_graph_extractor_extensions,
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

    # The whole point of the override: because this path EXISTS, run_batch's
    # binary pre-check PASSES and the scan proceeds to invoke it. Asserted by
    # driving run_batch with the spawn mocked out -- a BinaryNotFound here
    # would mean the pre-check rejected an override it was told to honour.
    #
    # (Asserting `_xray_cli_path.exists()` instead cannot fail: this test
    # created the file, and the `== binary` assertion above already pins the
    # only thing that could make it false.)
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (
        json.dumps(
            {
                "findings": [],
                "files_parsed": 1,
                "files_errored": 0,
                "parse_scan_ms": 0,
                "compile_ms": 0,
                "cached": True,
                "error": None,
            }
        ),
        "",
    )
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
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

    errors = [e for _m, errs, _meta in results for e in errs]
    assert errors == [], f"an existing override must not be rejected: {errors!r}"
    mock_popen.assert_called_once()
    # ...and the process it tried to spawn is the overridden binary, argv[0].
    assert mock_popen.call_args[0][0][0] == str(binary)


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

    # PRECONDITION, not a claim under test: tmp_path is freshly created per
    # test and nothing here writes this path, so it cannot exist. Stated so a
    # reader does not mistake it for one of the three assertions below, which
    # are what this test actually pins.
    assert not missing.exists(), "precondition: tmp_path must not contain xray-cli"

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


# ---------------------------------------------------------------------------
# 4. get_graph_extractor_extensions() resolves the SAME way run_batch does
#
# Bug #1907's lookup has its own binary-path resolution, separate from
# RustNativeBackend's. It must call resolve_xray_cli_path() per invocation,
# NOT read the import-time _XRAY_CLI_DEFAULT snapshot: a packaged install
# exports CIDX_XRAY_CLI_PATH, and anything that reads the snapshot instead
# would report "xray-cli binary not found at <source-tree path nobody
# configured>" for a perfectly well-configured install -- and, worse, make
# _collect_graph_candidate_files unable to tell whether an excluded file's
# language would have contributed call edges, which is exactly the
# false-completeness failure that function exists to prevent.
#
# Swapping `resolve_xray_cli_path()` for `_XRAY_CLI_DEFAULT` in that function
# leaves the rest of the suite green; these two tests are what kills it.
# ---------------------------------------------------------------------------


def _write_extensions_stub(path: Path, payload: str) -> Path:
    """Write an executable stand-in for xray-cli that prints `payload`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n")
    path.chmod(0o755)
    return path


def test_graph_extractor_extensions_invokes_the_overridden_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override's binary is the one actually executed for the lookup.

    The stub answers with a registry no real xray-cli could return
    (`ZZ -> Zeta`), so a passing assertion can only mean THIS binary ran --
    reading the import-time snapshot would either fail with "not found" or,
    on a developer machine with a built source-tree binary, return the real
    java/kotlin registry. Either way the mapping below cannot match.
    """
    stub = _write_extensions_stub(
        tmp_path / "pkg" / "xray-cli",
        '{"extensions": [{"extension": "ZZ", "language": "Zeta"}]}',
    )
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(stub))

    mapping, error = get_graph_extractor_extensions()

    assert error is None, f"expected success from the overridden binary: {error!r}"
    # Lower-cased, leading dot stripped -- the documented key shape.
    assert mapping == {"zz": "Zeta"}


def test_graph_extractor_extensions_error_names_the_overridden_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken override is reported against ITSELF, not the source tree.

    Same anti-misdiagnosis contract resolve_xray_cli_path() pins for
    run_batch: the message an operator reads must name the path they
    configured. Reading the import-time snapshot would name
    _XRAY_CLI_SOURCE_TREE_PATH instead.
    """
    missing = tmp_path / "absent" / "xray-cli"
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(missing))

    mapping, error = get_graph_extractor_extensions()

    assert mapping is None
    assert error is not None
    assert str(missing) in error, (
        f"the error must name the configured override path: {error!r}"
    )
    assert str(_XRAY_CLI_SOURCE_TREE_PATH) not in error, (
        "a broken override must never be reported against the source-tree "
        f"path nobody configured: {error!r}"
    )
