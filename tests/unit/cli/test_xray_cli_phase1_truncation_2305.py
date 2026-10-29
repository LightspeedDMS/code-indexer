from __future__ import annotations

import pytest

_CUT_RESULT = {
    "matches": [],
    "evaluation_errors": [],
    "files_processed": 3,
    "files_total": None,
    "phase1_candidate_files": 3,
    "elapsed_seconds": 0.1,
    "partial": True,
    "phase1_truncated": True,
    "phase1_truncation_reason": "match_cap",
}


@pytest.mark.parametrize("renderer", ["_print_table", "_render_explore"])
def test_renderer_reports_phase1_cut(renderer, capsys):
    from code_indexer import cli_xray

    getattr(cli_xray, renderer)(dict(_CUT_RESULT))
    out = capsys.readouterr().out

    assert "[PARTIAL: Phase 1 truncated (match_cap)]" in out, out
    assert "Files processed: 3/unknown" in out, out
    assert "timeout" not in out, out
    assert "None" not in out, out
