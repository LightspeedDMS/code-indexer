"""Harness: start/stop the sidecar, generate per-run key material.

Used identically by pytest (``start_sidecar``) and by ``e2e-automation.sh``
(``python3 -m tests.fixtures.secops_sidecar.harness keygen ...`` followed by
starting the sidecar process itself).

``harness_endpoint`` and ``token_uri`` come from ONE source: the ingest port.
The generated service-account key file's ``token_uri`` is exactly
``harness_endpoint + "/token"``, which is what the server under test requires
of a harness destination.

Harness boundary with CIDX: a server process accepts the loopback ingest URL
and the key file's loopback ``token_uri`` only when its non-production
fault-injection gate is active (``fault_injection_enabled`` plus
``fault_injection_nonprod_ack`` in the bootstrap config).  Start the server
under test with that gate on.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .server import DEFAULT_TOKEN_ISSUER, READY_LINE_PREFIX

LOOPBACK = "127.0.0.1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECT = "example-project"
DEFAULT_LOCATION = "us"
DEFAULT_INSTANCE = "00000000-0000-0000-0000-000000000000"
DEFAULT_API_VERSION = "v1"
TEST_CLIENT_EMAIL = DEFAULT_TOKEN_ISSUER  # the issuer the sidecar accepts
KEY_FILE_NAME = "sa-key.json"
PUBLIC_KEY_FILE_NAME = "pub.pem"
RSA_KEY_BITS = 2048
RSA_PUBLIC_EXPONENT = 65537
READY_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 5.0
CONTROL_TIMEOUT_SECONDS = 30.0
READ_CHUNK_BYTES = 4096
LOG_TAIL_CHARS = 2000


def harness_endpoint_for(ingest_port: int) -> str:
    return f"http://{LOOPBACK}:{ingest_port}"


def token_uri_for(ingest_port: int) -> str:
    return harness_endpoint_for(ingest_port) + "/token"


def find_free_port() -> int:
    """Bind 127.0.0.1:0, close, and return the port (race-tolerant for tests)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOOPBACK, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


@dataclass(frozen=True)
class KeyMaterial:
    key_file_path: Path
    public_key_path: Path


def write_key_material(
    key_dir: Path, ingest_port: int, project: str = DEFAULT_PROJECT
) -> KeyMaterial:
    """Generate a fresh RSA keypair and a service-account key file in key_dir.

    Never committed: callers pass a scratch directory.  The private key lives
    only inside the key file (mode 0600).
    """
    key_dir.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(
        public_exponent=RSA_PUBLIC_EXPONENT, key_size=RSA_KEY_BITS
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_path = key_dir / PUBLIC_KEY_FILE_NAME
    public_key_path.write_bytes(public_pem)
    key_file_path = key_dir / KEY_FILE_NAME
    key_doc = {
        "type": "service_account",
        "project_id": project,
        "private_key_id": secrets.token_hex(20),
        "private_key": private_pem,
        "client_email": TEST_CLIENT_EMAIL,
        "client_id": "000000000000000000000",
        "token_uri": token_uri_for(ingest_port),
    }
    fd = os.open(str(key_file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(key_doc, fh)
    return KeyMaterial(key_file_path=key_file_path, public_key_path=public_key_path)


@dataclass(frozen=True)
class SidecarCoordinates:
    """Where the sidecar listens and the ONE parent it accepts."""

    ingest_port: int
    control_port: int
    project: str = DEFAULT_PROJECT
    location: str = DEFAULT_LOCATION
    instance: str = DEFAULT_INSTANCE
    api_version: str = DEFAULT_API_VERSION

    def __post_init__(self) -> None:
        for port in (self.ingest_port, self.control_port):
            if not 1 <= port <= 65535:
                raise ValueError(f"not a TCP port: {port}")

    @property
    def parent(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.location}"
            f"/instances/{self.instance}"
        )

    @property
    def import_path(self) -> str:
        return f"/{self.api_version}/{self.parent}/events:import"

    @property
    def harness_endpoint(self) -> str:
        return harness_endpoint_for(self.ingest_port)

    @property
    def token_uri(self) -> str:
        return token_uri_for(self.ingest_port)

    @property
    def control_url(self) -> str:
        return f"http://{LOOPBACK}:{self.control_port}"


class SidecarControl:
    """Client for the control API of a running sidecar (owned or attached)."""

    def __init__(self, control_url: str) -> None:
        if not control_url:
            raise ValueError("control_url must not be empty")
        self.control_url = control_url

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return httpx.get(
            self.control_url + path, params=params, timeout=CONTROL_TIMEOUT_SECONDS
        )

    def post(self, path: str, payload: Dict[str, Any]) -> httpx.Response:
        return httpx.post(
            self.control_url + path, json=payload, timeout=CONTROL_TIMEOUT_SECONDS
        )

    def reset(self, keep_tokens: bool = False) -> None:
        resp = self.post("/_control/reset", {"keep_tokens": keep_tokens})
        if resp.status_code != 200:
            raise RuntimeError(f"sidecar reset failed: {resp.status_code} {resp.text}")


# Every sidecar started by this process, so a session teardown can fail on a
# leaked one (test-only registry; nothing reads it across processes).
_LIVE_HANDLES: List["SidecarHandle"] = []


def live_handles() -> List["SidecarHandle"]:
    return list(_LIVE_HANDLES)


@dataclass(eq=False)
class SidecarHandle:
    """A sidecar process started by this harness."""

    process: "subprocess.Popen[bytes]"
    coords: SidecarCoordinates
    key_material: KeyMaterial
    ready_line: str
    log_path: Path

    @property
    def control(self) -> SidecarControl:
        return SidecarControl(self.coords.control_url)

    def read_key_file(self) -> Dict[str, Any]:
        loaded = json.loads(self.key_material.key_file_path.read_text("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("service-account key file is not a JSON object")
        return loaded

    def stop(self) -> None:
        """SIGTERM, wait up to STOP_TIMEOUT_SECONDS, then SIGKILL (idempotent)."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self in _LIVE_HANDLES:
            _LIVE_HANDLES.remove(self)


def _kill_and_close(process: "subprocess.Popen[bytes]") -> None:
    process.kill()
    process.wait(timeout=STOP_TIMEOUT_SECONDS)
    if process.stdout is not None:
        process.stdout.close()


def _await_ready_line(process: "subprocess.Popen[bytes]", log_path: Path) -> str:
    """Return the READY line, or kill the process and raise (bounded wait).

    Reads raw chunks with os.read (never a buffered readline, which could
    hide an already-buffered line from the selector or block past the
    deadline on a partial line).
    """
    assert process.stdout is not None
    fd = process.stdout.fileno()
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    buffer = b""
    ready: Optional[str] = None
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while ready is None and time.monotonic() < deadline:
                if not sel.select(timeout=max(0.0, deadline - time.monotonic())):
                    continue
                chunk = os.read(fd, READ_CHUNK_BYTES)
                if not chunk:
                    break  # EOF: READY can never arrive
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith(READY_LINE_PREFIX + " "):
                        ready = line
                        break
    finally:
        if ready is None:
            _kill_and_close(process)
    if ready is None:
        tail = log_path.read_text("utf-8", errors="replace")[-LOG_TAIL_CHARS:]
        raise RuntimeError(f"sidecar did not report READY in time; log tail:\n{tail}")
    return ready


def start_sidecar(
    scratch_dir: Path,
    ingest_port: Optional[int] = None,
    control_port: Optional[int] = None,
    *,
    token_ttl_seconds: Optional[int] = None,
    max_request_bytes: Optional[int] = None,
    self_test_defect: Optional[str] = None,
) -> SidecarHandle:
    """Start a sidecar process on loopback ports and wait until it is ready."""
    coords = SidecarCoordinates(
        ingest_port=ingest_port or find_free_port(),
        control_port=control_port or find_free_port(),
    )
    keys = write_key_material(scratch_dir, coords.ingest_port, coords.project)
    cmd = [
        sys.executable, "-m", "tests.fixtures.secops_sidecar",
        "--ingest-port", str(coords.ingest_port),
        "--control-port", str(coords.control_port),
        "--project", coords.project, "--location", coords.location,
        "--instance", coords.instance, "--api-version", coords.api_version,
        "--token-public-key", str(keys.public_key_path),
    ]  # fmt: skip
    if token_ttl_seconds is not None:
        cmd += ["--token-ttl-seconds", str(token_ttl_seconds)]
    if max_request_bytes is not None:
        cmd += ["--max-request-bytes", str(max_request_bytes)]
    if self_test_defect is not None:  # negative-control self-tests only
        cmd += ["--self-test-defect", self_test_defect]
    log_path = scratch_dir / "sidecar.log"
    with open(log_path, "wb") as log_fh:
        process = subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=log_fh
        )
    ready_line = _await_ready_line(process, log_path)
    handle = SidecarHandle(process, coords, keys, ready_line, log_path)
    _LIVE_HANDLES.append(handle)
    try:
        health = handle.control.get("/_control/health")
    except Exception:
        handle.stop()
        raise
    if health.status_code != 200:
        handle.stop()
        raise RuntimeError(f"sidecar health check failed: {health.status_code}")
    return handle


def main(argv: Optional[List[str]] = None) -> int:
    """``keygen``: write per-run key material for a shell-started sidecar."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.fixtures.secops_sidecar.harness"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="generate sa-key.json and pub.pem")
    keygen.add_argument("--dir", type=Path, required=True)
    keygen.add_argument("--ingest-port", type=int, required=True)
    keygen.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args(argv)
    keys = write_key_material(args.dir, args.ingest_port, args.project)
    print(f"key_file={keys.key_file_path}")
    print(f"public_key={keys.public_key_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
