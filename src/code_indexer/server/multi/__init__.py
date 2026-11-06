"""
Multi-repository search module.

Provides parallel search execution across multiple repositories with
proper timeout handling and result aggregation.

Includes:
- Multi-repository semantic/FTS/regex/temporal search
(Multi-repository SCIP intelligence is the cidx-scip feature's.)
"""

from .multi_search_config import MultiSearchConfig
from .multi_result_aggregator import MultiResultAggregator
from .multi_search_service import MultiSearchService
from .models import MultiSearchRequest, MultiSearchResponse, MultiSearchMetadata

__all__ = [
    "MultiSearchConfig",
    "MultiResultAggregator",
    "MultiSearchService",
    "MultiSearchRequest",
    "MultiSearchResponse",
    "MultiSearchMetadata",
]
