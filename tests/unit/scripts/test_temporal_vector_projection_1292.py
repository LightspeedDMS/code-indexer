"""
Unit tests for scripts/analysis/temporal_vector_projection.py (Story #1292, AC1/AC2).

Story #1292 requires a git-history-only projection script that computes, per
temporal embedder adapter, the projected vector/file count and token/$ cost --
WITHOUT reading any old index and WITHOUT building an old-style index (AC1) --
plus the legacy per-file-diff formula used to compute (not build) `old_vectors`
for the >=10x reduction ratio (AC2).

Tests verify (TDD RED phase first):
- Pricing is read from the model-spec YAML source (voyage_models.yaml /
  cohere_models.yaml), not a bare hard-coded literal with no documented
  fallback (AC1).
- New-layout vector counting reuses the REAL production chunking function
  (contextual_chunker.chunk_aggregated_document) so projected counts are
  provably identical to what indexing will actually produce (AC2 measured
  check).
- The legacy per-file-diff old_vectors formula (1 message vector + per file
  >=1 chunk at 15% overlap) matches FixedSizeChunker.estimate_chunks exactly.
- walk_commits() only shells out to git (no .code-indexer directory is ever
  created/read) -- proves AC1's "git history alone" claim.
- run_projection()/write_report() produce a well-formed report under
  reports/perf/ with the required fields, including the printed old_vectors.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).parents[3]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "analysis" / "temporal_vector_projection.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "temporal_vector_projection", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    # Register in sys.modules BEFORE exec: the script defines module-level
    # dataclasses, and dataclasses' ClassVar detection does
    # sys.modules.get(cls.__module__) -- an unregistered module resolves to
    # None and crashes with AttributeError during class body execution.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tvp(tmp_path: Path) -> Any:
    return _load_module()


# ---------------------------------------------------------------------------
# AC1: pricing read from config/pricing source
# ---------------------------------------------------------------------------


def test_pricing_voyage_context_4_default_is_012_per_million(tvp: Any) -> None:
    price = tvp.get_pricing_usd_per_million("voyage-context-4")
    assert price == pytest.approx(0.12)


def test_pricing_cohere_embed_v4_reads_documented_rate(tvp: Any) -> None:
    price = tvp.get_pricing_usd_per_million("embed-v4.0")
    assert price == pytest.approx(0.12)


def test_pricing_sourced_from_yaml_not_bare_literal(tvp: Any) -> None:
    """The pricing value must be traceable to the model-spec YAML field,
    not just a number embedded directly in the projection script with no
    documented source -- read the YAML directly and compare.
    """
    from code_indexer.services.voyage_ai import _get_voyage_model_specs
    from code_indexer.services.cohere_embedding import _get_cohere_model_specs

    voyage_specs = _get_voyage_model_specs()
    voyage_price = voyage_specs["voyage_models"]["voyage-context-4"][
        "pricing_usd_per_million_tokens"
    ]
    assert tvp.get_pricing_usd_per_million("voyage-context-4") == pytest.approx(
        voyage_price
    )

    cohere_specs = _get_cohere_model_specs()
    cohere_price = cohere_specs["cohere_models"]["embed-v4.0"][
        "pricing_usd_per_million_tokens"
    ]
    assert tvp.get_pricing_usd_per_million("embed-v4.0") == pytest.approx(cohere_price)


def test_pricing_unknown_embedder_raises(tvp: Any) -> None:
    with pytest.raises(KeyError):
        tvp.get_pricing_usd_per_million("not-a-real-embedder")


# ---------------------------------------------------------------------------
# AC1/AC2: new-layout vector count reuses production chunking exactly
# ---------------------------------------------------------------------------


def test_new_vector_count_matches_chunk_aggregated_document_exactly(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        AggregatedCommitDocument,
    )
    from code_indexer.services.temporal.contextual_chunker import (
        chunk_aggregated_document,
    )

    doc = AggregatedCommitDocument(text="x" * 10_000, provenance=[], file_paths=[])
    expected = len(
        chunk_aggregated_document(doc, chunk_chars=4096, overlap_percentage=0.0)
    )
    actual = tvp.compute_new_vector_count(doc, chunk_chars=4096, overlap_percentage=0.0)
    assert actual == expected
    assert expected == 3  # ceil(10000/4096) == 3, hand-computed


def test_new_vector_count_15pct_overlap_hand_computed(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        AggregatedCommitDocument,
    )

    doc = AggregatedCommitDocument(text="x" * 10_000, provenance=[], file_paths=[])
    # Hand-computed reference: chunk_chars=4096, overlap=0.15 -> overlap_chars=614,
    # step=3482. pos=0 (count1,end4096) -> pos=3482(count2,end7578) ->
    # pos=6964(count3,end<=10000? 6964+4096=11060>10000 so end=10000, done).
    actual = tvp.compute_new_vector_count(
        doc, chunk_chars=4096, overlap_percentage=0.15
    )
    assert actual == 3


def test_new_vector_count_empty_document_is_zero(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        AggregatedCommitDocument,
    )

    doc = AggregatedCommitDocument(text="", provenance=[], file_paths=[])
    assert (
        tvp.compute_new_vector_count(doc, chunk_chars=4096, overlap_percentage=0.0) == 0
    )


# ---------------------------------------------------------------------------
# AC2: legacy per-file-diff old_vectors formula (documented, not built)
# ---------------------------------------------------------------------------


def test_old_vectors_formula_message_plus_one_chunk_per_small_file(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import FileChange

    changes = [
        FileChange(path="a.py", diff_type="modified", diff_text="short diff"),
        FileChange(path="b.py", diff_type="added", diff_text="short diff 2"),
    ]
    # 1 message vector + 1 chunk per small file (each well under 4096 chars)
    assert tvp.compute_old_vectors_for_commit(changes) == 3


def test_old_vectors_formula_matches_fixed_size_chunker_estimate(tvp: Any) -> None:
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
    from code_indexer.config import IndexingConfig
    from code_indexer.services.temporal.commit_aggregator import FileChange

    big_diff = "y" * 20_000
    changes = [FileChange(path="big.py", diff_type="modified", diff_text=big_diff)]

    chunker = FixedSizeChunker(IndexingConfig())
    chunker.chunk_size = 4096
    chunker.overlap_size = int(4096 * 0.15)
    chunker.step_size = chunker.chunk_size - chunker.overlap_size
    expected_file_chunks = chunker.estimate_chunks(big_diff)

    # old_vectors = 1 message vector + file's estimated chunks
    assert tvp.compute_old_vectors_for_commit(changes, chunk_chars=4096) == (
        1 + expected_file_chunks
    )


def test_old_vectors_minimum_one_chunk_per_file_even_if_empty_diff(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import FileChange

    changes = [FileChange(path="empty.py", diff_type="added", diff_text="")]
    # 1 message vector + >=1 chunk for the file even though diff_text is empty
    assert tvp.compute_old_vectors_for_commit(changes) == 2


def test_old_vectors_skips_binary_and_pure_rename(tvp: Any) -> None:
    from code_indexer.services.temporal.commit_aggregator import FileChange

    changes = [
        FileChange(path="img.png", diff_type="binary", diff_text=""),
        FileChange(
            path="new_name.py",
            diff_type="renamed",
            diff_text="",
            old_path="old_name.py",
        ),
        FileChange(path="real.py", diff_type="modified", diff_text="content change"),
    ]
    # Only real.py is content-bearing: 1 message + 1 chunk
    assert tvp.compute_old_vectors_for_commit(changes) == 2


# ---------------------------------------------------------------------------
# AC1: git-history-only walk (no old-index read, no index build)
# ---------------------------------------------------------------------------


def _init_temp_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    for i in range(3):
        (repo_dir / f"file{i}.txt").write_text(f"content {i}\n" * 50)
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo_dir, check=True
        )


def test_walk_commits_git_history_only_no_index_dir_created(
    tvp: Any, tmp_path: Path
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    commits = tvp.walk_commits(repo_dir)
    assert len(commits) == 3

    # AC1: no old-index read, no A/B build -- no .code-indexer dir ever created
    assert not (repo_dir / ".code-indexer").exists()


def test_walk_commits_returns_in_chronological_order(tvp: Any, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo2"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    commits = tvp.walk_commits(repo_dir)
    messages = [c.message for c in commits]
    assert messages == ["commit 0", "commit 1", "commit 2"]


def test_walk_commits_respects_max_commits_bound(tvp: Any, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo3"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    commits = tvp.walk_commits(repo_dir, max_commits=1)
    assert len(commits) == 1


# ---------------------------------------------------------------------------
# AC1/AC2: end-to-end run_projection() + write_report()
# ---------------------------------------------------------------------------


def test_run_projection_produces_per_embedder_and_old_vectors(
    tvp: Any, tmp_path: Path
) -> None:
    repo_dir = tmp_path / "repo4"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    result = tvp.run_projection(repo_dir)

    assert "voyage-context-4" in result.per_embedder
    assert "embed-v4.0" in result.per_embedder
    for name, stats in result.per_embedder.items():
        assert stats.new_vectors > 0
        assert stats.file_count == stats.new_vectors  # AC1: file_count == vector_count
        assert stats.estimated_tokens > 0
        assert stats.estimated_cost_usd > 0

    assert result.old_vectors > 0
    assert result.commit_count == 3


def test_run_projection_new_vs_old_ratio_computed(tvp: Any, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo5"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    result = tvp.run_projection(repo_dir)
    ratio = result.ratio_for("voyage-context-4")
    assert ratio == pytest.approx(
        result.old_vectors / result.per_embedder["voyage-context-4"].new_vectors
    )


def test_write_report_creates_json_under_reports_perf(tvp: Any, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo6"
    repo_dir.mkdir()
    _init_temp_repo(repo_dir)

    result = tvp.run_projection(repo_dir)
    output_dir = tmp_path / "reports_out"
    report_path = tvp.write_report(result, output_dir, repo_label="tiny-fixture-repo")

    assert report_path.exists()
    assert report_path.parent == output_dir
    data = json.loads(report_path.read_text())

    assert data["repo"] == "tiny-fixture-repo"
    assert data["commit_count"] == 3
    assert data["old_vectors"] == result.old_vectors
    assert "voyage-context-4" in data["per_embedder"]
    assert "embed-v4.0" in data["per_embedder"]
    for name, stats in data["per_embedder"].items():
        assert set(stats.keys()) >= {
            "new_vectors",
            "file_count",
            "estimated_tokens",
            "estimated_cost_usd",
            "ratio_vs_old",
            "overlap_percentage",
            "pricing_usd_per_million_tokens",
        }


# ---------------------------------------------------------------------------
# AC2: measured predicted-vs-actual comparison helper (pure logic)
# ---------------------------------------------------------------------------


def test_within_tolerance_exact_match(tvp: Any) -> None:
    assert tvp.within_tolerance(predicted=100, actual=100, tolerance_pct=0.0) is True


def test_within_tolerance_rejects_large_mismatch(tvp: Any) -> None:
    assert tvp.within_tolerance(predicted=100, actual=50, tolerance_pct=0.05) is False


def test_within_tolerance_accepts_small_mismatch_within_bound(tvp: Any) -> None:
    assert tvp.within_tolerance(predicted=100, actual=103, tolerance_pct=0.05) is True
