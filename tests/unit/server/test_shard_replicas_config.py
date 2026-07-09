"""shard_replicas must be configurable and default to functional sharding.

With replicas >= node count, every node owns every repo (no sharding). The
default must be 1 so a 2+ node cluster shards out of the box.
"""

from code_indexer.server.utils.config_manager import ClusterConfig
from code_indexer.server.services.shard_ownership import ShardOwnership


def test_cluster_config_shard_replicas_default_is_one():
    assert ClusterConfig().shard_replicas == 1


def test_replicas_one_distributes_ownership_on_two_nodes():
    nodes = ["nodeA", "nodeB"]
    a = ShardOwnership("nodeA", lambda: nodes, replicas=1)
    b = ShardOwnership("nodeB", lambda: nodes, replicas=1)
    # every alias is owned by exactly one of the two nodes
    for alias in ["r1-global", "r2-global", "r3", "r4"]:
        assert a.owns(alias) != b.owns(alias), alias


def test_replicas_ge_nodecount_makes_everyone_own_everything():
    nodes = ["nodeA", "nodeB"]
    a = ShardOwnership("nodeA", lambda: nodes, replicas=2)
    b = ShardOwnership("nodeB", lambda: nodes, replicas=2)
    for alias in ["r1", "r2"]:
        assert a.owns(alias) and b.owns(alias), alias
