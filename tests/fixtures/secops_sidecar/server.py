"""Listener management: two loopback-only ThreadingHTTPServers.

The ingest listener (Chronicle API + token endpoint) can be closed and
reopened on the SAME port mid-run (the outage switch), which is why the stdlib
server is used: it exposes SO_REUSEADDR and a clean close/reopen.  The control
listener stays up throughout so tests can always end an outage and inspect.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Type

from .state import SidecarState

LOOPBACK = "127.0.0.1"  # there is deliberately no bind option
# The one stdout line the process prints when both listeners accept
# connections; the harness and e2e-automation.sh wait for it.
READY_LINE_PREFIX = "SIDECAR READY"
# The service account the harness generates keys for (its client_email); the
# token endpoint accepts assertions from this issuer only.
DEFAULT_TOKEN_ISSUER = "sidecar-test@example.com"
# Scopes the Chronicle events:import REST reference accepts.
ALLOWED_TOKEN_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/chronicle",
        "https://www.googleapis.com/auth/cloud-platform",
    }
)
logger = logging.getLogger("secops_sidecar")


@dataclass(frozen=True)
class SidecarConfig:
    ingest_port: int
    control_port: int
    project: str
    location: str
    instance: str
    api_version: str
    public_key_pem: bytes
    max_request_bytes: int = 4_000_000
    token_ttl_seconds: int = 3600
    token_issuer: str = DEFAULT_TOKEN_ISSUER

    @property
    def parent(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.location}"
            f"/instances/{self.instance}"
        )

    @property
    def token_audience(self) -> str:
        return f"http://{LOOPBACK}:{self.ingest_port}/token"


class _Listener(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, port: int, handler: Type[BaseHTTPRequestHandler], sidecar: "Sidecar"
    ) -> None:
        super().__init__((LOOPBACK, port), handler)
        self.sidecar = sidecar


class Sidecar:
    """The running service: config, state, and both listeners."""

    def __init__(self, config: SidecarConfig, state: SidecarState) -> None:
        self.config = config
        self.state = state
        self.shutdown_event = threading.Event()
        self._listener_lock = threading.Lock()
        self._ingest: Optional[_Listener] = None
        self._control: Optional[_Listener] = None
        self._threads: list = []

    def _serve(self, listener: _Listener, name: str) -> None:
        thread = threading.Thread(target=listener.serve_forever, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def start(self) -> None:
        from .control_api import ControlHandler
        from .ingest_api import IngestHandler

        self._ingest = _Listener(self.config.ingest_port, IngestHandler, self)
        self._control = _Listener(self.config.control_port, ControlHandler, self)
        self._serve(self._ingest, "sidecar-ingest")
        self._serve(self._control, "sidecar-control")

    @property
    def ingest_listening(self) -> bool:
        return self._ingest is not None

    def refuse_ingest(self) -> None:
        """Close the ingest listener: clients now get connection refused."""
        with self._listener_lock:  # held for the whole close: no reopen race
            listener, self._ingest = self._ingest, None
            if listener is not None:
                listener.shutdown()
                listener.server_close()
                logger.info("outage: ingest listener closed")

    def restore_ingest(self) -> None:
        """Reopen the ingest listener on the same port."""
        from .ingest_api import IngestHandler

        with self._listener_lock:
            if self._ingest is not None:
                return
            listener = _Listener(self.config.ingest_port, IngestHandler, self)
            self._ingest = listener
            self._serve(listener, "sidecar-ingest")
            logger.info("outage ended: ingest listener reopened")

    def stop(self) -> None:
        self.shutdown_event.set()
        self.refuse_ingest()
        if self._control is not None:
            self._control.shutdown()
            self._control.server_close()
            self._control = None
