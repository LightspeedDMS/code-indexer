"""Self-tests for the network sandbox and the child audit hook."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox import (
    AUDIT_DIR,
    SENTINEL_API_KEY,
    build_sandbox_command,
    child_env,
    prepare_sandbox_assets,
    verify_isolation,
)

HARNESS_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    return prepare_sandbox_assets(tmp_path_factory.mktemp("assets"))


def _run_in_sandbox(assets, code):
    cmd = build_sandbox_command(
        assets, [sys.executable, "-c", code], cwd=str(HARNESS_DIR)
    )
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def test_outside_the_sandbox_isolation_check_reports_problems():
    assert verify_isolation() != []


def test_inside_the_sandbox_isolation_check_passes(assets):
    result = _run_in_sandbox(
        assets, "import json, sandbox; print(json.dumps(sandbox.verify_isolation()))"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_inside_the_sandbox_uid_and_gid_map_back_and_chown_works(assets, tmp_path):
    target = tmp_path / "owned.txt"
    code = (
        "import os, pathlib\n"
        f"p = pathlib.Path({str(target)!r}); p.write_text('x')\n"
        "os.chown(p, os.getuid(), os.getgid())\n"
        "print(os.getuid(), os.getgid())\n"
    )
    result = _run_in_sandbox(assets, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"{os.getuid()} {os.getgid()}"


def test_tmpfs_dir_is_ram_backed_inside_and_left_empty_outside(assets, tmp_path):
    ram_dir = tmp_path / "ram"
    ram_dir.mkdir()
    code = (
        "import pathlib\n"
        f"d = {str(ram_dir)!r}\n"
        "mounts = [l.split() for l in open('/proc/self/mounts')]\n"
        "print([m[2] for m in mounts if m[1] == d])\n"
        "pathlib.Path(d, 'x.txt').write_text('in ram')\n"
    )
    cmd = build_sandbox_command(
        assets, [sys.executable, "-c", code], cwd=str(HARNESS_DIR), tmpfs_dirs=[ram_dir]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "['tmpfs']"
    assert list(ram_dir.iterdir()) == []


def test_fake_server_answers_as_api_voyageai_com_inside_sandbox(assets):
    code = (
        "import os, httpx\n"
        "from fake_voyage_server import FakeVoyageServer\n"
        f"os.environ['SSL_CERT_FILE'] = {str(assets.ca_pem)!r}\n"
        f"s = FakeVoyageServer('{SENTINEL_API_KEY}')\n"
        f"s.start('127.0.0.1', 443, {str(assets.leaf_pem)!r}, {str(assets.leaf_key)!r})\n"
        "s.ledger.begin_run('r')\n"
        "with httpx.Client() as c:\n"
        "    r = c.post('https://api.voyageai.com/v1/embeddings', json={'input': ['x'],"
        " 'model': 'voyage-code-3'}, headers={'Authorization': 'Bearer "
        f"{SENTINEL_API_KEY}'}})\n"
        "print(r.status_code, s.ledger.run_stats('r')['inputs'])\n"
        "s.stop()\n"
    )
    result = _run_in_sandbox(assets, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "200 1"


def test_audit_hook_logs_foreign_connects_and_lookups_only(tmp_path):
    log = tmp_path / "audit.jsonl"
    code = (
        "import socket\n"
        "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)\n"
        "socket.create_connection(srv.getsockname(), timeout=2).close()\n"
        "s = socket.socket(); s.settimeout(0.2)\n"
        "s.connect_ex(('192.0.2.1', 443))\n"
        "try:\n"
        "    socket.getaddrinfo('example.invalid', 443)\n"
        "except OSError:\n"
        "    pass\n"
    )
    env = dict(os.environ, PYTHONPATH=str(AUDIT_DIR), REEMBED_AUDIT_LOG=str(log))
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=30)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert {(e["event"], e["target"]) for e in events} == {
        ("socket.connect", "192.0.2.1"),
        ("socket.getaddrinfo", "example.invalid"),
    }


def test_child_env_strips_provider_keys_and_sets_sentinel(tmp_path):
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/someone",
        "VOYAGE_API_KEY": "real-looking",
        "CO_API_KEY": "x",
        "COHERE_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "SOME_TOKEN": "x",
    }
    env = child_env(
        base,
        src_dir=Path("/src"),
        home=tmp_path / "home",
        server_data_dir=tmp_path / "srv",
        ca_pem=Path("/ca.pem"),
        audit_log=tmp_path / "a.jsonl",
    )
    assert env["VOYAGE_API_KEY"] == SENTINEL_API_KEY
    assert not {
        "CO_API_KEY",
        "COHERE_API_KEY",
        "ANTHROPIC_API_KEY",
        "SOME_TOKEN",
    } & set(env)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(AUDIT_DIR), "/src"]
    assert env["SSL_CERT_FILE"] == "/ca.pem"
    assert env["CIDX_SERVER_DATA_DIR"] == str(tmp_path / "srv")
    assert env["HOME"] == str(tmp_path / "home")
    # The tokenizer is read from the real, already-populated cache, never fetched.
    assert env["HF_HOME"] == "/home/someone/.cache/huggingface"


def test_isolation_check_rejects_a_namespace_with_a_non_loopback_interface(assets):
    import shlex
    import shutil

    ip_bin = shutil.which("ip") or shutil.which("ip", path="/usr/sbin:/sbin")
    assert ip_bin is not None
    code = "import json, sandbox; print(json.dumps(sandbox.verify_isolation()))"
    script = "; ".join(
        [
            "set -e",
            f"{ip_bin} link set lo up",
            f"{ip_bin} link add dummy0 type dummy",
            f"{ip_bin} link set dummy0 up",
            f"mount --bind {shlex.quote(str(assets.hosts_file))} /etc/hosts",
            f"cd {shlex.quote(str(HARNESS_DIR))}",
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
        ]
    )
    cmd = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "sh",
        "-c",
        script,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    problems = json.loads(result.stdout.strip().splitlines()[-1])
    assert any("dummy0" in p for p in problems), problems


def test_handshake_accepts_only_the_token_in_its_inherited_pipe():
    from sandbox import HandshakeError, accept_handshake, open_handshake

    read_fd, argument = open_handshake()
    accept_handshake(argument)  # the genuine pipe and digest
    forged_fd, _ = open_handshake()
    with pytest.raises(HandshakeError):
        accept_handshake(f"{forged_fd}:{'0' * 64}")
    with pytest.raises(HandshakeError):
        accept_handshake(argument)  # the fd was consumed and closed: not replayable


def test_child_env_keeps_an_explicit_hf_home(tmp_path):
    env = child_env(
        {"HOME": "/home/someone", "HF_HOME": "/data/hf"},
        src_dir=Path("/src"),
        home=tmp_path / "home",
        server_data_dir=tmp_path / "srv",
        ca_pem=Path("/ca.pem"),
        audit_log=tmp_path / "a.jsonl",
    )
    assert env["HF_HOME"] == "/data/hf"
