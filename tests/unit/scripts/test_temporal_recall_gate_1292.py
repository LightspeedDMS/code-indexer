"""
Unit tests for scripts/analysis/temporal_recall_gate.py (Story #1292, AC5).

AC5 requires an ABSOLUTE recall-quality gate: a curated benchmark corpus of
queries with known-relevant commit hashes, run against the NEW per-commit
index on a representative repo, with NO comparison to the old index. These
tests cover the harness's PURE logic (parsing `cidx query` CLI output into
ranked commit hashes, and the top-K/dedup-by-commit hit evaluation) without
requiring a live index or network access -- the actual corpus run against a
real repo is a separate, documented integration run (see reports/perf/).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).parents[3]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "analysis" / "temporal_recall_gate.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("temporal_recall_gate", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def trg(tmp_path: Path) -> Any:
    return _load_module()


_SAMPLE_CLI_OUTPUT = """\
1. unknown
   Score: 0.465
   Commit: bc1cafa (2016-09-12)
   Author: jsbattig <jsbattig@gmail.com>
   Message: Merge pull request #12


2. unknown
   Score: 0.364
   Commit: cf12fc0 (2016-09-12)
   Author: jsbattig <jsbattig@gmail.com>
   Message: Merge pull request #24


3. unknown
   Score: 0.143
   Commit: dde672c (2023-05-27)
   Author: jsbattig <jsbattig@gmail.com>
   Message: Small fix to allow for working in Delphi 11
"""


def test_parse_commit_hashes_extracts_in_rank_order(trg: Any) -> None:
    hashes = trg.parse_commit_hashes_from_cli_output(_SAMPLE_CLI_OUTPUT)
    assert hashes == ["bc1cafa", "cf12fc0", "dde672c"]


def test_parse_commit_hashes_dedup_by_commit_collapses_repeats(trg: Any) -> None:
    """dedup-by-commit means the SAME commit ranked twice (two chunks matched)
    must appear only ONCE in the ranked hash list used for the top-K check.
    """
    output = (
        _SAMPLE_CLI_OUTPUT
        + "\n4. unknown \n   Score: 0.09\n   Commit: bc1cafa (2016-09-12)\n"
    )
    hashes = trg.parse_commit_hashes_from_cli_output(output)
    assert hashes == ["bc1cafa", "cf12fc0", "dde672c"]


def test_parse_commit_hashes_empty_output_returns_empty_list(trg: Any) -> None:
    assert trg.parse_commit_hashes_from_cli_output("") == []


def test_evaluate_entry_hit_when_expected_hash_within_top_k(trg: Any) -> None:
    ranked = ["aaa1111", "bbb2222", "ccc3333"]
    assert trg.evaluate_entry(ranked, expected_hashes=["ccc3333"], top_k=3) is True


def test_evaluate_entry_miss_when_expected_hash_outside_top_k(trg: Any) -> None:
    ranked = ["aaa1111", "bbb2222", "ccc3333", "ddd4444"]
    assert trg.evaluate_entry(ranked, expected_hashes=["ddd4444"], top_k=3) is False


def test_evaluate_entry_hit_when_any_of_multiple_accepted_hashes_matches(
    trg: Any,
) -> None:
    ranked = ["aaa1111", "bbb2222"]
    assert (
        trg.evaluate_entry(ranked, expected_hashes=["zzz9999", "bbb2222"], top_k=5)
        is True
    )


def test_evaluate_entry_miss_on_empty_ranked_list(trg: Any) -> None:
    assert trg.evaluate_entry([], expected_hashes=["aaa1111"], top_k=5) is False


# ---------------------------------------------------------------------------
# CorpusEntry + report writer
# ---------------------------------------------------------------------------


def test_corpus_entry_defaults_accepted_miss_false(trg: Any) -> None:
    entry = trg.CorpusEntry(
        query="test query",
        expected_commit_hashes=["abc1234"],
        embedder="voyage-context-4",
    )
    assert entry.accepted_miss is False
    assert entry.top_k == 5


def test_write_recall_report_creates_json_with_summary(
    trg: Any, tmp_path: Path
) -> None:
    entry = trg.CorpusEntry(
        query="q1", expected_commit_hashes=["abc1234"], embedder="voyage-context-4"
    )
    results = [
        trg.CorpusResult(entry=entry, ranked_hashes=["abc1234"], hit=True),
    ]
    output_dir = tmp_path / "reports_out"
    report_path = trg.write_recall_report(
        results, output_dir, repo_label="tries (bounded)"
    )

    assert report_path.exists()
    data = json.loads(report_path.read_text())
    assert data["repo"] == "tries (bounded)"
    assert data["total_queries"] == 1
    assert data["hits"] == 1
    assert data["critical_misses"] == 0
    assert data["queries"][0]["query"] == "q1"
    assert data["queries"][0]["hit"] is True


def test_write_recall_report_counts_accepted_miss_separately(
    trg: Any, tmp_path: Path
) -> None:
    entry = trg.CorpusEntry(
        query="q2",
        expected_commit_hashes=["def5678"],
        embedder="embed-v4.0",
        accepted_miss=True,
        note="documented delta: acceptable",
    )
    results = [trg.CorpusResult(entry=entry, ranked_hashes=[], hit=False)]
    output_dir = tmp_path / "reports_out2"
    report_path = trg.write_recall_report(results, output_dir, repo_label="tries")

    data = json.loads(report_path.read_text())
    assert data["hits"] == 0
    assert data["critical_misses"] == 0  # accepted_miss doesn't count as critical
    assert data["accepted_misses"] == 1
