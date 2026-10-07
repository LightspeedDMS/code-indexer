"""Local, deterministic stand-in for the VoyageAI embeddings endpoint.

Serves ``POST /v1/embeddings`` with the response shape the real provider
returns, so the unmodified ``cidx`` child parses it exactly as it would a
real reply. Every embedded input is counted and its SHA-256 recorded per
harness run, so the harness can tell "re-embedded content already embedded
before" from "embedded new content".

Provider-boundary ledger (design 15.1, Codex E3): every request is recorded
with its keys, when the fake processed it and when its response bytes were
fully written. The harness marks the instant of each kill, then classifies
each request against the keys durable after the child exited: ``saved``,
``received_unsaved`` (response written, never saved: IN FLIGHT) or
``unanswered`` (response not written at the kill: IN FLIGHT).

A run may ``hold`` every response for some seconds, so requests are in flight
at a deterministic SIGTERM (R-d).

Safety: the server accepts only the harness sentinel API key. Any other key,
an unknown path or an unknown model is refused and recorded as a violation;
the harness fails when any violation exists.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set, Tuple, TypedDict

import numpy as np


class BoundaryKeyRow(TypedDict):
    request: int
    key: str
    delivered: bool  # response bytes fully written before the kill
    durable: bool  # in the store or pending after the child exited
    state: str  # "saved" | "durable_undelivered" | "received_unsaved" | "unanswered"


class BoundaryReport(TypedDict):
    summary: Dict[str, int]
    keys: List[BoundaryKeyRow]


#: Dimensions the fake returns, per model (mirrors voyage_ai._VOYAGE_MODEL_DIMENSIONS
#: for the models a code index can use).
MODEL_DIMENSIONS: Dict[str, int] = {
    "voyage-code-3": 1024,
    "voyage-large-2": 1536,
    "voyage-code-2": 1536,
}

EMBEDDINGS_PATH = "/v1/embeddings"


def deterministic_vector(text: str, dims: int) -> List[float]:
    """Unit-length vector derived only from ``text`` (same text, same vector)."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    vec = np.random.default_rng(seed).standard_normal(dims)
    vec /= np.linalg.norm(vec)
    return [float(v) for v in np.round(vec, 6)]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class RequestRecord:
    request_id: int
    run: str
    keys: Tuple[str, ...]
    processed_at: float
    written_at: Optional[float] = None
    aborted: bool = False


@dataclass
class _RunCounters:
    requests: int = 0
    inputs: int = 0
    dup_prior_runs: int = 0
    dup_same_run: int = 0
    hashes: Set[str] = field(default_factory=set)
    key_counts: Counter = field(default_factory=Counter)
    records: List[RequestRecord] = field(default_factory=list)
    hold_seconds: float = 0.0
    kill_at: Optional[float] = None


class EmbeddingLedger:
    """Thread-safe record of every embedded input, segmented by harness run."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._runs: Dict[str, _RunCounters] = {}
        self._current: Optional[str] = None
        self._all_hashes: Set[str] = set()
        self._requests = 0
        self._inputs = 0
        self._aborted = 0
        self._violations: List[str] = []
        self._by_id: Dict[int, RequestRecord] = {}

    def begin_run(self, label: str, hold_seconds: float = 0.0) -> None:
        if hold_seconds < 0:
            raise ValueError("hold_seconds must not be negative")
        with self._cond:
            if label in self._runs:
                raise ValueError(f"run label already used: {label}")
            self._runs[label] = _RunCounters(hold_seconds=hold_seconds)
            self._current = label

    def current_label(self) -> str:
        with self._cond:
            if self._current is None:
                raise RuntimeError("no run has begun")
            return self._current

    def record(self, texts: List[str]) -> int:
        """Record a processed request; return its id."""
        with self._cond:
            label = self._current or "unlabelled"
            run = self._runs.setdefault(label, _RunCounters())
            run.requests += 1
            self._requests += 1
            keys = []
            for text in texts:
                digest = text_hash(text)
                keys.append(digest)
                run.inputs += 1
                self._inputs += 1
                if digest in run.hashes:
                    run.dup_same_run += 1
                elif digest in self._all_hashes:
                    run.dup_prior_runs += 1
                run.hashes.add(digest)
                run.key_counts[digest] += 1
                self._all_hashes.add(digest)
            rec = RequestRecord(self._requests, label, tuple(keys), time.monotonic())
            run.records.append(rec)
            self._by_id[rec.request_id] = rec
            self._cond.notify_all()
            return rec.request_id

    def finish(self, request_id: int, written: bool) -> None:
        """The response was fully written (or the write failed: aborted)."""
        with self._cond:
            rec = self._by_id[request_id]
            if written:
                rec.written_at = time.monotonic()
            else:
                rec.aborted = True
            self._cond.notify_all()

    def hold_seconds_for(self, request_id: int) -> float:
        with self._cond:
            return self._runs[self._by_id[request_id].run].hold_seconds

    def _held(self) -> int:
        if self._current is None:
            return 0
        return sum(
            1
            for r in self._runs[self._current].records
            if r.written_at is None and not r.aborted
        )

    def held_count(self) -> int:
        """Requests of the current run processed but not yet answered."""
        with self._cond:
            return self._held()

    def wait_for_held(self, threshold: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: self._held() >= threshold, timeout=timeout
            )

    def mark_kill(self, label: str) -> Set[int]:
        """Record the kill instant of ``label``; return its unanswered request ids."""
        with self._cond:
            run = self._runs[label]
            run.kill_at = time.monotonic()
            return {r.request_id for r in run.records if r.written_at is None}

    def kill_at(self, label: str) -> Optional[float]:
        with self._cond:
            return self._runs[label].kill_at

    def requests(self, label: str) -> List[RequestRecord]:
        with self._cond:
            return list(self._runs[label].records)

    def run_key_counts(self, label: str) -> Dict[str, int]:
        with self._cond:
            return dict(self._runs[label].key_counts)

    def boundary(self, label: str, durable: Set[str]) -> BoundaryReport:
        """Classify every key sent by ``label`` against the keys durable after exit.

        Durability decides first: a key is in flight iff it is not durable
        (store or pending), whatever happened to its response. Delivery
        (response bytes written before the kill) is reported separately, per
        request, and splits the keys into the four states.
        """
        with self._cond:
            run = self._runs[label]
            kill_at = run.kill_at
            rows: List[BoundaryKeyRow] = []
            counts: "Counter[str]" = Counter()
            for rec in run.records:
                delivered = rec.written_at is not None and (
                    kill_at is None or rec.written_at <= kill_at
                )
                counts[
                    "responses_delivered" if delivered else "responses_undelivered"
                ] += 1
                for key in rec.keys:
                    is_durable = key in durable
                    if is_durable:
                        state = "saved" if delivered else "durable_undelivered"
                    else:
                        state = "received_unsaved" if delivered else "unanswered"
                    counts[state] += 1
                    counts["durable" if is_durable else "in_flight"] += 1
                    rows.append(
                        {
                            "request": rec.request_id,
                            "key": key,
                            "delivered": delivered,
                            "durable": is_durable,
                            "state": state,
                        }
                    )
        summary = {
            "requests": len(run.records),
            "responses_delivered": counts["responses_delivered"],
            "responses_undelivered": counts["responses_undelivered"],
            "key_sends": len(rows),
            "durable": counts["durable"],
            "in_flight": counts["in_flight"],
            "saved": counts["saved"],
            "durable_undelivered": counts["durable_undelivered"],
            "received_unsaved": counts["received_unsaved"],
            "unanswered": counts["unanswered"],
            "inflight_items": counts["in_flight"],
        }
        return {"summary": summary, "keys": rows}

    def record_violation(self, description: str) -> None:
        with self._cond:
            self._violations.append(description)

    def record_aborted(self) -> None:
        """A response the client never read (child killed mid-request)."""
        with self._cond:
            self._aborted += 1

    def violations(self) -> List[str]:
        with self._cond:
            return list(self._violations)

    def wait_for_run_inputs(self, threshold: int, timeout: float) -> bool:
        """Block until the current run has embedded ``threshold`` inputs."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._current is not None
                and self._runs[self._current].inputs >= threshold,
                timeout=timeout,
            )

    def current_run_inputs(self) -> int:
        with self._cond:
            if self._current is None:
                return 0
            return self._runs[self._current].inputs

    def run_stats(self, label: str) -> Dict[str, int]:
        with self._cond:
            run = self._runs[label]
            return {
                "requests": run.requests,
                "inputs": run.inputs,
                "dup_prior_runs": run.dup_prior_runs,
                "dup_same_run": run.dup_same_run,
                "new_unique": run.inputs - run.dup_prior_runs - run.dup_same_run,
            }

    def run_hashes(self, label: str) -> Set[str]:
        with self._cond:
            return set(self._runs[label].hashes)

    def totals(self) -> Dict[str, int]:
        with self._cond:
            return {
                "requests": self._requests,
                "inputs": self._inputs,
                "unique_hashes": len(self._all_hashes),
                "aborted_responses": self._aborted,
            }

    def to_json(self) -> Dict[str, object]:
        with self._cond:
            labels = list(self._runs)
        return {
            "totals": self.totals(),
            "runs": {label: self.run_stats(label) for label in labels},
            "violations": self.violations(),
        }


def _make_handler(server: "FakeVoyageServer") -> type:
    ledger = server.ledger

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _reply(self, status: int, body: object) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802 - health probe
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/stats":
                self._reply(200, ledger.to_json())
                return
            ledger.record_violation(f"unexpected GET {self.path}")
            self._reply(404, {"detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if len(raw) < length:
                # The client went away mid-send (an interrupted child).
                ledger.record_aborted()
                self.close_connection = True
                return
            if self.path != EMBEDDINGS_PATH:
                ledger.record_violation(f"unexpected POST {self.path}")
                self._reply(404, {"detail": "not found"})
                return
            if self.headers.get("Authorization") != f"Bearer {server.expected_api_key}":
                ledger.record_violation(
                    "request carried an api key other than the harness sentinel"
                )
                self._reply(401, {"detail": "invalid api key"})
                return
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                ledger.record_violation(f"malformed request body: {exc}")
                self._reply(400, {"detail": "malformed request body"})
                return
            model = payload.get("model")
            dims = MODEL_DIMENSIONS.get(model)
            if dims is None:
                ledger.record_violation(f"unknown model {model!r}")
                self._reply(400, {"detail": f"unknown model {model}"})
                return
            texts = payload["input"]
            request_id = ledger.record(texts)
            hold = ledger.hold_seconds_for(request_id)
            if hold:
                server.stopping.wait(hold)  # keep the request in flight (R-d)
            data = [
                {
                    "object": "embedding",
                    "embedding": deterministic_vector(t, dims),
                    "index": i,
                }
                for i, t in enumerate(texts)
            ]
            tokens = sum(max(1, len(t) // 4) for t in texts)
            written = False
            try:
                self._reply(
                    200,
                    {
                        "object": "list",
                        "data": data,
                        "model": model,
                        "usage": {"total_tokens": tokens},
                    },
                )
                written = True
            except (ConnectionError, ssl.SSLError):
                # The client (an interrupted child) went away while its
                # response was held or being written: an aborted response,
                # not a harness violation. TLS reports it as SSLError [SYS].
                ledger.record_aborted()
                self.close_connection = True
            finally:
                ledger.finish(request_id, written)

    return Handler


class FakeVoyageServer:
    """Threaded HTTP(S) server answering the VoyageAI embeddings protocol."""

    def __init__(self, expected_api_key: str) -> None:
        self.expected_api_key = expected_api_key
        self.ledger = EmbeddingLedger()
        self.stopping = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._scheme = "http"

    def start(
        self,
        host: str,
        port: int,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
    ) -> None:
        owner = self

        class _Server(ThreadingHTTPServer):
            def handle_error(self, request: object, client_address: object) -> None:
                owner.handle_error(client_address)

        httpd = _Server((host, port), _make_handler(self))
        httpd.daemon_threads = True
        if certfile is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            self._scheme = "https"
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def handle_error(self, client_address: object) -> None:
        """Called from the handling thread while its exception is active."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, ssl.SSLEOFError, ssl.SSLZeroReturnError)):
            self.ledger.record_aborted()
        else:
            self.ledger.record_violation(
                f"handler error for {client_address}: {type(exc).__name__}: {exc}"
            )

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("server not started")
        host, port = self._httpd.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"{self._scheme}://{host_text}:{port}"

    def stop(self) -> None:
        self.stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
