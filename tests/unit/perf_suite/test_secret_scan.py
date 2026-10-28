import os
import sys
import traceback
from pathlib import Path
from typing import List

import httpx
import pytest

from tests.e2e.conftest import E2EConfig

from .live_server import running_cidx_server
from .secret_scan import (
    Masked,
    ScanError,
    StepFailed,
    StepResult,
    files_containing,
    remove_golden_repos,
    run_step,
    run_with_teardown,
)

SYNTHETIC = "synthetic-Key-0000-example-only"
BIG_FILE_BYTES = 60 * 1024 * 1024
TIMEOUT_S = 0.5
CHILD_TIMEOUT_S = 30

# ^ security every secret check below asserts a plain bool; pytest renders compared operands on failure


def _rendered(exc: BaseException) -> str:
    chained = exc.__cause__ is not None or exc.__context__ is not None
    assert not chained, "exception carries a chain"
    parts = [
        "".join(
            traceback.TracebackException.from_exception(
                exc, capture_locals=True
            ).format()
        )
    ]
    tb = exc.__traceback__
    while tb is not None:
        parts.extend(repr(value) for value in tb.tb_frame.f_locals.values())
        tb = tb.tb_next
    return "\n".join(parts)


def _child(code: str) -> list:
    # ^ why Masked argument, not raw; run_step resolves it for the child only
    return [sys.executable, "-c", code, Masked(SYNTHETIC)]


def _tree(root: Path) -> None:
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / "report.json").write_text('{"note": "%s"}' % SYNTHETIC)
    (root / "server.log").write_text("line one\nleaked " + SYNTHETIC + "\n")
    (root / "blob.db").write_bytes(b"\x00\x01" + SYNTHETIC.encode() + b"\xff")
    (root / "index.bin").write_bytes(b"\x00" * 64 + SYNTHETIC.encode())
    (root / "nested" / "deeper" / "notes.md").write_text(SYNTHETIC)
    (root / "clean.txt").write_text("nothing here")


def test_masked_never_shows_the_value():
    secret = Masked(SYNTHETIC)
    shown = repr(secret) + str(secret) + repr([secret]) + f"{secret}"
    leaked = SYNTHETIC in shown
    assert not leaked, "Masked rendered its value"


def test_files_containing_scans_every_file_and_returns_names_only(tmp_path: Path):
    _tree(tmp_path)
    names = files_containing(Masked(SYNTHETIC), [tmp_path])
    assert sorted(names) == [
        "blob.db",
        "index.bin",
        "notes.md",
        "report.json",
        "server.log",
    ]


def test_files_containing_finds_key_at_end_of_large_file(tmp_path: Path):
    with open(tmp_path / "large.db", "wb") as handle:
        handle.seek(BIG_FILE_BYTES)
        handle.write(SYNTHETIC.encode())
    assert files_containing(Masked(SYNTHETIC), [tmp_path]) == ["large.db"]


def test_files_containing_finds_key_split_across_chunks(tmp_path: Path):
    (tmp_path / "split.bin").write_bytes(b"0123456789" + SYNTHETIC.encode())
    assert files_containing(Masked(SYNTHETIC), [tmp_path], chunk_bytes=16) == [
        "split.bin"
    ]


def test_files_containing_clean_tree_returns_nothing(tmp_path: Path):
    (tmp_path / "a.log").write_text("clean")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert files_containing(Masked(SYNTHETIC), [tmp_path]) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read chmod 000 files")
def test_unreadable_file_fails_loudly_without_the_secret(tmp_path: Path):
    blocked = tmp_path / "blocked.log"
    blocked.write_text("anything")
    blocked.chmod(0)
    try:
        with pytest.raises(ScanError, match="blocked.log: unreadable") as raised:
            files_containing(Masked(SYNTHETIC), [tmp_path])
    finally:
        blocked.chmod(0o600)
    leaked = SYNTHETIC in _rendered(raised.value)
    assert not leaked, "secret reachable from the scan failure"


def test_invalid_utf8_output_is_decoded_not_raised():
    done = run_step(
        "binary child",
        _child("import sys; sys.stdout.buffer.write(bytes([255, 254]))"),
        CHILD_TIMEOUT_S,
    )
    assert done.returncode == 0
    assert "�" in done.stdout


def test_crashing_child_output_never_rendered():
    done = run_step(
        "crashing child",
        _child("import sys; sys.stderr.write(sys.argv[1]); sys.exit(3)"),
        CHILD_TIMEOUT_S,
    )
    child_wrote = SYNTHETIC in done.stderr
    assert child_wrote, "child did not echo the argument"
    with pytest.raises(AssertionError) as raised:
        assert done.returncode == 0, f"crashing child exited {done.returncode}"
    leaked = SYNTHETIC in repr(done) + _rendered(raised.value)
    assert not leaked, "child output reachable from a failing assertion"


def test_step_result_repr_shows_code_and_lengths_only():
    shown = repr(StepResult(3, SYNTHETIC, SYNTHETIC))
    leaked = SYNTHETIC in shown
    assert not leaked, "StepResult repr shows child output"
    assert "returncode=3" in shown


@pytest.mark.parametrize(
    "argv, timeout, kind",
    [
        (_child("import time; time.sleep(60)"), TIMEOUT_S, "TimeoutExpired"),
        (
            ["/nonexistent/perf-suite-child", Masked(SYNTHETIC)],
            CHILD_TIMEOUT_S,
            "FileNotFoundError",
        ),
    ],
)
def test_step_failure_is_sanitized(argv: list, timeout: float, kind: str):
    with pytest.raises(StepFailed) as raised:
        run_step("failing child", argv, timeout)
    assert str(raised.value) == f"failing child: failed ({kind})"
    leaked = SYNTHETIC in _rendered(raised.value)
    assert not leaked, "secret reachable from the step failure"


def test_run_with_teardown_keeps_body_failure_and_prints_problems(capsys):
    def body() -> None:
        raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed"):
        run_with_teardown(body, lambda: ["repo-a: HTTP 500"])
    assert "repo-a: HTTP 500" in capsys.readouterr().err


def test_teardown_exception_never_replaces_body_failure(capsys):
    def body() -> None:
        raise ValueError("body failed")

    def teardown() -> List[str]:
        raise RuntimeError("token unavailable")

    with pytest.raises(ValueError, match="body failed"):
        run_with_teardown(body, teardown)
    assert "RuntimeError" in capsys.readouterr().err


def test_run_with_teardown_fails_when_only_teardown_fails():
    with pytest.raises(AssertionError, match="repo-b: HTTP 404"):
        run_with_teardown(lambda: None, lambda: ["repo-b: HTTP 404"])


def test_run_with_teardown_clean_path_returns():
    calls: List[str] = []

    def body() -> None:
        calls.append("body")

    def teardown() -> List[str]:
        calls.append("teardown")
        return []

    run_with_teardown(body, teardown)
    assert calls == ["body", "teardown"]


def test_e2e_config_repr_hides_secrets():
    config = E2EConfig(
        server_host="127.0.0.1",
        server_port=9,
        admin_user="example-user",
        admin_pass=SYNTHETIC + "-pass",
        seed_cache_dir=Path("/x"),
        server_data_dir=Path("/x"),
        work_dir=Path("/x"),
        voyage_api_key=SYNTHETIC + "-key",
        golden_repo_job_timeout=1.0,
        golden_repo_job_poll_interval=1.0,
        repo_activation_timeout=1.0,
    )
    leaked = SYNTHETIC in repr(config) + str(config)
    assert not leaked, "E2EConfig repr shows a secret field"


@pytest.mark.slow
def test_remove_golden_repos_reports_failures_by_alias(tmp_path: Path):
    with running_cidx_server(tmp_path / "home") as url:
        with httpx.Client(base_url=url, timeout=30.0) as http:
            login = http.post(
                "/auth/login", json={"username": "admin", "password": "admin"}
            )
            assert login.status_code == 200
            failures = remove_golden_repos(
                http, login.json()["access_token"], ["absent-repo"]
            )
    assert len(failures) == 1
    assert failures[0].startswith("absent-repo: HTTP 404")
