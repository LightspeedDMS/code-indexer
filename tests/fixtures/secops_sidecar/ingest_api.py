"""Chronicle-mimicking ingest API (ingest port): token endpoint and events:import."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .http_util import content_length, read_bounded_body, send_bytes, send_json
from .import_pipeline import ImportRequest, process_import
from .token_api import handle_token

IMPORT_PATH = re.compile(
    r"^/([^/]+)/projects/([^/]+)/locations/([^/]+)/instances/([^/]+)/events:import$"
)

if TYPE_CHECKING:
    from .server import Sidecar


class IngestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "secops-sidecar-ingest"

    @property
    def sidecar(self) -> "Sidecar":
        return self.server.sidecar  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence access logging: request lines may carry nothing secret, but
        headers do, and the sidecar logs its own sanitised summary instead."""

    def _not_found(self) -> None:
        self.close_connection = True
        send_json(
            self,
            404,
            {"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}},
        )

    def do_GET(self) -> None:  # noqa: N802
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/token":
            handle_token(self)
            return
        match = IMPORT_PATH.match(path)
        if match is None:
            self._not_found()
            return
        self._import(path, match)

    def _import(self, path: str, match: "re.Match[str]") -> None:
        limit = self.sidecar.config.max_request_bytes + 1
        declared = content_length(self) or 0
        body = read_bounded_body(self, limit)
        version, project, location, instance = match.groups()
        req = ImportRequest(
            seq=self.sidecar.state.next_seq(),
            path=path,
            version=version,
            parent=f"projects/{project}/locations/{location}/instances/{instance}",
            authorization=self.headers.get("Authorization"),
            body=body,
        )
        reply = process_import(self.sidecar, req)
        if reply.close or reply.drop or declared > len(body):
            self.close_connection = True  # unread bytes must not reach keep-alive
        if reply.drop:
            return  # accepted, then the connection closes with no response
        if reply.hold_seconds:
            self.sidecar.shutdown_event.wait(reply.hold_seconds)
        send_bytes(
            self,
            reply.status,
            reply.body,
            content_type=reply.content_type,
            headers=reply.headers,
        )
