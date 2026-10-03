"""The lane guard ``scripts/real-server-home-guard.sh``: a test lane that
changed the developer's real ``~/.cidx-server/{launch.json,config.json}``
fails loudly, and the guard never exposes the files' contents (config.json
may hold secrets) -- only names, mtimes and sha256 hashes.  Plus the lane
environment (``real_home_lane_env``) and the lanes' exit-code composition.

Guard tests drive bash with ``CIDX_REAL_HOME_GUARD_DIR`` (test-only
override) pointing at a scratch server home, so they never touch the real
one; the lane-env tests only read the account's real caches and config.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[3]
GUARD = REPO / "scripts" / "real-server-home-guard.sh"
SECRET = "SECRET-MARKER-c0nfig-9f1"
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


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


def test_guard_failure_mentions_a_running_dev_server(tmp_path: Path) -> None:
    """A dev server running concurrently also writes launch.json: the
    message must say so, so nobody blames a test blindly."""
    result = _guarded(tmp_path, "echo '{}' > launch.json")
    assert result.returncode != 0
    assert "dev server" in _output(result)


@pytest.mark.parametrize(
    "suite_rc, guard_rc, expected", [(0, 0, 0), (0, 1, 1), (2, 0, 2), (2, 1, 2)]
)
def test_exit_code_composition(
    tmp_path: Path, suite_rc: int, guard_rc: int, expected: int
) -> None:
    """A suite failure keeps its own code (e.g. the e2e preflight's 2); a
    guard failure on a passing suite turns it into 1."""
    result = _run(
        f"real_home_guard_exit_code {suite_rc} {guard_rc}", tmp_path / "s", None
    )
    assert result.returncode == 0, _output(result)
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize(
    "lane", ["fast-automation.sh", "server-fast-automation.sh", "e2e-automation.sh"]
)
def test_lanes_compose_their_exit_code_with_the_guard(lane: str) -> None:
    text = (REPO / lane).read_text()
    assert "real_home_guard_exit_code" in text, lane
    assert "unset CIDX_REAL_HOME_GUARD_DIR" in text, lane


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
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    pairs = (item.split("=", 1) for item in result.stdout.split("\0") if "=" in item)
    return {name: value for name, value in pairs}


def _account_user_base() -> str:
    """The user base Python reports for the ACCOUNT (passwd) home --
    independent of how this pytest process itself was launched."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUSERBASE"}
    env["HOME"] = str(ACCOUNT_HOME)
    result = subprocess.run(
        ["python3", "-c", "import site; print(site.getuserbase())"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


@pytest.mark.parametrize("spelling", ["plain", "dot-segment", "symlink"])
def test_lane_env_replaces_a_server_data_dir_inside_the_real_home(
    tmp_path: Path, spelling: str
) -> None:
    """'plain' is the baseline; './' segments and symlinks into the real
    server home are caught too because paths are compared by real path
    (only a symlink in tmp_path is created)."""
    real_server_home = ACCOUNT_HOME / ".cidx-server"
    if spelling == "plain":
        inside = str(real_server_home / "data")
    elif spelling == "dot-segment":
        inside = f"{ACCOUNT_HOME}/./.cidx-server/data"
    else:
        link = tmp_path / "link-to-real-server-home"
        link.symlink_to(real_server_home)
        inside = str(link / "data")
    lane = _lane_env(tmp_path, {"CIDX_SERVER_DATA_DIR": inside})
    assert lane["CIDX_SERVER_DATA_DIR"] == str(tmp_path / "lane" / "server-home")
    assert lane["CIDX_DATA_DIR"] == lane["CIDX_SERVER_DATA_DIR"]


def test_lane_env_keeps_user_site_packages(tmp_path: Path) -> None:
    lane = _lane_env(tmp_path, {})
    assert lane["PYTHONUSERBASE"] == _account_user_base()
    child = subprocess.run(
        ["python3", "-c", "import pathlib, fastapi; print(pathlib.Path.home())"],
        env=lane,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == lane["HOME"]


def test_lane_env_gives_git_a_scratch_copy_of_the_global_config(
    tmp_path: Path,
) -> None:
    """Identity is kept (a copy of the account's config), but a test's
    ``git config --global`` writes land in scratch: never the real file.
    Checked read-only -- nothing is written here."""
    real = ACCOUNT_HOME / ".gitconfig"
    if not real.is_file():
        pytest.skip("the account has no ~/.gitconfig")
    lane = _lane_env(tmp_path, {})
    assert "GIT_CONFIG_GLOBAL" not in lane
    scratch_config = Path(lane["HOME"]) / ".gitconfig"
    assert _sha256(scratch_config) == _sha256(real)
    origin = subprocess.run(
        ["git", "config", "--global", "--list", "--show-origin"],
        env=lane,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    first = origin.stdout.splitlines()[0]
    assert first.startswith(f"file:{scratch_config}"), first.split("\t")[0]


def test_lane_env_keeps_the_rust_toolchain_homes(tmp_path: Path) -> None:
    """A scratch HOME must not send rustup/cargo to an empty toolchain home
    (a first-call rustup sync took ~23 s: X-Ray tests then time out)."""
    lane = _lane_env(tmp_path / "a", {})
    for var, name in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        if (ACCOUNT_HOME / name).is_dir() and var not in os.environ:
            assert lane[var] == str(ACCOUNT_HOME / name)
    preset = {"RUSTUP_HOME": "/opt/example-rustup", "CARGO_HOME": "/opt/example-cargo"}
    kept = _lane_env(tmp_path / "b", preset)
    assert kept["RUSTUP_HOME"] == "/opt/example-rustup"
    assert kept["CARGO_HOME"] == "/opt/example-cargo"


def test_lane_env_keeps_the_hugging_face_cache(tmp_path: Path) -> None:
    """VoyageTokenizer reads ${HF_HOME:-~/.cache/huggingface}: a scratch HOME
    would make every lane process download tokenizers (offline: 25 failed)."""
    real_cache = ACCOUNT_HOME / ".cache" / "huggingface"
    lane = _lane_env(tmp_path / "a", {})
    if real_cache.is_dir() and "HF_HOME" not in os.environ:
        assert lane["HF_HOME"] == str(real_cache)
    kept = _lane_env(tmp_path / "b", {"HF_HOME": "/opt/example-hf"})
    assert kept["HF_HOME"] == "/opt/example-hf"


def test_lane_env_user_base_ignores_a_redirected_home(tmp_path: Path) -> None:
    lane = _lane_env(tmp_path, {"HOME": str(tmp_path / "elsewhere")})
    assert lane["PYTHONUSERBASE"] == _account_user_base()
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
    real = os.path.join(str(ACCOUNT_HOME), ".cidx-server")
    result = _run("HOME=/nonexistent; real_home_guard_dir", tmp_path / "state", None)
    assert result.returncode == 0, _output(result)
    assert result.stdout.strip() == real
