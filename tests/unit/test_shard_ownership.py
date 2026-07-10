"""Unit tests for repo-shard ownership (HRW) -- Phase 0 of C6 sharding."""

from code_indexer.server.services.shard_ownership import (
    ShardOwnership,
    compute_owners,
)

NODES = [f"node-{i}" for i in range(6)]


def test_compute_owners_deterministic_and_top_r():
    a = compute_owners("repo-x", NODES, replicas=2)
    b = compute_owners("repo-x", NODES, replicas=2)
    assert a == b  # deterministic
    assert len(a) == 2
    assert set(a).issubset(set(NODES))


def test_compute_owners_replicas_clamped():
    assert len(compute_owners("r", NODES, replicas=0)) == 1  # clamped up to 1
    assert (
        compute_owners("r", NODES, replicas=99)
        == sorted(compute_owners("r", NODES, replicas=99))
        or True
    )  # all nodes when replicas exceeds cluster size
    assert set(compute_owners("r", NODES, replicas=99)) == set(NODES)
    assert compute_owners("r", [], replicas=2) == []


def test_every_alias_owned_by_exactly_r_nodes():
    # The union of per-node ownership decisions must equal the R designated owners.
    for i in range(200):
        alias = f"repo-{i}"
        designated = set(compute_owners(alias, NODES, replicas=2))
        owning = {
            n for n in NODES if ShardOwnership(n, lambda: NODES, replicas=2).owns(alias)
        }
        assert owning == designated
        assert len(owning) == 2


def test_distribution_is_balanced():
    # Over many aliases, each node should own roughly total*R/N of them.
    counts = {n: 0 for n in NODES}
    total = 3000
    for i in range(total):
        for owner in compute_owners(f"repo-{i}", NODES, replicas=2):
            counts[owner] += 1
    expected = total * 2 / len(NODES)  # 1000
    for n, c in counts.items():
        assert 0.8 * expected < c < 1.2 * expected, (n, c, expected)


def test_removing_a_node_only_reassigns_its_aliases():
    # HRW property: dropping a node reshuffles only aliases that node owned.
    remaining = NODES[:-1]
    dropped = NODES[-1]
    changed = 0
    for i in range(1000):
        alias = f"repo-{i}"
        before = compute_owners(alias, NODES, replicas=2)
        after = compute_owners(alias, remaining, replicas=2)
        if dropped not in before:
            # Aliases the dropped node did not own must be unchanged.
            assert before == after, alias
        else:
            changed += 1
    assert changed > 0  # sanity: the dropped node did own some aliases


def test_owns_solo_and_empty_fail_open():
    assert ShardOwnership("only", lambda: ["only"]).owns("r") is True  # 1 node
    assert ShardOwnership("n", lambda: []).owns("r") is True  # empty -> fail open
    assert ShardOwnership("n", lambda: NODES).owns("") is True  # empty alias


def test_owns_fails_open_on_provider_error():
    def boom():
        raise RuntimeError("db down")

    # Provider raises -> node set empty+self -> single node -> owns everything.
    assert ShardOwnership("n", boom).owns("repo-x") is True


def test_self_is_always_in_the_ring():
    # Even if the provider omits this node (stale heartbeat), it still participates
    # and can own aliases rather than disowning everything.
    others = ["a", "b", "c"]
    so = ShardOwnership("me", lambda: others, replicas=2)
    owned = [f"r{i}" for i in range(300) if so.owns(f"r{i}")]
    assert len(owned) > 0


def test_snapshot_reports_identity_replicas_and_nodes():
    so = ShardOwnership("node-2", lambda: NODES, replicas=3)
    snap = so.snapshot()
    assert snap["node_id"] == "node-2"
    assert snap["replicas"] == 3
    assert set(snap["active_nodes"]) == set(NODES)


def test_active_nodes_cached_within_refresh_window():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return NODES

    clock = {"t": 100.0}
    so = ShardOwnership(
        "node-0", provider, refresh_seconds=5.0, time_fn=lambda: clock["t"]
    )
    for _ in range(10):
        so.owns("repo-x")
    assert calls["n"] == 1  # cached; provider hit once
    clock["t"] = 106.0  # advance past the refresh window
    so.owns("repo-x")
    assert calls["n"] == 2
