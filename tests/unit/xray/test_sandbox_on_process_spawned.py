"""Unit tests for on_process_spawned callback in PythonEvaluatorSandbox.

Verifies that:
- run_batch() accepts an on_process_spawned callback (backward compat without it)
- run_batch() forwards the callback to _run_driver_batch()

These tests mock _run_driver_batch to avoid real subprocess spawning.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


class TestOnProcessSpawned:
    def test_run_batch_no_callback_by_default(self):
        """run_batch works without on_process_spawned (backward compatibility)."""
        sandbox = PythonEvaluatorSandbox()
        validation = sandbox.validate('return {"matches": [], "value": None}')
        assert validation.ok

        # Call with no callback — must not raise TypeError
        with patch(
            "code_indexer.xray.sandbox._run_driver_batch",
            return_value=[],
        ):
            result = sandbox.run_batch(
                evaluator_code='return {"matches": [], "value": None}',
                file_specs=[
                    {
                        "file_path": "f.py",
                        "source": "x=1",
                        "lang": "python",
                        "match_positions": [],
                    }
                ],
            )
        assert result == []

    def test_run_batch_passes_callback_to_driver(self):
        """run_batch forwards on_process_spawned to _run_driver_batch."""
        sandbox = PythonEvaluatorSandbox()
        callback = MagicMock()

        with patch(
            "code_indexer.xray.sandbox._run_driver_batch",
            return_value=[],
        ) as mock_driver:
            sandbox.run_batch(
                evaluator_code='return {"matches": [], "value": None}',
                file_specs=[
                    {
                        "file_path": "f.py",
                        "source": "x=1",
                        "lang": "python",
                        "match_positions": [],
                    }
                ],
                on_process_spawned=callback,
            )

        mock_driver.assert_called_once()
        call_kwargs = mock_driver.call_args
        # on_process_spawned must be forwarded — check keyword arg
        assert call_kwargs.kwargs.get("on_process_spawned") is callback

    def test_run_batch_callback_none_when_not_provided(self):
        """on_process_spawned defaults to None when not provided."""
        sandbox = PythonEvaluatorSandbox()

        with patch(
            "code_indexer.xray.sandbox._run_driver_batch",
            return_value=[],
        ) as mock_driver:
            sandbox.run_batch(
                evaluator_code='return {"matches": [], "value": None}',
                file_specs=[
                    {
                        "file_path": "f.py",
                        "source": "x=1",
                        "lang": "python",
                        "match_positions": [],
                    }
                ],
            )

        mock_driver.assert_called_once()
        call_kwargs = mock_driver.call_args
        assert call_kwargs.kwargs.get("on_process_spawned") is None
