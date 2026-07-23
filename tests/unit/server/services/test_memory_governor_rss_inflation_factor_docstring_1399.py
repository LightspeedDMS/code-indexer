"""
Bug #1399 item 5: memory_governor_rss_inflation_factor is echoed live in
stats but has ZERO behavioral consumer anywhere in src/ -- its docstring
inaccurately claims it is read live in _tick() / corrects LRU-cap eviction
budgets.

Verified by direct grep: `rss_inflation_factor` is set in __init__, exposed
via a read-only property, and echoed in get_snapshot() -- but never consumed
in _tick(), _advance_band(), evict_lru_to_floor(), or anywhere else in the
codebase outside memory_governor.py itself.

Given the scope of a full fix (either implementing a real LRU-cap-inflation
consumer or removing the field) exceeds this pass's low-risk footprint, the
"at minimum" fix per the issue is applied here: correct the docstrings that
falsely claim live behavioral consumption, so operators/future maintainers
are not misled into thinking this setting does something it does not.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOVERNOR_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "memory_governor.py"
)
_CONFIG_MANAGER_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "utils" / "config_manager.py"
)


class TestRssInflationFactorDocstringAccuracy:
    def test_memory_governor_docstring_does_not_claim_live_read(self):
        """
        MemoryGovernor's class docstring must not claim rss_inflation_factor
        is read LIVE alongside the fields that genuinely are (yellow_pct,
        red_pct, hysteresis_pct, swap_forces_red, enabled).
        """
        source = _GOVERNOR_PATH.read_text()

        docstring_start = source.find('class MemoryGovernor:\n    """')
        assert docstring_start != -1, "MemoryGovernor class docstring not found"
        docstring_end = source.find('"""', docstring_start + 30)
        docstring = source[docstring_start:docstring_end]

        assert "rss_inflation_factor" not in docstring or (
            "rss_inflation_factor are all read LIVE" not in docstring
        ), (
            "Bug #1399: MemoryGovernor's class docstring must not claim "
            "rss_inflation_factor is read LIVE -- grep confirms zero "
            "behavioral consumers exist (only get_snapshot() echoes it)."
        )

    def test_config_manager_docstring_does_not_claim_lru_cap_correction(self):
        """
        CacheConfig's memory_governor_rss_inflation_factor field comment must
        not claim it is applied to LRU-cap eviction budget computation --
        no such computation exists anywhere in src/.
        """
        source = _CONFIG_MANAGER_PATH.read_text()

        comment_start = source.find("# memory_governor_rss_inflation_factor:")
        assert comment_start != -1, (
            "memory_governor_rss_inflation_factor comment not found in "
            "config_manager.py"
        )
        comment_end = source.find(
            "memory_governor_rss_inflation_factor: float", comment_start
        )
        comment = source[comment_start:comment_end]

        assert "eviction budgets" not in comment, (
            "Bug #1399: the memory_governor_rss_inflation_factor field "
            "comment in config_manager.py must not claim it corrects "
            "LRU-cap eviction budget computation -- no such consumer "
            "exists (echoed live in stats only)."
        )
