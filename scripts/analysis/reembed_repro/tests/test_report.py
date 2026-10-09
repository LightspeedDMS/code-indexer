"""Self-tests for the harness's report formatting."""

from invariant import Check, Verdict
from report import format_run_table, format_verdict


def _row(**overrides):
    row = {
        "label": "recovery-1",
        "kind": "recovery",
        "node": 1,
        "reconcile": True,
        "interrupt": "SIGTERM@chunks:2000",
        "exit_code": -15,
        "duration_s": 12.5,
        "index_points_before": 100,
        "index_points_after": 90,
        "unindexed_at_start": 5,
        "planned_progress": 805,
        "planned_metadata": 805,
        "chunks_sent": 2004,
        "dup_prior_runs": 1999,
        "dup_same_run": 0,
        "new_unique": 5,
        "reembedded_already_indexed": 1490,
        "status_after": "in_progress",
        "files_processed_after": 0,
    }
    row.update(overrides)
    return row


def test_run_table_has_header_and_one_line_per_run():
    lines = format_run_table(
        [_row(), _row(label="final", interrupt=None, exit_code=0)]
    ).splitlines()
    assert lines[0].split()[:4] == ["run", "kind", "node", "ver"]
    body = [line for line in lines if line.startswith(("recovery-1", "final"))]
    assert len(body) == 2
    first = body[0].split()
    assert first[2] == "1"
    assert "re_idx" in lines[0].split()
    assert "1490" in first
    for expected in (
        "recovery-1",
        "recovery",
        "reconcile",
        "SIGTERM@chunks:2000",
        "-15",
        "805/805",
        "2004",
        "1999",
        "in_progress",
    ):
        assert expected in first
    assert "-" in body[1].split()  # no interrupt


def test_verdict_text_names_each_check():
    verdict = Verdict(
        checks=[
            Check("embedding budget", False, "embedded 9, limit 5"),
            Check("final index complete", True, "ok"),
        ]
    )
    text = format_verdict(verdict)
    assert text.splitlines()[0] == "VERDICT: REPRODUCED"
    assert "FAIL  embedding budget: embedded 9, limit 5" in text
    assert "PASS  final index complete: ok" in text
    assert format_verdict(Verdict(checks=[Check("x", True, "y")])).startswith(
        "VERDICT: NOT REPRODUCED"
    )


def test_verdict_text_shows_skipped_checks_and_titles():
    verdict = Verdict(
        checks=[
            Check("R-b", True, "re_idx 0", title="no stored content re-embedded"),
            Check(
                "A10",
                False,
                "needs S15",
                skipped="SKIPPED-NOT-IMPLEMENTED",
                title="lease",
            ),
        ]
    )
    text = format_verdict(verdict)
    assert text.splitlines()[0] == "VERDICT: INCOMPLETE (1 checks SKIPPED, not passed)"
    assert "PASS  R-b no stored content re-embedded: re_idx 0" in text
    assert "SKIPPED-NOT-IMPLEMENTED  A10 lease: needs S15" in text


def test_full_report_has_header_table_boundaries_verdict_and_guards():
    from report import format_report

    verdict = Verdict(checks=[Check("R-b", False, "cycle-2[12.83.0] re_idx 14")])
    boundaries = {"recovery-1": {"summary": {"requests": 3, "unanswered": 1}}}
    text = format_report(["scenario: default"], [_row()], boundaries, verdict, [])
    lines = text.splitlines()
    assert lines[0] == "scenario: default"
    assert any(line.startswith("recovery-1") for line in lines)
    assert "  recovery-1: {'requests': 3, 'unanswered': 1}" in lines
    assert "VERDICT: REPRODUCED" in lines
    assert "GUARD FAILURE" not in text
    assert "GUARD FAILURE (result invalid):\n  audit log" in format_report(
        [], [_row()], {}, verdict, ["audit log"]
    )


def test_write_reports_writes_text_and_json(tmp_path):
    import json

    from report import write_reports

    verdict = Verdict(checks=[Check("R-c", False, "3 of 9 files", title="content")])
    text = write_reports(
        tmp_path,
        ["scenario: git"],
        [_row()],
        {"versions": {"x": "12.83.0"}},
        {},
        verdict,
        [],
    )
    assert (tmp_path / "report.txt").read_text() == text + "\n"
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["versions"] == {"x": "12.83.0"}
    assert data["verdict"]["reproduced"] is True
    assert data["verdict"]["checks"] == [
        {"name": "R-c", "title": "content", "status": "FAIL", "detail": "3 of 9 files"}
    ]


def test_reports_redact_the_home_prefix(tmp_path):
    from report import write_reports

    home = "/home/example-user"
    verdict = Verdict(checks=[Check("R-b", True, f"read {home}/.tmp/x")])
    text = write_reports(
        tmp_path,
        [f"versions: {{'{home}/.tmp/trees/abc/src': '12.83.0'}}"],
        [_row(command=f"cidx index {home}")],
        {
            "args": {
                "src": f"{home}/.tmp/trees/abc/src",
                "other": "/home/example-user2/x",
            }
        },
        {},
        verdict,
        [],
        home=home,
    )
    written = (tmp_path / "report.txt").read_text() + (
        tmp_path / "report.json"
    ).read_text()
    assert "~/.tmp/trees/abc/src" in written and "~/.tmp/x" in written
    assert f"{home}/" not in written and f'{home}"' not in written
    assert "/home/example-user2/x" in written  # a look-alike prefix is not touched
    assert home + "/" not in text


def test_run_table_shows_the_code_version_of_each_run():
    lines = format_run_table([_row(version="12.83.0")]).splitlines()
    assert "ver" in lines[0].split()
    assert "12.83.0" in lines[2].split()
