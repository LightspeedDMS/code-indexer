import subprocess
import sys
from pathlib import Path

import pytest

from perf_suite import cli, client, operations, report

from .conftest import PROJECT_ROOT

RUN_PY = PROJECT_ROOT / "scripts" / "analysis" / "perf_suite" / "run.py"
SECRET = "s3cret-Pa55-example"
UNROUTABLE_URL = "http://192.0.2.1:9"


def test_help_documents_every_option_and_three_argument_usage():
    done = subprocess.run(
        [sys.executable, str(RUN_PY), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    for option in (
        "--url",
        "--username",
        "--password",
        "--baseline",
        "--write-baseline",
        "--force",
        "--tolerance-pct",
        "--label",
        "--quick",
        "--repetitions",
        "--include-cold-semantic",
        "--corpus",
        "--output-dir",
        "--xray-poll-deadline",
    ):
        assert option in done.stdout, option
    assert "run.py --url URL --username USER --password PASS" in done.stdout
    assert "CIDX_PERF_PASSWORD" in done.stdout


def test_password_precedence_flag_then_env_then_prompt():
    def prompt(_: str) -> str:
        return "from-prompt"

    assert (
        cli.resolve_password("from-flag", {"CIDX_PERF_PASSWORD": "env"}, prompt)
        == "from-flag"
    )
    assert cli.resolve_password(None, {"CIDX_PERF_PASSWORD": "env"}, prompt) == "env"
    assert cli.resolve_password(None, {}, prompt) == "from-prompt"


def _argv(*extra: str) -> list:
    return [
        "--url",
        UNROUTABLE_URL,
        "--username",
        "example-user",
        "--password",
        SECRET,
        *extra,
    ]


def test_existing_write_baseline_without_force_exits_2_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    existing = tmp_path / "base.json"
    existing.write_text("{}")
    assert cli.main(_argv("--write-baseline", str(existing))) == 2
    out = capsys.readouterr()
    assert "--force" in out.err
    assert SECRET not in out.out + out.err


def test_missing_baseline_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture):
    assert cli.main(_argv("--baseline", str(tmp_path / "absent.json"))) == 2
    assert "absent.json" in capsys.readouterr().err


def test_invalid_corpus_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture):
    bad = tmp_path / "corpus.yaml"
    bad.write_text("top_n: 0\nrepos: []\n")
    assert cli.main(_argv("--corpus", str(bad))) == 2
    assert "top_n" in capsys.readouterr().err


def test_unreachable_target_exits_2_without_password_in_output(
    capsys: pytest.CaptureFixture,
):
    assert cli.main(_argv("--http-timeout", "0.5")) == 2
    out = capsys.readouterr()
    assert "login" in out.err
    assert SECRET not in out.out + out.err


def _measured_report(health_after: str) -> dict:
    stats = {
        "requests": 4,
        "served": 4,
        "rps": 10.0,
        "p50_ms": 20.0,
        "p95_ms": 30.0,
        "p99_ms": 40.0,
        "errors": 0,
        "wrong": 0,
    }
    scenario = {
        "concurrency": 1,
        "warmup_ms": [25.0],
        "warmup_errors": 0,
        "warmup_wrong": 0,
        "wrong_samples": [],
        "repetitions": [],
        "median": stats,
        "mad": {k: 0.0 for k in stats},
        "aborted": None,
    }
    rep: dict = report.build_report(
        label="example-box",
        server_version="1.2.3",
        registered_repo_count=3,
        corpus_repos=[
            {"url": "https://example.com/a/b", "alias": "example-repo-global"}
        ],
        health_before="degraded",
        health_after=health_after,
        settings={},
        duration_s=1.0,
        scenarios={"fts": {"rest": {"1": scenario}, "mcp": None}},
        request_tally={"POST /auth/login": 1},
    )
    return rep


def _report_files(directory: Path) -> tuple:
    return (
        len(list(directory.glob("perf_*.json"))),
        len(list(directory.glob("perf_*.md"))),
    )


def test_unavailable_closing_health_exits_1_and_still_writes_reports(tmp_path: Path):
    rep = _measured_report("unavailable (RequestFailedError: GET /health HTTP 500)")
    code, summary = cli.conclude(rep, None, 10.0, tmp_path, None, False, False)
    assert code == 1
    assert "closing health unavailable" in summary
    assert _report_files(tmp_path) == (1, 1)
    degraded = _measured_report("degraded")
    assert (
        cli.conclude(degraded, None, 10.0, tmp_path / "d", None, False, False)[0] == 0
    )


def test_baseline_mismatch_exits_2_naming_keys_and_still_writes_reports(tmp_path: Path):
    rep = _measured_report("degraded")
    baseline = {
        "label": "x",
        "scenarios": {
            "fts/rest/c1": {"p50_ms": 0.0, "p50_mad": 0.0, "rps": 1.0},
            "regex/mcp/c4": {"p50_ms": 5.0, "p50_mad": 0.1, "rps": 1.0},
        },
    }
    code, summary = cli.conclude(rep, baseline, 10.0, tmp_path, None, False, False)
    assert code == 2
    assert "fts/rest/c1" in summary and "regex/mcp/c4" in summary
    assert _report_files(tmp_path) == (1, 1)


@pytest.mark.parametrize("label", ["someone@host.example.com", "a" * 65, "box a"])
def test_unsafe_label_with_write_baseline_exits_2_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture, label: str
):
    target = tmp_path / "base.json"
    assert cli.main(_argv("--label", label, "--write-baseline", str(target))) == 2
    assert "--label" in capsys.readouterr().err
    assert not target.exists()


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "-inf"])
def test_xray_poll_deadline_must_be_positive(value: str):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(_argv("--xray-poll-deadline", value))
    assert raised.value.code == 2


def test_xray_poll_deadline_parses_and_defaults():
    assert (
        cli.parse_args(_argv("--xray-poll-deadline", "45")).xray_poll_deadline == 45.0
    )
    default = cli.parse_args(_argv()).xray_poll_deadline
    assert default == operations.DEFAULT_XRAY_POLL_DEADLINE_S


_GOOD_ENTRY = '{"p50_ms": 10.0, "p50_mad": 1.0, "rps": 5.0}'


@pytest.mark.parametrize(
    "content, named",
    [
        ('{"scenarios": {"fts/rest/c1": {"p50_ms": 10.0, "rps": 5.0}}}', "fts/rest/c1"),
        (
            '{"scenarios": {"ok/rest/c1": %s, "fts/mcp/c4": '
            '{"p50_ms": "fast", "p50_mad": 1.0, "rps": 5.0}}}' % _GOOD_ENTRY,
            "fts/mcp/c4",
        ),
        ("", "base.json"),
        ("not json at all", "base.json"),
        (
            '{"scenarios": {"nan/rest/c1": {"p50_ms": NaN, "p50_mad": 1.0, "rps": 5.0}}}',
            "nan/rest/c1",
        ),
        (
            '{"scenarios": {"inf/mcp/c1": {"p50_ms": 1.0, "p50_mad": 1.0, "rps": Infinity}}}',
            "inf/mcp/c1",
        ),
    ],
)
def test_malformed_baseline_exits_2_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture, content: str, named: str
):
    baseline = tmp_path / "base.json"
    baseline.write_text(content)
    assert cli.main(_argv("--baseline", str(baseline))) == 2
    err = capsys.readouterr().err
    assert named in err
    assert "login" not in err
    assert "ok/rest/c1" not in err


def test_health_after_failure_is_recorded_not_raised():
    perf = client.PerfClient(UNROUTABLE_URL, "example-user", SECRET, http_timeout=0.5)
    perf._token = "example-token"
    perf._token_obtained_at = float("inf")
    status = cli.health_after_status(perf)
    assert status.startswith("unavailable (")
    assert "TransportError" in status or "ConnectError" in status
    assert SECRET not in status
    assert perf.tally() == {"GET /health": 1}


@pytest.mark.parametrize("reason", ["file not found", "permission denied"])
def test_missing_or_unreadable_baseline_exits_2_with_a_validation_message(
    tmp_path: Path, reason: str
):
    baseline = tmp_path / "baseline.json"
    if reason == "permission denied":
        baseline.write_text('{"scenarios": {}}')
        baseline.chmod(0)
    try:
        done = subprocess.run(
            [sys.executable, str(RUN_PY), "--url", UNROUTABLE_URL]
            + ["--username", "u", "--password", SECRET, "--baseline", str(baseline)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        if baseline.exists():
            baseline.chmod(0o600)
    assert done.returncode == 2
    assert f"{baseline} cannot be read as a perf baseline: {reason}" in done.stderr
    assert "[Errno" not in done.stderr and "Traceback" not in done.stderr


@pytest.mark.parametrize("tolerance", ["nan", "inf", "-1"])
def test_tolerance_must_be_finite_and_not_negative(tolerance: str):
    done = subprocess.run(
        [sys.executable, str(RUN_PY), "--url", UNROUTABLE_URL, "--username", "u"]
        + ["--password", SECRET, "--tolerance-pct", tolerance],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "--tolerance-pct must be a finite number >= 0" in done.stderr
    assert "requests sent" not in done.stderr


@pytest.mark.parametrize("bad", ['"3"', "true", "0", "1.5"])
def test_baseline_repetitions_must_be_a_positive_int(tmp_path: Path, bad: str):
    older = {"p50_ms": 10.0, "p50_mad": 1.0, "rps": 5.0}
    assert report.baseline_problems({"scenarios": {"fts/rest/c1": older}}) == []
    entry = '{"p50_ms": 10.0, "p50_mad": 1.0, "rps": 5.0, "repetitions": ' + bad
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"scenarios": {"fts/rest/c1": ' + entry + "}}}")
    done = subprocess.run(
        [sys.executable, str(RUN_PY), "--url", UNROUTABLE_URL, "--username", "u"]
        + ["--password", SECRET, "--baseline", str(baseline)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "fts/rest/c1 (repetitions not a positive integer)" in done.stderr
    assert "Traceback" not in done.stderr and "requests sent" not in done.stderr
