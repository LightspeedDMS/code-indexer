"""Story #1082: named, tunable query-path-cache config knobs.

All caches and TTL bounds are RUNTIME config (no hardcoded literals on the hot
path). These knobs live on CacheConfig so they are surfaced/tunable via the Web
UI config system like the existing cache knobs.
"""

from code_indexer.server.utils.config_manager import CacheConfig


def test_query_path_cache_knobs_exist_with_defaults():
    cfg = CacheConfig()
    # Kill-switch (Story #1082 optional kill-switch).
    assert cfg.query_path_cache_enabled is True
    # Short, conservative TTL for mutable / not-provably-immutable repo config.
    assert cfg.repo_config_cache_ttl_seconds == 30
    # Bounded cardinality (cf. Bug #897 / 900-repo worst case).
    assert cfg.repo_config_cache_max_entries == 2048


def test_query_path_cache_knobs_are_mutable():
    cfg = CacheConfig()
    cfg.query_path_cache_enabled = False
    cfg.repo_config_cache_ttl_seconds = 5
    cfg.repo_config_cache_max_entries = 16
    assert cfg.query_path_cache_enabled is False
    assert cfg.repo_config_cache_ttl_seconds == 5
    assert cfg.repo_config_cache_max_entries == 16
