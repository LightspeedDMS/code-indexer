"""Self-tests for the local fake VoyageAI embedding server.

Run: python3 -m pytest scripts/analysis/reembed_repro/tests -q
(not collected by the project gates: pyproject testpaths = ["tests"]).
"""

import hashlib
import math
import socket
import time

import httpx
import pytest

from fake_voyage_server import (
    EmbeddingLedger,
    FakeVoyageServer,
    deterministic_vector,
)

SENTINEL_KEY = "reembed-repro-fake-key"


@pytest.fixture
def server():
    srv = FakeVoyageServer(expected_api_key=SENTINEL_KEY)
    srv.start(host="127.0.0.1", port=0)
    try:
        yield srv
    finally:
        srv.stop()


def _post(
    server, texts, key=SENTINEL_KEY, model="voyage-code-3", path="/v1/embeddings"
):
    with httpx.Client(timeout=10) as client:
        return client.post(
            f"{server.base_url}{path}",
            json={"input": texts, "model": model},
            headers={"Authorization": f"Bearer {key}"},
        )


def test_deterministic_vector_is_unit_length_1024_and_stable():
    a = deterministic_vector("hello", 1024)
    b = deterministic_vector("hello", 1024)
    c = deterministic_vector("world", 1024)
    assert len(a) == 1024
    assert a == b
    assert a != c
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0, rel_tol=1e-3)


def test_embeddings_response_shape_matches_voyage(server):
    resp = _post(server, ["one", "two", "three"])
    assert resp.status_code == 200
    body = resp.json()
    assert [d["index"] for d in body["data"]] == [0, 1, 2]
    assert all(len(d["embedding"]) == 1024 for d in body["data"])
    assert body["data"][1]["embedding"] == deterministic_vector("two", 1024)
    assert body["usage"]["total_tokens"] > 0


def test_ledger_counts_inputs_requests_and_duplicates_per_run(server):
    server.ledger.begin_run("run-1")
    _post(server, ["a", "b"])
    _post(server, ["c"])
    server.ledger.begin_run("run-2")
    _post(server, ["a", "d", "d"])
    r1 = server.ledger.run_stats("run-1")
    r2 = server.ledger.run_stats("run-2")
    assert (r1["requests"], r1["inputs"], r1["dup_prior_runs"], r1["dup_same_run"]) == (
        2,
        3,
        0,
        0,
    )
    # "a" was embedded in run-1; the second "d" repeats within run-2.
    assert (r2["requests"], r2["inputs"], r2["dup_prior_runs"], r2["dup_same_run"]) == (
        1,
        3,
        1,
        1,
    )
    assert r2["new_unique"] == 1
    totals = server.ledger.totals()
    assert totals["inputs"] == 6
    assert totals["unique_hashes"] == 4


def test_ledger_exposes_hash_set_of_a_run(server):
    server.ledger.begin_run("r")
    _post(server, ["x"])
    assert server.ledger.run_hashes("r") == {hashlib.sha256(b"x").hexdigest()}


def test_wrong_api_key_is_rejected_and_recorded_as_violation(server):
    resp = _post(server, ["a"], key="some-other-key")
    assert resp.status_code == 401
    assert any("api key" in v for v in server.ledger.violations())
    assert server.ledger.totals()["inputs"] == 0


def test_unknown_path_is_recorded_as_violation(server):
    resp = _post(server, ["a"], path="/v1/rerank")
    assert resp.status_code == 404
    assert any("/v1/rerank" in v for v in server.ledger.violations())


def test_unknown_model_is_rejected_and_recorded(server):
    resp = _post(server, ["a"], model="voyage-nope")
    assert resp.status_code == 400
    assert any("voyage-nope" in v for v in server.ledger.violations())


def test_options_probe_and_stats_endpoint(server):
    server.ledger.begin_run("r")
    _post(server, ["a"])
    with httpx.Client(timeout=10) as client:
        assert client.options(f"{server.base_url}/v1/embeddings").status_code == 200
        stats = client.get(f"{server.base_url}/stats").json()
    assert stats["totals"]["inputs"] == 1
    assert stats["runs"]["r"]["inputs"] == 1


def test_wait_for_run_inputs_returns_true_once_threshold_reached(server):
    server.ledger.begin_run("r")
    assert server.ledger.wait_for_run_inputs(1, timeout=0.2) is False
    _post(server, ["a", "b"])
    assert server.ledger.wait_for_run_inputs(2, timeout=0.2) is True


def test_client_disconnect_is_counted_not_printed(server, capsys):
    try:
        raise ConnectionResetError("peer reset")
    except ConnectionResetError:
        server.handle_error(("127.0.0.1", 5555))
    assert server.ledger.totals()["aborted_responses"] == 1
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("handler bug")
    except ValueError:
        server.handle_error(("127.0.0.1", 5555))
    assert any("ValueError" in v for v in server.ledger.violations())


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_truncated_request_body_counts_as_aborted(server):
    host, port = server.base_url.rsplit("//", 1)[1].split(":")
    with socket.create_connection((host, int(port))) as sock:
        sock.sendall(
            b"POST /v1/embeddings HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Authorization: Bearer " + SENTINEL_KEY.encode() + b"\r\n"
            b"Content-Length: 100\r\n\r\n" + b'{"input": '
        )
    assert _wait_until(lambda: server.ledger.totals()["aborted_responses"] == 1)
    assert server.ledger.violations() == []


def test_complete_malformed_body_is_a_violation(server):
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            f"{server.base_url}/v1/embeddings",
            content=b"not json",
            headers={"Authorization": f"Bearer {SENTINEL_KEY}"},
        )
    assert resp.status_code == 400
    assert any("malformed" in v for v in server.ledger.violations())


def test_ledger_rejects_unknown_run_label():
    ledger = EmbeddingLedger()
    with pytest.raises(KeyError):
        ledger.run_stats("missing")


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_run_key_counts_include_same_run_duplicates(server):
    server.ledger.begin_run("r")
    _post(server, ["a", "b"])
    _post(server, ["a"])
    assert server.ledger.run_key_counts("r") == {_h("a"): 2, _h("b"): 1}


def test_every_answered_request_is_recorded_processed_and_written(server):
    server.ledger.begin_run("r")
    _post(server, ["a", "b"])
    (req,) = server.ledger.requests("r")
    assert req.keys == (_h("a"), _h("b"))
    assert req.processed_at is not None
    assert _wait_until(lambda: server.ledger.requests("r")[0].written_at is not None)


def test_hold_keeps_responses_in_flight_and_kill_captures_them(server):
    import threading

    server.ledger.begin_run("r", hold_seconds=1.0)
    worker = threading.Thread(target=_post, args=(server, ["held"]))
    worker.start()
    assert server.ledger.wait_for_held(1, timeout=5) is True
    assert server.ledger.held_count() == 1
    unwritten = server.ledger.mark_kill("r")
    worker.join(timeout=10)
    (req,) = server.ledger.requests("r")
    assert unwritten == {req.request_id}
    assert req.written_at is not None and req.written_at > server.ledger.kill_at("r")


def test_boundary_classifies_durability_per_key_and_delivery_separately():
    ledger = EmbeddingLedger()
    ledger.begin_run("r")
    mixed = ledger.record(["m1", "m2"])  # delivered; only m1 was saved
    ledger.record(["d"])  # undelivered at the kill, but "d" is durable anyway
    ledger.record(["n"])  # undelivered and not durable
    ledger.finish(mixed, written=True)
    ledger.mark_kill("r")
    result = ledger.boundary("r", durable={_h("m1"), _h("d")})
    assert result["summary"] == {
        "requests": 3,
        "responses_delivered": 1,
        "responses_undelivered": 2,
        "key_sends": 4,
        "durable": 2,
        "in_flight": 2,
        "saved": 1,
        "durable_undelivered": 1,
        "received_unsaved": 1,
        "unanswered": 1,
        "inflight_items": 2,
    }
    states = {row["key"]: row["state"] for row in result["keys"]}
    assert states == {
        _h("m1"): "saved",
        _h("m2"): "received_unsaved",
        _h("d"): "durable_undelivered",
        _h("n"): "unanswered",
    }


def test_boundary_of_an_uninterrupted_run_treats_written_responses_as_delivered():
    ledger = EmbeddingLedger()
    ledger.begin_run("r")
    ledger.finish(ledger.record(["a"]), written=True)
    summary = ledger.boundary("r", durable={_h("a")})["summary"]
    assert (summary["saved"], summary["responses_delivered"], summary["in_flight"]) == (
        1,
        1,
        0,
    )


def test_client_gone_during_a_held_tls_response_is_aborted_not_a_violation(tmp_path):
    import json as _json
    import ssl

    from sandbox import PROVIDER_HOST, prepare_sandbox_assets

    assets = prepare_sandbox_assets(tmp_path / "assets")
    srv = FakeVoyageServer(expected_api_key=SENTINEL_KEY)
    srv.start("127.0.0.1", 0, str(assets.leaf_pem), str(assets.leaf_key))
    try:
        srv.ledger.begin_run("r", hold_seconds=1.0)
        port = int(srv.base_url.rsplit(":", 1)[1])
        ctx = ssl.create_default_context(cafile=str(assets.ca_pem))
        body = _json.dumps({"input": ["held"], "model": "voyage-code-3"}).encode()
        with socket.create_connection(("127.0.0.1", port)) as raw:
            with ctx.wrap_socket(raw, server_hostname=PROVIDER_HOST) as tls:
                tls.sendall(
                    b"POST /v1/embeddings HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Authorization: Bearer " + SENTINEL_KEY.encode() + b"\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                assert srv.ledger.wait_for_held(1, timeout=5)
        # The client (an interrupted child) is gone while its response is held.
        assert _wait_until(lambda: srv.ledger.held_count() == 0, timeout=10)
        (req,) = srv.ledger.requests("r")
        assert srv.ledger.violations() == []
        assert req.aborted is True and req.written_at is None
        assert srv.ledger.totals()["aborted_responses"] == 1
    finally:
        srv.stop()
