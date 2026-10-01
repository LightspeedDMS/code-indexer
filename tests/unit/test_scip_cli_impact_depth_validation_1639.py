"""Bug #1639: CLI `scip impact --depth` validation.

Sibling commands `dependencies`/`dependents --depth` (Bug #1627) and
`callchain --max-depth` (Bug #1603) already reject an out-of-range value
loudly and immediately at the CLI layer, BEFORE branching into local/remote
mode or reaching the deep engine layer. `impact --depth` had no equivalent
client-side guard: an out-of-range value (including negative/zero, which
had NO lower-bound check anywhere) silently reached
`code_indexer.scip.query.composites.analyze_impact`, whose own
`depth = min(depth, MAX_TRAVERSAL_DEPTH)` line only clamped the upper
bound with no user-visible message.
"""

from unittest.mock import Mock, patch

import click.testing
import pytest

MIN_DEPTH = 1
MAX_DEPTH = 10


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


def _invoke_impact(depth):
    """Invoke the local-mode `scip impact` CLI command with a mocked
    analyze_impact composite and status tracker, returning (CliRunner
    result, mock analyze_impact)."""
    from code_indexer.cli_scip import scip_impact

    mock_result = Mock(total_affected=0, affected_symbols=[], affected_files=[])

    with (
        patch(
            "code_indexer.scip.query.composites.analyze_impact",
            return_value=mock_result,
        ) as mock_analyze,
        patch(
            "code_indexer.scip.status.StatusTracker",
            return_value=_mock_status_tracker(),
        ),
    ):
        runner = click.testing.CliRunner()
        result = runner.invoke(scip_impact, ["SomeSymbol", "--depth", str(depth)])

    return result, mock_analyze


class TestImpactDepthValidation:
    """`impact --depth` must reject an out-of-range value loudly at the CLI
    layer, exactly like `dependencies`/`dependents --depth` and
    `callchain --max-depth` already do."""

    @pytest.mark.parametrize("depth", [MIN_DEPTH - 1, MIN_DEPTH - 6, MAX_DEPTH + 1])
    def test_out_of_range_depth_rejected_before_analysis(
        self, mock_scip_environment, depth
    ):
        """An out-of-range --depth (including negative/zero, which
        previously had no lower-bound check at all) must exit 1 with a
        clear message and must NEVER reach analyze_impact (which would
        silently clamp the upper bound and process the lower bound
        unguarded)."""
        result, mock_analyze = _invoke_impact(depth)

        assert result.exit_code == 1, (
            f"depth={depth}, exit_code={result.exit_code}, output={result.output!r}"
        )
        assert f"--depth must be between {MIN_DEPTH} and {MAX_DEPTH}" in result.output
        assert not mock_analyze.called

    @pytest.mark.parametrize("depth", [MIN_DEPTH, MAX_DEPTH, 5])
    def test_in_range_depth_still_reaches_analysis(self, mock_scip_environment, depth):
        """Boundary values (1, 10) and a mid-range value (5) must all be
        accepted and passed through to analyze_impact unchanged."""
        result, mock_analyze = _invoke_impact(depth)

        assert result.exit_code == 0, (
            f"depth={depth}, exit_code={result.exit_code}, output={result.output!r}"
        )
        assert mock_analyze.called
        _, call_kwargs = mock_analyze.call_args
        assert call_kwargs["depth"] == depth
