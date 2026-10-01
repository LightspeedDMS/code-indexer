"""Test PKI and a loopback HTTPS endpoint for the trusted-CA tests.

``make_ca`` / ``make_leaf`` build real X.509 certificates with
``cryptography``; :class:`TlsEndpoint` is a real TLS server on 127.0.0.1
serving BOTH legs SIEM delivery uses -- ``POST /token`` (an OAuth token
response) and ``POST <import path>`` (``{}``) -- with a certificate issued
by a test CA, so verification succeeds only when that CA is trusted.
"""

from __future__ import annotations

import datetime
import http.server
import json
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

IMPORT_PATH = "/v1/projects/p/locations/l/instances/i/events:import"


@dataclass(frozen=True)
class IssuedCert:
    cert: x509.Certificate
    key: Any

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def key_pem(self) -> bytes:
        return bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def make_ca(
    common_name: str = "Example Test CA",
    *,
    is_ca: bool = True,
    expired: bool = False,
    basic_constraints: bool = True,
) -> IssuedCert:
    key = ec.generate_private_key(ec.SECP256R1())
    start = _now() - datetime.timedelta(days=30 if expired else 1)
    end = (
        _now() - datetime.timedelta(days=1)
        if expired
        else _now() + datetime.timedelta(days=30)
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
    )
    if basic_constraints:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=None), critical=True
        )
    if is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    return IssuedCert(builder.sign(key, hashes.SHA256()), key)


def make_leaf(
    ca: IssuedCert, *, ip: str = "127.0.0.1", dns: str = "localhost"
) -> IssuedCert:
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=10))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(dns), x509.IPAddress(ipaddress.ip_address(ip))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(ca.key, hashes.SHA256())
    )
    return IssuedCert(cert, key)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        if self.path == "/token":
            body = json.dumps(
                {
                    "access_token": "example-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ).encode()
        elif self.path == IMPORT_PATH:
            body = b"{}"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


class _ConnectHandler(http.server.BaseHTTPRequestHandler):
    def do_CONNECT(self) -> None:  # noqa: N802 - http.server API
        import select
        import socket

        host, _, port = self.path.rpartition(":")
        self.server.targets.append(self.path)  # type: ignore[attr-defined]
        upstream = socket.create_connection((host, int(port)), timeout=10)
        self.send_response(200, "Connection established")
        self.end_headers()
        client = self.connection
        try:
            for _ in range(10_000):  # bounded relay loop
                readable, _, _ = select.select([client, upstream], [], [], 5)
                if not readable:
                    return
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        finally:
            upstream.close()

    def log_message(self, *args: Any) -> None:
        return


class ConnectProxy:
    """A loopback HTTP CONNECT proxy recording every tunnel target."""

    def __init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _ConnectHandler
        )
        self._server.daemon_threads = True
        self._server.targets = []  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def targets(self) -> List[str]:
        return list(self._server.targets)  # type: ignore[attr-defined]

    def __enter__(self) -> "ConnectProxy":
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="siem-proxy"
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)


def _relay(a: Any, b: Any) -> None:
    import select

    for _ in range(100_000):  # bounded relay loop
        readable, _, _ = select.select([a, b], [], [], 10)
        if not readable:
            return
        for sock in readable:
            try:
                data = sock.recv(65536)
                # select() cannot see bytes already decrypted and buffered
                # inside an SSL socket: drain them now, or the relay stalls
                pending = getattr(sock, "pending", None)
                while data and pending is not None and pending() > 0:
                    data += sock.recv(pending())
            except (OSError, ssl.SSLError):
                return
            if not data:
                return
            (b if sock is a else a).sendall(data)


class TlsTerminator:
    """A loopback TLS front for a plaintext upstream (e.g. the sidecar):
    clients see *leaf* (issued by a test CA); bytes are piped upstream."""

    def __init__(self, leaf: IssuedCert, scratch: Path, upstream_port: int) -> None:
        import socket

        cert_file, key_file = scratch / "front.pem", scratch / "front.key"
        cert_file.write_text(leaf.pem, encoding="ascii")
        key_file.write_bytes(leaf.key_pem())
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(cert_file), str(key_file))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.5)
        self.port = int(self._listener.getsockname()[1])
        self._upstream_port = upstream_port
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connections = 0

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def _serve_one(self, raw: Any) -> None:
        import socket

        try:
            tls = self._context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        self.connections += 1
        upstream = socket.create_connection(("127.0.0.1", self._upstream_port), 10)
        try:
            _relay(tls, upstream)
        finally:
            upstream.close()
            tls.close()

    def _accept_loop(self) -> None:
        import socket

        while not self._stop.is_set():
            try:
                raw, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            raw.settimeout(None)
            threading.Thread(target=self._serve_one, args=(raw,), daemon=True).start()

    def __enter__(self) -> "TlsTerminator":
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="siem-tls-front"
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._listener.close()


class TlsEndpoint:
    """A loopback HTTPS server presenting *leaf* (issued by a test CA)."""

    def __init__(self, leaf: IssuedCert, scratch: Path) -> None:
        cert_file, key_file = scratch / "leaf.pem", scratch / "leaf.key"
        cert_file.write_text(leaf.pem, encoding="ascii")
        key_file.write_bytes(leaf.key_pem())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_file), str(key_file))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.paths = []  # type: ignore[attr-defined]
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = int(self._server.server_address[1])
        self._thread: Optional[threading.Thread] = None

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    @property
    def paths(self) -> List[str]:
        return list(self._server.paths)  # type: ignore[attr-defined]

    def __enter__(self) -> "TlsEndpoint":
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="siem-tls-endpoint"
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
