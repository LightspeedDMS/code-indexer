"""
Dual Vector Calculation Manager (Story #487).

Wrapper that dispatches embedding work to two VectorCalculationManagers
concurrently (one per provider). Implements the same interface so
HighThroughputProcessor sees a single manager.

Architecture: Option B from the epic spec -- single-manager interface
with internal fan-out. HighThroughputProcessor is unchanged.
"""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from code_indexer.services.vector_calculation_manager import VectorCalculationManager

logger = logging.getLogger(__name__)


class DualVectorCalculationManager:
    """Wrapper dispatching to two VCMs for dual-provider embedding."""

    def __init__(
        self,
        primary_vcm: VectorCalculationManager,
        secondary_vcm: VectorCalculationManager,
        primary_name: str = "primary",
        secondary_name: str = "secondary",
    ):
        self.primary = primary_vcm
        self.secondary = secondary_vcm
        self.primary_name = primary_name
        self.secondary_name = secondary_name
        self._cancelled = False

    @property
    def embedding_provider(self):
        """Return primary provider (for collection naming in single-provider paths)."""
        return self.primary.embedding_provider

    @property
    def thread_count(self) -> int:
        """Return combined thread count."""
        return int(self.primary.thread_count) + int(self.secondary.thread_count)

    def get_resolved_thread_count(self, config=None) -> int:
        """Return primary thread count (compat with single-VCM callers)."""
        return int(self.primary.thread_count)

    def calculate_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        """Calculate embeddings using primary provider only.

        For single-result callers that expect one embedding set.
        Dual results are accessed via calculate_batch_dual().
        """
        result: List[List[float]] = self.primary.calculate_batch(texts, **kwargs)
        return result

    def calculate_batch_dual(
        self, texts: List[str], **kwargs
    ) -> Tuple[List[List[float]], Optional[List[List[float]]]]:
        """Calculate embeddings using both providers concurrently.

        Returns (primary_embeddings, secondary_embeddings).
        If secondary fails, returns (primary_embeddings, None).
        """
        primary_result: list = [None]
        secondary_result: list = [None]
        primary_error: list = [None]
        secondary_error: list = [None]

        def run_primary():
            try:
                primary_result[0] = self.primary.calculate_batch(texts, **kwargs)
            except Exception as e:
                primary_error[0] = e
                logger.error("%s embedding failed: %s", self.primary_name, e)

        def run_secondary():
            try:
                secondary_result[0] = self.secondary.calculate_batch(texts, **kwargs)
            except Exception as e:
                secondary_error[0] = e
                logger.warning(
                    "%s embedding failed (non-fatal): %s", self.secondary_name, e
                )

        t1 = threading.Thread(target=run_primary, name=f"dual-{self.primary_name}")
        t2 = threading.Thread(target=run_secondary, name=f"dual-{self.secondary_name}")
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if primary_error[0] is not None:
            raise primary_error[0]

        return primary_result[0], secondary_result[0]

    def request_cancellation(self) -> None:
        """Cancel both managers."""
        self._cancelled = True
        self.primary.request_cancellation()
        self.secondary.request_cancellation()

    def shutdown(self) -> None:
        """Shut down both managers."""
        self.primary.shutdown()
        self.secondary.shutdown()

    def get_progress(self) -> Dict[str, Any]:
        """Get combined progress from both managers."""
        return {
            self.primary_name: getattr(self.primary, "_progress", {}),
            self.secondary_name: getattr(self.secondary, "_progress", {}),
        }
