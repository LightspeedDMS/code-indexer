"""Story #1787 AC12: seed table for the per-language memory multiplier K.

`estimated_peak_bytes = source_bytes * K(language) * safety_factor`. K is
NOT stable across repos of the same language (measured: Keycloak 10.5x,
Elasticsearch 19.0x -- both Java) -- this table is a coarse SEED for Gate
1's pre-extract estimate, self-calibrated over time per AC16
(`k_calibration_store.py`). An unseen language uses the most conservative
(largest) K on record, per the story text.
"""

from __future__ import annotations

# Seeded from the two independent prototype measurements recorded in
# Story #1787's amendment (peak_rss / source_bytes):
#   Keycloak      (1.23M lines Java): 269 MB / 25.5 MB  = 10.5x
#   Elasticsearch (6.76M lines Java): 2578 MB / 135.8 MB = 19.0x (worst case)
# Both measured corpora are Java -- the worse (larger) of the two is kept
# as the seed so Gate 1 never under-estimates on the language we actually
# have data for.
MEASURED_K_SEED_TABLE: dict[str, float] = {
    "java": 19.0,
}

# The most conservative (largest) K in the seed table -- used for any
# language with no seed AND no calibrated sample yet (AC12: "an unseen
# language uses the most conservative K on record"; AC16: "a missing or
# stale K must degrade to the conservative default").
CONSERVATIVE_DEFAULT_K: float = max(MEASURED_K_SEED_TABLE.values())


def k_for_language(language: str) -> float:
    """Returns the seeded multiplier for `language`, or
    `CONSERVATIVE_DEFAULT_K` when the language has no seed entry (or
    `language` is falsy/not a string -- defensive: a caller passing an
    empty/`None` language must degrade to the conservative default, never
    raise). `language` is matched case-insensitively -- language
    identifiers reach this function from several sources (file
    extensions, driver metadata) with inconsistent casing.
    """
    if not isinstance(language, str) or not language:
        return CONSERVATIVE_DEFAULT_K
    return MEASURED_K_SEED_TABLE.get(language.lower(), CONSERVATIVE_DEFAULT_K)
