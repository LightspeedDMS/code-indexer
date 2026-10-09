"""A daemon search that fails is reported and exits non-zero.

The daemon answers a failed semantic search with ``results: []`` plus an
``error`` field. Both daemon client paths (the fast entry path and the full
CLI delegation path) must print that error and return a non-zero exit code,
``--quiet`` included -- never render it as "no results" with exit 0.

The daemon RPC connection is the external boundary and is replaced by a small
fake; everything on the client side runs for real.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer import cli_daemon_delegation

FAILURE_MESSAGE = "embedding provider unavailable"
FAILED_RESPONSE: Dict[str, Any] = {
    "results": [],
    "timing": {"error": FAILURE_MESSAGE},
    "error": FAILURE_MESSAGE,
}


class _FakeRoot:
    def exposed_query(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return dict(FAILED_RESPONSE)


class _FakeConnection:
    def __init__(self) -> None:
        self.root = _FakeRoot()

    def close(self) -> None:
        pass


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({"codebase_dir": str(tmp_path)}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_daemon_delegation,
        "_connect_to_daemon",
        lambda socket_path, daemon_config: _FakeConnection(),
    )
    return config_path


@pytest.mark.parametrize("extra_args", [[], ["--quiet"]])
def test_fast_path_semantic_failure_prints_error_and_exits_nonzero(
    project: Path, capsys: pytest.CaptureFixture, extra_args: List[str]
) -> None:
    from code_indexer.cli_daemon_fast import execute_via_daemon

    exit_code = execute_via_daemon(
        ["cidx", "query", "find the thing", *extra_args], project
    )

    output = capsys.readouterr()
    assert exit_code != 0
    assert FAILURE_MESSAGE in output.out + output.err
    assert "No results" not in output.out


def test_slow_path_semantic_failure_exits_nonzero(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli_daemon_delegation, "_find_config_file", lambda: project)

    exit_code = cli_daemon_delegation._query_via_daemon(
        "find the thing", {"enabled": True}, fts=False, semantic=True, limit=5
    )

    output = capsys.readouterr()
    assert exit_code != 0
    assert FAILURE_MESSAGE in output.out + output.err
