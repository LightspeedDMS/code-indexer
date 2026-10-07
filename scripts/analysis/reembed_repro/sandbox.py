"""Network sandbox for the reproduction: no route to any real provider.

The whole harness (fake server + every ``cidx`` child) runs inside a private
user + network + mount namespace:

* the network namespace has only a loopback interface -- no route and no DNS
  to the internet, so a real provider is unreachable at the OS level;
* a namespace-private ``/etc/hosts`` (bind mount, host file untouched) maps
  ``api.voyageai.com`` to 127.0.0.1, where the fake serves TLS on port 443
  with a leaf certificate signed by a throwaway harness CA;
* the child trusts that CA via ``SSL_CERT_FILE`` (honored by httpx), so the
  unmodified indexer talks to the fake exactly as it would to the provider,
  even though ``--server-managed-provider-settings`` pins the real endpoint
  URL;
* a nested user namespace maps the process back to the invoking uid, so the
  indexer never runs as (namespace) root.

Entering the sandboxed half needs BOTH a private parent-to-child handshake
(a one-time token in an inherited pipe; ``open_handshake``/``accept_handshake``)
and an isolation proof made from inside the namespace (``verify_isolation``).
No environment variable selects it, so nothing a shell can carry does.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import shlex
import shutil
import site
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

PROVIDER_HOST = "api.voyageai.com"
#: Cap for a RAM-backed repo directory (a 199k-file soak needs a few GB).
TMPFS_SIZE = "24g"
SENTINEL_API_KEY = "reembed-repro-fake-key"
AUDIT_DIR = Path(__file__).resolve().parent / "child_audit"
# RFC 5737 TEST-NET-1: never a real destination; used only to prove "no route".
_UNROUTABLE_PROBE = ("192.0.2.1", 443)
_DROP_ENV = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|^VOYAGE|^COHERE|^CO_API|PROXY|^SSL_CERT|"
    r"^REQUESTS_CA_BUNDLE|^PYTHONPATH$)",
    re.IGNORECASE,
)
_TOKEN_BYTES = 32
_HANDSHAKE_MAX_BYTES = 4 * _TOKEN_BYTES


class HandshakeError(RuntimeError):
    """The parent-to-child sandbox handshake is missing or not genuine."""


@dataclass(frozen=True)
class SandboxAssets:
    ca_pem: Path
    leaf_pem: Path
    leaf_key: Path
    hosts_file: Path


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def prepare_sandbox_assets(directory: Path) -> SandboxAssets:
    """Write a throwaway CA, a provider-host leaf cert and a hosts file."""
    directory.mkdir(parents=True, exist_ok=True)
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "7",
        "-subj",
        "/CN=reembed-repro-ca",
        cwd=directory,
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "leaf.key",
        "-out",
        "leaf.csr",
        "-subj",
        f"/CN={PROVIDER_HOST}",
        cwd=directory,
    )
    (directory / "leaf.ext").write_text(f"subjectAltName=DNS:{PROVIDER_HOST}\n")
    _openssl(
        "x509",
        "-req",
        "-in",
        "leaf.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "leaf.pem",
        "-days",
        "7",
        "-extfile",
        "leaf.ext",
        cwd=directory,
    )
    hosts = directory / "hosts"
    hosts.write_text(f"127.0.0.1 localhost\n::1 localhost\n127.0.0.1 {PROVIDER_HOST}\n")
    return SandboxAssets(
        ca_pem=directory / "ca.pem",
        leaf_pem=directory / "leaf.pem",
        leaf_key=directory / "leaf.key",
        hosts_file=hosts,
    )


def build_sandbox_command(
    assets: SandboxAssets,
    inner_argv: List[str],
    cwd: str,
    tmpfs_dirs: Sequence[Path] = (),
) -> List[str]:
    """Command that runs ``inner_argv`` inside the network sandbox.

    Each of ``tmpfs_dirs`` (which must exist) is overlaid, inside the
    sandbox's private mount namespace only, by a RAM-backed tmpfs; its
    contents vanish when the sandbox exits and the host never sees them.
    Inherited file descriptors (the handshake pipe) pass through unchanged.
    """
    ip_bin = shutil.which("ip") or shutil.which("ip", path="/usr/sbin:/sbin")
    if ip_bin is None:
        raise RuntimeError(
            "the 'ip' tool is required to bring up loopback in the sandbox"
        )
    q = shlex.quote
    # Mapped root here is the invoking user outside, so chown to 0 hands the
    # mount to that user once the nested namespace maps it back.
    tmpfs_steps = [
        f"mount -t tmpfs -o size={TMPFS_SIZE},mode=0700 tmpfs {q(str(d))}; chown 0:0 {q(str(d))}"
        for d in tmpfs_dirs
    ]
    script = "; ".join(
        [
            "set -e",
            f"{q(ip_bin)} link set lo up",
            "echo 0 > /proc/sys/net/ipv4/ip_unprivileged_port_start",
            f"mount --bind {q(str(assets.hosts_file))} /etc/hosts",
            *tmpfs_steps,
            f"cd {q(cwd)}",
            f"exec unshare --user --map-user={os.getuid()} --map-group={os.getgid()} "
            + " ".join(q(a) for a in inner_argv),
        ]
    )
    return [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "sh",
        "-c",
        script,
    ]


def interface_problems(names: Iterable[str]) -> List[str]:
    """A sandbox network namespace has exactly one interface: loopback."""
    others = sorted(n for n in names if n != "lo")
    if others:
        return [f"non-loopback network interfaces present: {others}"]
    return []


def verify_isolation() -> List[str]:
    """Problems that make this process unsafe for the repro ([] = isolated).

    Proven from inside the current network namespace: only ``lo`` exists
    (``if_nameindex`` is namespace-aware), the provider host resolves only to
    loopback, a TEST-NET address has no route, and public DNS fails.
    """
    problems = interface_problems(name for _, name in socket.if_nameindex())
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(PROVIDER_HOST, 443)}
    except OSError as exc:
        addrs = set()
        problems.append(f"{PROVIDER_HOST} does not resolve: {exc}")
    if addrs and addrs != {"127.0.0.1"}:
        problems.append(
            f"{PROVIDER_HOST} resolves to {sorted(addrs)}, not only 127.0.0.1"
        )
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        code = probe.connect_ex(_UNROUTABLE_PROBE)
    finally:
        probe.close()
    if code != errno.ENETUNREACH:
        problems.append(
            f"non-loopback connect returned errno {code}, expected ENETUNREACH"
        )
    try:
        socket.getaddrinfo("example.com", 443)
        problems.append("public DNS resolution works; network is not isolated")
    except OSError:
        pass
    return problems


def open_handshake() -> Tuple[int, str]:
    """Parent side: a pipe holding a one-time token; returns (read fd, argument).

    The read fd is inheritable (pass it with ``pass_fds``); the argument is
    ``"<fd>:<sha256 of the token>"``. The token itself exists only in the pipe.
    """
    token = secrets.token_hex(_TOKEN_BYTES).encode("ascii")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, token)
    finally:
        os.close(write_fd)
    os.set_inheritable(read_fd, True)
    return read_fd, f"{read_fd}:{hashlib.sha256(token).hexdigest()}"


def accept_handshake(argument: str) -> None:
    """Child side: accept only the genuine token from the inherited pipe.

    The fd must be an open FIFO whose whole content (read without blocking)
    hashes to the digest in ``argument``; the fd is closed either way, so a
    handshake is never replayable.
    """
    fd_text, _, digest = argument.partition(":")
    if not fd_text.isdigit() or len(digest) != 64:
        raise HandshakeError(f"malformed sandbox handshake {argument!r}")
    fd = int(fd_text)
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise HandshakeError("sandbox handshake fd is not a pipe")
        os.set_blocking(fd, False)
        data = os.read(fd, _HANDSHAKE_MAX_BYTES + 1)
    except OSError as exc:
        raise HandshakeError(f"sandbox handshake fd unreadable: {exc}") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass  # already closed: the check above has failed and says why
    if len(data) > _HANDSHAKE_MAX_BYTES or not secrets.compare_digest(
        hashlib.sha256(data).hexdigest(), digest
    ):
        raise HandshakeError("sandbox handshake token does not match")


def child_env(
    base: Mapping[str, str],
    src_dir: Path,
    home: Path,
    server_data_dir: Path,
    ca_pem: Path,
    audit_log: Path,
) -> Dict[str, str]:
    """Environment for a ``cidx`` child: no real credentials, local CA, scratch dirs."""
    # The voyage tokenizer reads tokenizer.json from the Hugging Face cache
    # (HF_HOME, default ~/.cache/huggingface) and downloads it otherwise. The
    # child's HOME is a scratch dir, so point it at the real, populated cache.
    hf_home = base.get("HF_HOME")
    if not hf_home:
        if not base.get("HOME"):
            raise RuntimeError(
                "cannot locate the Hugging Face cache: no HF_HOME or HOME"
            )
        hf_home = str(Path(base["HOME"]) / ".cache" / "huggingface")
    env = {k: v for k, v in base.items() if not _DROP_ENV.search(k)}
    env["HF_HOME"] = hf_home
    env.update(
        {
            "VOYAGE_API_KEY": SENTINEL_API_KEY,
            "PYTHONPATH": os.pathsep.join([str(AUDIT_DIR), str(src_dir)]),
            "PYTHONUSERBASE": base.get("PYTHONUSERBASE") or site.getuserbase(),
            "SSL_CERT_FILE": str(ca_pem),
            "CIDX_SERVER_DATA_DIR": str(server_data_dir),
            "HOME": str(home),
            "REEMBED_AUDIT_LOG": str(audit_log),
        }
    )
    return env
