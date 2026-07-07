"""C6 sharding is opt-in via cluster.sharding_enabled (default False).

Off (default) => replication mode: lifespan skips wiring ShardOwnership/
ShardRouter, so the fail-open cache gates own-everything and there is no query
forwarding. On => C6 sharding wired. This test pins the config contract; the
lifespan gate reads exactly this field.
"""

from code_indexer.server.utils.config_manager import ClusterConfig


def test_sharding_disabled_by_default():
    assert ClusterConfig().sharding_enabled is False


def test_sharding_enabled_parses_from_cluster_dict():
    # Mirrors how config_manager parses config.json's "cluster" block into a
    # ClusterConfig (ClusterConfig(**config_dict["cluster"])).
    cfg = ClusterConfig(**{"node_id": "pod-a", "sharding_enabled": True})
    assert cfg.node_id == "pod-a"
    assert cfg.sharding_enabled is True


def test_node_id_only_leaves_sharding_off():
    cfg = ClusterConfig(**{"node_id": "pod-b"})
    assert cfg.sharding_enabled is False
