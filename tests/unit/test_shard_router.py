"""Unit tests for the C6 Phase 1 internal shard router."""

from contextlib import contextmanager

import pytest

from code_indexer.server.services.shard_router import ShardRouter, FORWARD_HEADER


class FakeOwnership:
    def __init__(self, owned, owners):
        self._owned = owned
        self._owners = owners

    def owns(self, alias):
        return self._owned

    def owners_of(self, alias):
        return list(self._owners)


class FakeResponse:
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.posted = None

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response, record):
        self._response = response
        self._record = record

    def post(self, url, json=None, headers=None):
        self._record["url"] = url
        self._record["json"] = json
        self._record["headers"] = headers
        return self._response


class FakeHttpFactory:
    def __init__(self, response, record):
        self._response = response
        self._record = record

    @contextmanager
    def create_sync_client(self, timeout=None):
        self._record["timeout"] = timeout
        yield FakeClient(self._response, self._record)


def _router(ownership, addresses, response=None, record=None):
    record = record if record is not None else {}
    return ShardRouter(
        node_id="me",
        shard_ownership=ownership,
        node_addresses_provider=lambda: addresses,
        http_client_factory=FakeHttpFactory(response, record),
    )


def test_solo_mode_serves_locally():
    r = _router(None, {})
    assert r.target_for("repo") is None


def test_owned_serves_locally():
    r = _router(FakeOwnership(owned=True, owners=["me"]), {"me": "1.1.1.1:8090"})
    assert r.target_for("repo") is None


def test_not_owned_forwards_to_owner_with_address():
    r = _router(
        FakeOwnership(owned=False, owners=["peer", "me"]),
        {"peer": "2.2.2.2:8090", "me": "1.1.1.1:8090"},
    )
    assert r.target_for("repo") == "2.2.2.2:8090"


def test_not_owned_but_no_reachable_owner_serves_locally():
    # Owner has no registered address -> fail open to local.
    r = _router(FakeOwnership(owned=False, owners=["peer"]), {"me": "1.1.1.1:8090"})
    assert r.target_for("repo") is None


def test_unknown_ownership_serves_locally():
    r = _router(FakeOwnership(owned=False, owners=[]), {"peer": "2.2.2.2:8090"})
    assert r.target_for("repo") is None


def test_address_provider_error_serves_locally():
    def boom():
        raise RuntimeError("db down")

    router = ShardRouter(
        "me", FakeOwnership(False, ["peer"]), boom, FakeHttpFactory(None, {})
    )
    assert router.target_for("repo") is None


def test_forward_posts_with_loopguard_and_auth_and_returns_json():
    record = {}
    resp = FakeResponse({"results": [], "total_results": 0})
    r = _router(FakeOwnership(False, ["peer"]), {"peer": "2.2.2.2:8090"}, resp, record)
    out = r.forward("2.2.2.2:8090", {"query_text": "x"}, "Bearer tok")
    assert out == {"results": [], "total_results": 0}
    assert record["url"] == "http://2.2.2.2:8090/api/query"
    assert record["json"] == {"query_text": "x"}
    assert record["headers"][FORWARD_HEADER] == "1"
    assert record["headers"]["Authorization"] == "Bearer tok"


def test_forward_propagates_http_error():
    resp = FakeResponse({}, raise_exc=RuntimeError("502"))
    r = _router(FakeOwnership(False, ["peer"]), {"peer": "2.2.2.2:8090"}, resp, {})
    with pytest.raises(RuntimeError):
        r.forward("2.2.2.2:8090", {"q": 1}, None)


def test_forward_omits_auth_when_absent():
    record = {}
    resp = FakeResponse({"ok": True})
    r = _router(FakeOwnership(False, ["peer"]), {"peer": "2.2.2.2:8090"}, resp, record)
    r.forward("2.2.2.2:8090", {}, None)
    assert "Authorization" not in record["headers"]
    assert record["headers"][FORWARD_HEADER] == "1"


def test_forward_uses_path_param():
    record = {}
    resp = FakeResponse({"ok": True})
    r = _router(FakeOwnership(False, ["peer"]), {"peer": "2.2.2.2:8090"}, resp, record)
    r.forward("2.2.2.2:8090", {}, None, path="/api/query/multi")
    assert record["url"] == "http://2.2.2.2:8090/api/query/multi"


class PerAliasOwnership:
    """Ownership stub with per-alias control (owns() and owners_of())."""

    def __init__(self, owned, owners_map):
        self._owned = set(owned)
        self._owners = owners_map

    def owns(self, alias):
        return alias in self._owned

    def owners_of(self, alias):
        return list(self._owners.get(alias, []))


def test_group_by_owner_partitions_local_and_remote():
    # me owns 'a'; 'b'->peer1, 'c'->peer2; 'd' owned by peerX which has no address
    own = PerAliasOwnership({"a"}, {"b": ["peer1"], "c": ["peer2"], "d": ["peerX"]})
    addrs = {"peer1": "1.1.1.1:8090", "peer2": "2.2.2.2:8090"}  # no peerX
    r = ShardRouter("me", own, lambda: addrs, FakeHttpFactory(None, {}))
    local, groups = r.group_by_owner(["a", "b", "c", "d"])
    # 'a' owned locally; 'd' has no reachable owner -> fail open to local
    assert set(local) == {"a", "d"}
    assert groups == {"1.1.1.1:8090": ["b"], "2.2.2.2:8090": ["c"]}


def test_group_by_owner_all_local_when_solo():
    r = _router(None, {})  # no ownership -> solo
    local, groups = r.group_by_owner(["a", "b", "c"])
    assert local == ["a", "b", "c"]
    assert groups == {}


def test_group_by_owner_groups_multiple_repos_per_owner():
    own = PerAliasOwnership(set(), {"b": ["peer1"], "c": ["peer1"], "d": ["peer2"]})
    addrs = {"peer1": "1.1.1.1:8090", "peer2": "2.2.2.2:8090"}
    r = ShardRouter("me", own, lambda: addrs, FakeHttpFactory(None, {}))
    local, groups = r.group_by_owner(["b", "c", "d"])
    assert local == []
    assert groups == {"1.1.1.1:8090": ["b", "c"], "2.2.2.2:8090": ["d"]}


def test_forward_increments_counters():
    r = _router(
        FakeOwnership(False, ["peer"]), {"peer": "2.2.2.2:8090"}, FakeResponse({})
    )
    r.forward("2.2.2.2:8090", {}, None)
    assert r._forwards == 1
    assert r._forward_failures == 0
    r2 = _router(
        FakeOwnership(False, ["peer"]),
        {"peer": "2.2.2.2:8090"},
        FakeResponse({}, raise_exc=RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        r2.forward("2.2.2.2:8090", {}, None)
    assert r2._forwards == 1
    assert r2._forward_failures == 1


def test_stats_reports_identity_ring_and_counters():
    from code_indexer.server.services.shard_ownership import ShardOwnership

    so = ShardOwnership("me", lambda: ["me", "peer"], replicas=2)
    r = ShardRouter(
        "me",
        so,
        lambda: {"me": "1.1.1.1:8090", "peer": "2.2.2.2:8090"},
        FakeHttpFactory(None, {}),
    )
    s = r.stats()
    assert s["node_id"] == "me"
    assert s["replicas"] == 2
    assert set(s["active_nodes"]) == {"me", "peer"}
    assert s["node_addresses"] == {"me": "1.1.1.1:8090", "peer": "2.2.2.2:8090"}
    assert s["forwards"] == 0
    assert s["forward_failures"] == 0
