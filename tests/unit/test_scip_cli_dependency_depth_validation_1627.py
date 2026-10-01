"""Bug #1627: CLI `scip dependencies`/`scip dependents --depth` validation.

Sibling command `cidx scip callchain --max-depth` already rejects an
out-of-range value loudly and immediately at the CLI layer (see Bug #1603
remediation in `test_scip_cli_callchain_timeout_1603.py`,
`TestCallchainMaxDepthValidation`). `dependencies`/`dependents --depth` had
no equivalent client-side guard, so an out-of-range value silently reached
the deep engine layer instead of failing fast with a clear CLI message --
a UX consistency gap (this session's third and final layer of the sweep,
after Bug #1625's Web UI ceiling and Bug #1626's SCIPMultiService guard,
both using MIN_SCIP_DEPENDENCY_DEPTH=1 / MAX_SCIP_DEPENDENCY_DEPTH=10 from
`server/services/constants.py`).
"""

from unittest.mock import Mock, patch

import click.testing
import pytest

MIN_DEPTH = 1
MAX_DEPTH = 10

_COMMAND_CASES = [
    ("dependencies", "get_dependencies", "UserService"),
    ("dependents", "get_dependents", "Logger"),
]


@pytest.fixture
def mock_scip_environment(tmp_path, monkeypatch):
    """Setup mock SCIP environment with necessary files and mocks."""
    scip_dir = tmp_path / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    (scip_dir / "test.scip.db").touch()
    monkeypatch.chdir(tmp_path)


def _mock_status_tracker():
    mock_tracker = Mock()
    mock_status = Mock()
    mock_status.projects = {"test": "project"}
    mock_tracker.load.return_value = mock_status
    return mock_tracker


def _get_command(command_name):
    from code_indexer.cli_scip import scip_dependencies, scip_dependents

    return {"dependencies": scip_dependencies, "dependents": scip_dependents}[
        command_name
    ]


def _invoke(command_name, engine_method, symbol, depth):
    """Invoke a local-mode dependencies/dependents CLI command with a mocked
    engine and status tracker, returning (CliRunner result, mock engine)."""
    mock_engine = Mock()
    setattr(mock_engine, engine_method, Mock(return_value=[]))

    with (
        patch("code_indexer.scip.query.SCIPQueryEngine", return_value=mock_engine),
        patch(
            "code_indexer.scip.status.StatusTracker",
            return_value=_mock_status_tracker(),
        ),
    ):
        runner = click.testing.CliRunner()
        result = runner.invoke(
            _get_command(command_name), [symbol, "--depth", str(depth)]
        )

    return result, mock_engine


class TestDependencyDepthValidation:
    """Both `dependencies --depth` and `dependents --depth` must reject an
    out-of-range value loudly at the CLI layer, exactly like
    `callchain --max-depth` already does."""

    @pytest.mark.parametrize("command_name,engine_method,symbol", _COMMAND_CASES)
    @pytest.mark.parametrize("depth", [MIN_DEPTH - 1, MAX_DEPTH + 1])
    def test_out_of_range_depth_rejected_before_engine_call(
        self, mock_scip_environment, command_name, engine_method, symbol, depth
    ):
        """An out-of-range --depth must exit 1 with a clear message and
        must NEVER reach the engine (which would silently accept it)."""
        result, mock_engine = _invoke(command_name, engine_method, symbol, depth)

        assert result.exit_code == 1, (
            f"command={command_name}, depth={depth}, "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert f"--depth must be between {MIN_DEPTH} and {MAX_DEPTH}" in result.output
        assert not getattr(mock_engine, engine_method).called

    @pytest.mark.parametrize("command_name,engine_method,symbol", _COMMAND_CASES)
    @pytest.mark.parametrize("depth", [MIN_DEPTH, MAX_DEPTH, 5])
    def test_in_range_depth_still_reaches_engine(
        self, mock_scip_environment, command_name, engine_method, symbol, depth
    ):
        """Boundary values (1, 10) and a mid-range value (5) must all be
        accepted and passed through to the engine unchanged."""
        result, mock_engine = _invoke(command_name, engine_method, symbol, depth)

        assert result.exit_code == 0, (
            f"command={command_name}, depth={depth}, "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        engine_call = getattr(mock_engine, engine_method)
        assert engine_call.called
        _, call_kwargs = engine_call.call_args
        assert call_kwargs["depth"] == depth
