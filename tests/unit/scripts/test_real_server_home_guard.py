"""The lane guard ``scripts/real-server-home-guard.sh``: a test lane that
changed the developer's real ``~/.cidx-server/{launch.json,config.json}``
fails loudly, and the guard never exposes the files' contents (config.json
may hold secrets) -- only names, mtimes and sha256 hashes.

Driven through bash with ``CIDX_REAL_HOME_GUARD_DIR`` (test-only override)
pointing at a scratch server home, so these tests never touch the real one.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

GUARD = Path(__file__).resolve().parents[3] / "scripts" / "real-server-home-guard.sh"
SECRET = "SECRET-MARKER-c0nfig-9f1"


def _run(
    script: str, state: Path, server_home: Optional[Path]
) -> subprocess.CompletedProcess:
    env: Dict[str, str] = dict(os.environ)
    env.pop("CIDX_REAL_HOME_GUARD_DIR", None)
    if server_home is not None:
        env["CIDX_REAL_HOME_GUARD_DIR"] = str(server_home)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{GUARD}"; {script}',
            "_",
            str(state),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _guarded(
    tmp_path: Path, change: str, absent: Tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """Snapshot a scratch server home (minus *absent* files), apply *change*
    (a shell command run inside it), then verify."""
    home = tmp_path / ".cidx-server"
    home.mkdir()
    files = {
        "launch.json": '{"host": "127.0.0.1"}',
        "config.json": f'{{"token": "{SECRET}"}}',
    }
    for name, text in files.items():
        if name not in absent:
            (home / name).write_text(text)
    state = tmp_path / "state"
    snap = _run('real_home_guard_snapshot "$1"', state, home)
    assert snap.returncode == 0, snap.stderr
    if change:
        subprocess.run(["bash", "-c", change], cwd=home, check=True, timeout=30)
    return _run('real_home_guard_verify "$1"', state, home)


def _output(result: subprocess.CompletedProcess) -> str:
    return f"{result.stdout}{result.stderr}"


def test_an_untouched_home_verifies(tmp_path: Path) -> None:
    result = _guarded(tmp_path, "")
    assert result.returncode == 0, _output(result)


def test_a_rewritten_launch_json_fails_and_names_it(tmp_path: Path) -> None:
    result = _guarded(tmp_path, 'echo \'{"host": "0.0.0.0"}\' > launch.json')
    assert result.returncode != 0
    assert "launch.json" in _output(result)


def test_a_touched_config_json_with_the_same_content_fails(tmp_path: Path) -> None:
    result = _guarded(tmp_path, "touch -d '2001-01-01' config.json")
    assert result.returncode != 0
    assert "config.json" in _output(result)


def test_a_removed_file_fails(tmp_path: Path) -> None:
    result = _guarded(tmp_path, "rm launch.json")
    assert result.returncode != 0
    assert "launch.json" in _output(result)


def test_a_file_that_appears_fails(tmp_path: Path) -> None:
    result = _guarded(tmp_path, "echo '{}' > launch.json", absent=("launch.json",))
    assert result.returncode != 0
    assert "launch.json" in _output(result)


def test_the_guard_never_prints_file_contents(tmp_path: Path) -> None:
    result = _guarded(
        tmp_path, f'printf \'{{"token": "{SECRET}-rotated"}}\' > config.json'
    )
    assert result.returncode != 0
    assert SECRET not in _output(result)


def _lane_env(tmp_path: Path, preset: Dict[str, str]) -> Dict[str, str]:
    """The environment ``real_home_lane_env SCRATCH`` leaves behind."""
    env: Dict[str, str] = dict(os.environ)
    for name in ("CIDX_SERVER_DATA_DIR", "CIDX_DATA_DIR", "PYTHONUSERBASE"):
        env.pop(name, None)
    env.update(preset)
    scratch = tmp_path / "lane"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{GUARD}"; real_home_lane_env "$1"; env -0',
            "_",
            str(scratch),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    pairs = (item.split("=", 1) for item in result.stdout.split("\0") if "=" in item)
    return {name: value for name, value in pairs}


def test_lane_env_isolates_home_and_the_server_data_dirs(tmp_path: Path) -> None:
    lane = _lane_env(tmp_path, {})
    scratch = tmp_path / "lane"
    assert lane["HOME"] == str(scratch / "home")
    assert lane["CIDX_SERVER_DATA_DIR"] == str(scratch / "server-home")
    assert lane["CIDX_DATA_DIR"] == lane["CIDX_SERVER_DATA_DIR"]
    assert (scratch / "home").is_dir() and (scratch / "server-home").is_dir()


def test_lane_env_keeps_a_server_data_dir_outside_the_real_home(
    tmp_path: Path,
) -> None:
    chunk_dir = str(tmp_path / "chunk1")
    lane = _lane_env(tmp_path, {"CIDX_SERVER_DATA_DIR": chunk_dir})
    assert lane["CIDX_SERVER_DATA_DIR"] == chunk_dir
    assert lane["CIDX_DATA_DIR"] == chunk_dir


def test_lane_env_keeps_user_site_packages_and_git_identity(tmp_path: Path) -> None:
    import site

    lane = _lane_env(tmp_path, {})
    assert lane["PYTHONUSERBASE"] == site.getuserbase()
    real_gitconfig = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".gitconfig"
    if real_gitconfig.exists():
        assert lane["GIT_CONFIG_GLOBAL"] == str(real_gitconfig)
    child = subprocess.run(
        [
            "python3",
            "-c",
            "import pathlib, fastapi; print(pathlib.Path.home())",
        ],
        env=lane,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == lane["HOME"]


def test_lane_env_keeps_the_rust_toolchain_homes(tmp_path: Path) -> None:
    """A scratch HOME must not send rustup/cargo to an empty toolchain home
    (a first-call rustup sync took ~23 s: X-Ray tests then time out)."""
    account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    lane = _lane_env(tmp_path / "a", {})
    for var, name in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        if (account / name).is_dir() and var not in os.environ:
            assert lane[var] == str(account / name)
    preset = {"RUSTUP_HOME": "/opt/example-rustup", "CARGO_HOME": "/opt/example-cargo"}
    kept = _lane_env(tmp_path / "b", preset)
    assert kept["RUSTUP_HOME"] == "/opt/example-rustup"
    assert kept["CARGO_HOME"] == "/opt/example-cargo"


def test_lane_env_user_base_ignores_a_redirected_home(tmp_path: Path) -> None:
    import site

    lane = _lane_env(tmp_path, {"HOME": str(tmp_path / "elsewhere")})
    assert lane["PYTHONUSERBASE"] == site.getuserbase()
    child = subprocess.run(
        ["python3", "-c", "import msgpack"],
        env=lane,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr


def test_without_override_the_guard_targets_the_account_home(tmp_path: Path) -> None:
    """$HOME is ignored (a lane may isolate it): the password database
    decides which home is the real one."""
    real = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".cidx-server")
    result = _run("HOME=/nonexistent; real_home_guard_dir", tmp_path / "state", None)
    assert result.returncode == 0, _output(result)
    assert result.stdout.strip() == real
