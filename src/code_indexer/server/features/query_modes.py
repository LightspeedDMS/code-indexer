"""Consume ``web_query_modes()``: search modes of the Web UI query page.

The query page (``web/pages/query.py``) runs its own modes (semantic, FTS,
hybrid, temporal) itself and hands a request in any other mode to the
enabled feature that contributes it, after resolving the repository. With no
contributor the mode is not offered and a request in it is refused.

Process wiring, identical on every node (it follows ``enabled_features``),
replaced as a whole whenever an app is built.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence, Tuple

from cidx_sdk import FeatureLoadError, WebQueryModeContribution

from code_indexer.server.features.loader import LoadedFeature

#: The modes the query page runs itself; a feature cannot contribute one.
CORE_QUERY_MODES = frozenset({"semantic", "fts", "hybrid", "temporal"})

_lock = threading.Lock()
_modes: Dict[str, WebQueryModeContribution] = {}


def install_feature_query_modes(loaded: Sequence[LoadedFeature]) -> List[str]:
    """Replace the installed query modes with those of *loaded*.

    Raises:
        FeatureLoadError: a mode is a core mode, or two features contribute
            the same mode. Nothing is installed then.
    """
    owner: Dict[str, str] = {}
    modes: Dict[str, WebQueryModeContribution] = {}
    for item in loaded:
        feature = item.manifest.name
        for contribution in item.contributions.web_query_modes:
            mode = contribution.mode
            if mode in CORE_QUERY_MODES:
                raise FeatureLoadError(
                    f"feature {feature!r} query mode {mode!r} is a core mode"
                )
            if mode in owner:
                raise FeatureLoadError(
                    f"query mode {mode!r} is contributed by both "
                    f"{owner[mode]!r} and {feature!r}"
                )
            owner[mode] = feature
            modes[mode] = contribution
    with _lock:
        _modes.clear()
        _modes.update(modes)
    return sorted(modes)


def feature_query_mode(mode: str) -> Optional[WebQueryModeContribution]:
    """The contributed query mode *mode*, or None."""
    with _lock:
        return _modes.get(mode)


def feature_query_modes() -> Tuple[str, ...]:
    """Every contributed query mode, sorted."""
    with _lock:
        return tuple(sorted(_modes))
