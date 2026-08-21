"""Bug #1603 code review round 4 remediation: CLI callchain timeout handling.

Both the local-mode `cidx scip callchain` command and the remote-mode
`_display_callchain_results` helper must never report a query timeout as an
indistinguishable "no call chain found" success. This mirrors the fix
already applied to the MCP (handlers/scip.py), REST (routers/scip_queries.py),
and web UI (server/web/routes.py) callchain front doors.
"""

import re

import click.testing
import pytest
from unittest.mock import Mock, patch


def _make_mock_engine(from_symbol="main", to_symbol="UserService"):
    """Build a mock SCIPQueryEngine that resolves from_symbol/to_symbol.

    Also used by the CLI's chain-enrichment step, which re-looks-up every
    symbol name in a found chain's path (including intermediate hops not
    in defs_by_name) -- those must return [] rather than a bare Mock, or
    the CLI's Path(defs[0].file_path) call blows up with a TypeError.
    """
    from_def = Mock(symbol=from_symbol, file_path="src/main.py", line=1, column=0)
    to_def = Mock(symbol=to_symbol, file_path="src/service.py", line=2, column=0)
    defs_by_name = {from_symbol: [from_def], to_symbol: [to_def]}

    engine = Mock()
    engine.find_definition.side_effect = lambda sym, exact=False: defs_by_name.get(
        sym, []
    )
    return engine


def _invoke_local_callchain(tmp_path, monkeypatch, engine, args):
    """Set up a fake local SCIP repo and invoke `cidx scip callchain`."""
    scip_dir = tmp_path / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    (scip_dir / "index.scip.db").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "code_indexer.scip.query.primitives.SCIPQueryEngine",
            return_value=engine,
        ),
        patch("code_indexer.scip.status.StatusTracker") as mock_tracker_cls,
    ):
        mock_tracker = Mock()
        mock_status = Mock()
        mock_status.projects = {"test": "project"}
        mock_tracker.load.return_value = mock_status
        mock_tracker_cls.return_value = mock_tracker

        from code_indexer.cli_scip import scip_callchain

        runner = click.testing.CliRunner()
        return runner.invoke(scip_callchain, args)


def _timeout_trace_call_chain(from_symbol, to_symbol, max_depth=3, **kwargs):
    """Fake trace_call_chain that reports a timeout via the mutable list."""
    timeout_errors = kwargs.get("timeout_errors")
    if timeout_errors is not None:
        timeout_errors.append(
            "Query exceeded 30-second timeout. Try reducing depth or narrowing search."
        )
    return []


class TestLocalCallchainTimeoutHandling:
    """`cidx scip callchain` (local mode) must surface a timeout, not a
    false-negative 'No call chain found' success at exit code 0."""

    def test_local_callchain_reports_timeout_and_exits_nonzero(
        self, tmp_path, monkeypatch
    ):
        """A timeout during trace_call_chain must exit 1 with a clear
        timeout message -- never the false-negative "no chain found"."""
        engine = _make_mock_engine()
        engine.trace_call_chain.side_effect = _timeout_trace_call_chain

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService"]
        )

        assert result.exit_code == 1, (
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "No call chain found" not in result.output
        assert "timed out" in result.output.lower() or (
            "timeout" in result.output.lower()
        )

    def test_local_callchain_succeeds_normally_without_timeout(
        self, tmp_path, monkeypatch
    ):
        """Sanity: the happy path (no timeout) still exits 0 and finds chains."""
        from code_indexer.scip.query.backends import CallChain

        engine = _make_mock_engine()
        engine.trace_call_chain.return_value = [
            CallChain(path=["main", "run", "UserService"], length=2, has_cycle=False)
        ]

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService"]
        )

        assert result.exit_code == 0
        assert "Found 1 call chain" in result.output

    def test_trace_call_chain_called_with_timeout_errors_kwarg(
        self, tmp_path, monkeypatch
    ):
        """The command must pass a mutable timeout_errors list into
        trace_call_chain -- otherwise a timeout is silently lost."""
        engine = _make_mock_engine()
        engine.trace_call_chain.return_value = []

        _invoke_local_callchain(tmp_path, monkeypatch, engine, ["main", "UserService"])

        assert engine.trace_call_chain.called
        _, call_kwargs = engine.trace_call_chain.call_args
        assert "timeout_errors" in call_kwargs
        assert isinstance(call_kwargs["timeout_errors"], list)


class TestRemoteCallchainTimeoutHandling:
    """`_display_callchain_results` (remote mode) must exit non-zero when
    the server reports a per-repo error (including a timeout), not just
    print it in red and exit 0."""

    @pytest.mark.parametrize(
        "results",
        [
            {},
            {"good-repo": [{"path": [{"symbol": "main"}, {"symbol": "run"}]}]},
        ],
        ids=["no-chains", "partial-chains"],
    )
    def test_exits_nonzero_when_errors_present(self, results):
        """A per-repo error (e.g. a timeout) must exit 1 regardless of
        whether other repos returned partial chains."""
        from code_indexer.cli_scip import _display_callchain_results

        result = {
            "results": results,
            "errors": {"bad-repo": "Query timeout exceeded while tracing call chain"},
        }

        with pytest.raises(SystemExit) as exc_info:
            _display_callchain_results(result, "main", "run")
        assert exc_info.value.code == 1

    def test_exits_zero_on_success_no_errors(self):
        from code_indexer.cli_scip import _display_callchain_results

        result = {
            "results": {"good-repo": [{"path": [{"symbol": "main"}]}]},
            "errors": {},
        }

        with pytest.raises(SystemExit) as exc_info:
            _display_callchain_results(result, "main", "run")
        assert exc_info.value.code == 0

    def test_exits_zero_when_no_chains_and_no_errors(self):
        from code_indexer.cli_scip import _display_callchain_results

        result = {"results": {}, "errors": {}}

        with pytest.raises(SystemExit) as exc_info:
            _display_callchain_results(result, "main", "run")
        assert exc_info.value.code == 0


class TestCallchainLoopEarlyBreakOnTimeout:
    """Bug #1603 code review round 5 remediation (F2): the local CLI's
    N-from x M-to fuzzy-match loop must stop at the FIRST timeout instead
    of exhausting the full cross-product (up to ~100 pairs observed by the
    reviewer, each eligible for the full 30s default timeout) -- otherwise
    the round-4 timeout message can take up to ~50 minutes to ever fire,
    defeating its own purpose."""

    def _make_multi_def_engine(self):
        """2 from_defs x 2 to_defs = 4 possible pairs, so an early break
        after the first pair is provably distinguishable from exhausting
        the cross-product."""
        from_defs = [
            Mock(symbol="main#1", file_path="src/main.py", line=1, column=0),
            Mock(symbol="main#2", file_path="src/main2.py", line=1, column=0),
        ]
        to_defs = [
            Mock(symbol="UserService#1", file_path="src/service.py", line=2, column=0),
            Mock(symbol="UserService#2", file_path="src/service2.py", line=2, column=0),
        ]
        defs_by_name = {"main": from_defs, "UserService": to_defs}

        engine = Mock()
        engine.find_definition.side_effect = lambda sym, exact=False: defs_by_name.get(
            sym, []
        )
        return engine

    def test_first_pair_timeout_stops_scan_immediately(self, tmp_path, monkeypatch):
        """When the first (from_def, to_def) pair times out, none of the
        remaining 3 pairs may be attempted."""
        engine = self._make_multi_def_engine()

        def _timeout_once(from_symbol, to_symbol, max_depth=3, **kwargs):
            timeout_errors = kwargs.get("timeout_errors")
            if timeout_errors is not None:
                timeout_errors.append(
                    "Query exceeded 30-second timeout. Try reducing depth or "
                    "narrowing search."
                )
            return []

        engine.trace_call_chain.side_effect = _timeout_once

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService"]
        )

        assert result.exit_code == 1, (
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "timed out" in result.output.lower() or (
            "timeout" in result.output.lower()
        )
        assert engine.trace_call_chain.call_count == 1, (
            "expected the loop to break after the first timeout instead of "
            f"attempting all 4 pairs, got {engine.trace_call_chain.call_count} calls"
        )


class TestCallchainMaxDepthValidation:
    """Bug #1603 code review round 5 remediation (F1): local mode must
    reject an out-of-range --max-depth loudly, exactly like remote mode's
    SCIPAPIClient.callchain already does -- never silently clamp deep
    inside trace_call_chain_v2_batched with only a server-side-style log
    the CLI user never sees."""

    def test_local_max_depth_above_cap_rejected_not_silently_clamped(
        self, tmp_path, monkeypatch
    ):
        """--max-depth 5 (above the cap of 3) must exit 1 with a clear
        message and must NEVER reach trace_call_chain (which would
        silently clamp it back down to 3)."""
        engine = _make_mock_engine()
        engine.trace_call_chain.return_value = []

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService", "--max-depth", "5"]
        )

        assert result.exit_code == 1, (
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "--max-depth must be between 1 and 3" in result.output
        assert not engine.trace_call_chain.called

    def test_local_max_depth_below_minimum_rejected(self, tmp_path, monkeypatch):
        """--max-depth 0 must also be rejected loudly (symmetric with the
        upper-bound check)."""
        engine = _make_mock_engine()
        engine.trace_call_chain.return_value = []

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService", "--max-depth", "0"]
        )

        assert result.exit_code == 1, (
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "--max-depth must be between 1 and 3" in result.output
        assert not engine.trace_call_chain.called

    def test_local_max_depth_within_cap_still_succeeds(self, tmp_path, monkeypatch):
        """Sanity: a valid --max-depth (e.g. 2) is unaffected."""
        from code_indexer.scip.query.backends import CallChain

        engine = _make_mock_engine()
        engine.trace_call_chain.return_value = [
            CallChain(path=["main", "run", "UserService"], length=2, has_cycle=False)
        ]

        result = _invoke_local_callchain(
            tmp_path, monkeypatch, engine, ["main", "UserService", "--max-depth", "2"]
        )

        assert result.exit_code == 0
        assert "Found 1 call chain" in result.output


class TestCallchainDocstringConsistency:
    """Bug #1603 code review Priority 2: the docstring example must not
    contradict the --max-depth option's own advertised [1, 3] cap."""

    def test_all_documented_max_depth_examples_within_advertised_cap(self):
        from code_indexer.cli_scip import scip_callchain

        max_depth_option = [p for p in scip_callchain.params if p.name == "max_depth"][
            0
        ]
        # The option's own help text advertises "default 3, max 3".
        assert "max 3" in max_depth_option.help

        doc = scip_callchain.__doc__ or ""
        documented_depths = [int(n) for n in re.findall(r"--max-depth (\d+)", doc)]
        assert documented_depths, "expected at least one --max-depth example"
        for depth in documented_depths:
            assert 1 <= depth <= 3, (
                f"docstring example uses --max-depth {depth}, which is "
                "outside the [1, 3] cap advertised by the --max-depth "
                "option itself (Bug #1603)"
            )
