import json
from pathlib import Path
from typing import Iterator, List

import pytest

from .live_server import run_cli, running_cidx_server
from .secret_scan import StepResult

pytestmark = pytest.mark.slow

CLI_TIMEOUT_S = 120
SEEDED_ADMIN = ("admin", "admin")
CORPUS_URLS = [
    "https://github.com/pallets/click",
    "https://github.com/tiangolo/typer",
    "https://github.com/tqdm/tqdm",
    "https://github.com/pallets/flask",
    "https://github.com/pallets/markupsafe",
    "https://github.com/jhy/jsoup",
    "https://github.com/microsoft/TypeScript",
    "https://github.com/Homebrew/homebrew-core",
]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with running_cidx_server(tmp_path_factory.mktemp("perf_server_home")) as url:
        yield url


def _run(url: str, username: str, password: str, out_dir: Path) -> StepResult:
    return run_cli(
        [
            "--url",
            url,
            "--username",
            username,
            "--password",
            password,
            "--quick",
            "--output-dir",
            str(out_dir),
        ],
        timeout=CLI_TIMEOUT_S,
    )


def _tally(stderr: str) -> dict:
    line = next(x for x in stderr.splitlines() if x.startswith("requests sent: "))
    return dict(json.loads(line[len("requests sent: ") :]))


def test_guard_aborts_naming_every_missing_repo_without_write_requests(
    live_server: str, tmp_path: Path
):
    done = _run(live_server, *SEEDED_ADMIN, tmp_path)
    assert done.returncode == 2, done.stdout + done.stderr
    missing: List[str] = [
        u for u in CORPUS_URLS if f"{u} (not registered)" not in done.stderr
    ]
    assert missing == []
    assert "must register and index" in done.stderr
    assert _tally(done.stderr) == {
        "POST /auth/login": 1,
        "GET /health": 1,
        "MCP list_global_repos": 1,
    }
    assert not list(tmp_path.iterdir()), "no report is written on a guard abort"


def test_rejected_login_is_not_retried_and_password_never_printed(
    live_server: str, tmp_path: Path
):
    password = "Wrong-Example-Pa55-7319"
    done = _run(live_server, "admin", password, tmp_path)
    assert done.returncode == 2
    assert "login rejected" in done.stderr
    assert _tally(done.stderr) == {"POST /auth/login": 1}
    assert password not in done.stdout + done.stderr
