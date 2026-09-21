"""Tests for `get_graph_extractor_extensions` — Bug #1907.

Scoping `include_patterns` to one extractable language on a mixed-language
repository made `analyze_graph` report `fact_graph_complete: true` with
every degradation counter at zero, while the graph was missing every call
site in the excluded language. The excluded files never became candidates,
so no Rust-side counter could ever see them.

`get_graph_extractor_extensions` is the seam that fixes this: it asks
`xray-cli --print-graph-extractor-extensions` for the REAL extension set
with a graph extractor (the single source of truth `graph::extract::
graph_extractor_extensions` owns on the Rust side), so Python's
candidate-collection walk can classify an about-to-be-excluded file BEFORE
it disappears from every counter forever.

These are genuine component tests against the REAL compiled xray-cli
release binary (skipped if not built) — no subprocess mocking, mirroring
the existing `_require_xray_cli_binary()` pattern in
test_rust_backend_graph_mode.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.xray.rust_backend import (
    _XRAY_CLI_DEFAULT,
    get_graph_extractor_extensions,
)


def _require_xray_cli_binary() -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def test_get_graph_extractor_extensions_reports_java_and_kotlin() -> None:
    """The real registry today backs Java and both Kotlin extensions
    (`.kt`/`.kts`) — this must come from the REAL Rust registry, never a
    Python-side hardcoded duplicate that could silently drift the moment a
    new language's extractor lands (option 1 the bug report explicitly
    rejected).
    """
    _require_xray_cli_binary()

    mapping, error = get_graph_extractor_extensions()

    assert error is None, f"expected success, got error={error!r}"
    assert mapping is not None
    assert mapping["java"] == "Java"
    assert mapping["kt"] == "Kotlin"
    assert mapping["kts"] == "Kotlin"
    # A language with no extractor (e.g. python) must NEVER appear here --
    # its presence would make an excluded .py file wrongly downgrade
    # fact_graph_complete, when in fact including it would have contributed
    # nothing (no extractor reads it either way).
    assert "py" not in mapping


def test_get_graph_extractor_extensions_missing_binary_returns_structured_error(
    tmp_path: Path,
) -> None:
    """A missing xray-cli binary must produce `(None, message)`, never an
    unhandled `FileNotFoundError` propagating into the candidate-collection
    walk (Bug #1612's rule, mirrored here for the new lookup).
    """
    missing_path = tmp_path / "does-not-exist" / "xray-cli"

    mapping, error = get_graph_extractor_extensions(xray_cli_path=missing_path)

    assert mapping is None
    assert error is not None
    assert "not found" in error.lower()


def test_get_graph_extractor_extensions_malformed_binary_returns_structured_error(
    tmp_path: Path,
) -> None:
    """A binary that produces non-JSON garbage (simulating a corrupted or
    incompatible xray-cli build) must fail LOUDLY via `(None, message)` --
    NEVER silently return an empty mapping. An empty mapping would make
    EVERY excluded file read as "no extractor here", exactly the false
    "narrowing is safe" signal Bug #1907 exists to eliminate (Rule 2,
    anti-fallback: no silent degradation to a falsely-reassuring value).
    """
    fake_cli = tmp_path / "fake-xray-cli.sh"
    fake_cli.write_text("#!/bin/sh\necho 'not json at all'\nexit 0\n")
    fake_cli.chmod(0o755)

    mapping, error = get_graph_extractor_extensions(xray_cli_path=fake_cli)

    assert mapping is None
    assert error is not None


def test_get_graph_extractor_extensions_nonzero_exit_returns_structured_error(
    tmp_path: Path,
) -> None:
    """A subcommand that exits non-zero must be treated as a real failure,
    never silently swallowed into an empty/default mapping.
    """
    fake_cli = tmp_path / "fake-xray-cli.sh"
    fake_cli.write_text("#!/bin/sh\necho 'boom' 1>&2\nexit 1\n")
    fake_cli.chmod(0o755)

    mapping, error = get_graph_extractor_extensions(xray_cli_path=fake_cli)

    assert mapping is None
    assert error is not None
