"""Consume ``search_augmenters()``: sections features add to code search.

The MCP ``search_code`` handler (single-repository path,
``mcp/handlers/search/repo_search.py``) asks :func:`active_search_augmenters`
which contributed sections apply to the search's mode before it searches (so
the query embedding is computed once, shared by the search and every
augmenter), then hands each one the finished search through
:func:`run_search_augmenters` and answers its section under
``query_metadata[key]``. With no contributor the response has no such key.

Process wiring, identical on every node (it follows ``enabled_features``),
replaced as a whole whenever an app is built.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from cidx_sdk import (
    FeatureLoadError,
    SearchAugmenterContribution,
    SearchAugmentRequest,
)

from code_indexer.server.features.loader import LoadedFeature

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_augmenters: Tuple[SearchAugmenterContribution, ...] = ()


def install_feature_search_augmenters(loaded: Sequence[LoadedFeature]) -> List[str]:
    """Replace the installed augmenters with those of *loaded*.

    Raises:
        FeatureLoadError: two features answer under the same key. Nothing is
            installed then.
    """
    global _augmenters
    owner: Dict[str, str] = {}
    found: List[SearchAugmenterContribution] = []
    for item in loaded:
        feature = item.manifest.name
        for contribution in item.contributions.search_augmenters:
            if contribution.key in owner:
                raise FeatureLoadError(
                    f"search augmenter key {contribution.key!r} is contributed by "
                    f"both {owner[contribution.key]!r} and {feature!r}"
                )
            owner[contribution.key] = feature
            found.append(contribution)
    with _lock:
        _augmenters = tuple(found)
    return sorted(owner)


def _is_active(augmenter: SearchAugmenterContribution) -> bool:
    try:
        return bool(augmenter.is_active())
    except Exception:
        logger.warning(
            "search augmenter %r: is_active raised; skipped for this search",
            augmenter.key,
            exc_info=True,
        )
        return False


def active_search_augmenters(
    search_mode: str,
) -> Tuple[SearchAugmenterContribution, ...]:
    """The installed augmenters that apply to *search_mode* and are active."""
    with _lock:
        installed = _augmenters
    return tuple(
        a for a in installed if search_mode in a.search_modes and _is_active(a)
    )


def wants_query_vector(augmenters: Sequence[SearchAugmenterContribution]) -> bool:
    """Whether any of *augmenters* needs the query embedding."""
    return any(a.wants_query_vector for a in augmenters)


def run_search_augmenters(
    augmenters: Sequence[SearchAugmenterContribution],
    request: SearchAugmentRequest,
) -> Dict[str, object]:
    """Each augmenter's section for *request*, by key; an augmenter that
    answers None (or raises: logged) adds none."""
    sections: Dict[str, object] = {}
    for augmenter in augmenters:
        try:
            section: Optional[Sequence[Mapping[str, object]]] = augmenter.augment(
                request
            )
        except Exception:
            logger.warning(
                "search augmenter %r raised; no section for this search",
                augmenter.key,
                exc_info=True,
            )
            continue
        if section is not None:
            sections[augmenter.key] = [dict(item) for item in section]
    return sections
