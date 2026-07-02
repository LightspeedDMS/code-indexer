"""Tests for DualVectorCalculationManager (Story #487)."""

from unittest.mock import MagicMock

import pytest

from code_indexer.services.dual_vector_calculation_manager import (
    DualVectorCalculationManager,
)


class TestDualVCMCreation:
    def test_dual_vcm_holds_two_managers(self):
        primary = MagicMock()
        secondary = MagicMock()
        dual = DualVectorCalculationManager(primary, secondary)
        assert dual.primary is primary
        assert dual.secondary is secondary

    def test_embedding_provider_returns_primary(self):
        primary = MagicMock()
        secondary = MagicMock()
        primary.embedding_provider = MagicMock()
        dual = DualVectorCalculationManager(primary, secondary)
        assert dual.embedding_provider is primary.embedding_provider


class TestDualVCMConcurrentExecution:
    def test_calculate_batch_dual_calls_both(self):
        primary = MagicMock()
        secondary = MagicMock()
        primary.calculate_batch.return_value = [[0.1, 0.2]]
        secondary.calculate_batch.return_value = [[0.3, 0.4]]

        dual = DualVectorCalculationManager(primary, secondary)
        p_result, s_result = dual.calculate_batch_dual(["hello"])

        primary.calculate_batch.assert_called_once()
        secondary.calculate_batch.assert_called_once()
        assert p_result == [[0.1, 0.2]]
        assert s_result == [[0.3, 0.4]]

    def test_secondary_failure_returns_none(self):
        primary = MagicMock()
        secondary = MagicMock()
        primary.calculate_batch.return_value = [[0.1, 0.2]]
        secondary.calculate_batch.side_effect = RuntimeError("API down")

        dual = DualVectorCalculationManager(primary, secondary)
        p_result, s_result = dual.calculate_batch_dual(["hello"])

        assert p_result == [[0.1, 0.2]]
        assert s_result is None

    def test_primary_failure_raises(self):
        primary = MagicMock()
        secondary = MagicMock()
        primary.calculate_batch.side_effect = RuntimeError("Primary down")
        secondary.calculate_batch.return_value = [[0.3, 0.4]]

        dual = DualVectorCalculationManager(primary, secondary)
        with pytest.raises(RuntimeError, match="Primary down"):
            dual.calculate_batch_dual(["hello"])


class TestDualVCMCancellation:
    def test_request_cancellation_cancels_both(self):
        primary = MagicMock()
        secondary = MagicMock()
        dual = DualVectorCalculationManager(primary, secondary)

        dual.request_cancellation()

        primary.request_cancellation.assert_called_once()
        secondary.request_cancellation.assert_called_once()

    def test_shutdown_shuts_down_both(self):
        primary = MagicMock()
        secondary = MagicMock()
        dual = DualVectorCalculationManager(primary, secondary)

        dual.shutdown()

        primary.shutdown.assert_called_once()
        secondary.shutdown.assert_called_once()
