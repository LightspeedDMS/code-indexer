"""
Story #912 AC5/AC6/AC7/AC10/AC11: run_verification_gate tests.

Uses real filesystem fixtures and real rg subprocess.
Timeout simulation monkeypatches subprocess.run to raise TimeoutExpired.

Tests (exhaustive list):
  test_refuted_verdict_passes_through
  test_inconclusive_verdict_passes_through
  test_no_resolvable_source_paths_downgrades
  test_ac11_repo_not_in_domain_downgrades
  test_ac6_file_missing_in_repo_downgrades
  test_ac7_symbol_absent_from_source_repos_pleaser_effect
  test_ac6_and_ac7_both_pass_keeps_confirmed
  test_ac10_rg_timeout_yields_verification_timeout

Module-level helpers (exhaustive list):
  _confirmed_verdict(repo_alias, file_path, symbol) -- build CONFIRMED verdict
  _refuted_verdict()                                 -- build REFUTED verdict
  _inconclusive_verdict()                            -- build INCONCLUSIVE verdict
  _make_domains(src, src_repos, tgt, tgt_repos)      -- build domains_json list
  _make_resolver(mapping)                            -- callable alias->path resolver
  _make_two_repo_dirs(tmp_path, src_symbol_present)  -- shared dir/file/domains/resolver setup
"""

import subprocess
from typing import Dict, Tuple

from code_indexer.server.services.dep_map_repair_bidirectional import (
    CitationLine,
    EdgeAuditVerdict,
    run_verification_gate,
)

_SYMBOL = "PaymentClient"
_FILE_PATH = "src/charge.py"
_TGT_ALIAS = "tgt-repo"
_SRC_ALIAS = "src-repo"


def _confirmed_verdict(
    repo_alias: str = _TGT_ALIAS,
    file_path: str = _FILE_PATH,
    symbol: str = _SYMBOL,
) -> EdgeAuditVerdict:
    """Build a CONFIRMED verdict with one citation using the given fields."""
    return EdgeAuditVerdict(
        verdict="CONFIRMED",
        evidence_type="code",
        citations=(
            CitationLine(
                repo_alias=repo_alias,
                file_path=file_path,
                line_or_range="1",
                symbol_or_token=symbol,
            ),
        ),
        reasoning="test",
        action="auto_backfilled",
        dropped_citation_lines=(),
    )


def _refuted_verdict() -> EdgeAuditVerdict:
    """Build a REFUTED verdict with no citations."""
    return EdgeAuditVerdict(
        verdict="REFUTED",
        evidence_type="none",
        citations=(),
        reasoning="not found",
        action="claude_refuted_pending_operator_approval",
        dropped_citation_lines=(),
    )


def _inconclusive_verdict() -> EdgeAuditVerdict:
    """Build an INCONCLUSIVE verdict with no citations."""
    return EdgeAuditVerdict(
        verdict="INCONCLUSIVE",
        evidence_type="none",
        citations=(),
        reasoning="unclear",
        action="inconclusive_manual_review",
        dropped_citation_lines=(),
    )


def _make_domains(src: str, src_repos: list, tgt: str, tgt_repos: list) -> list:
    """Build a minimal domains_json list for src and tgt domains."""
    return [
        {"name": src, "participating_repos": src_repos},
        {"name": tgt, "participating_repos": tgt_repos},
    ]


def _make_resolver(mapping: Dict[str, str]):
    """Return a path resolver callable that looks up aliases in mapping dict."""

    def resolver(alias: str) -> str:
        return str(mapping.get(alias, ""))

    return resolver


def _make_two_repo_dirs(
    tmp_path,
    src_symbol_present: bool,
) -> Tuple[object, object, list, object]:
    """Create tgt-repo and src-repo under tmp_path.

    tgt-repo always contains PaymentClient in src/charge.py.
    src-repo references PaymentClient only when src_symbol_present=True.
    Returns (tgt_dir, src_dir, domains, resolver).
    """
    tgt_repo = tmp_path / _TGT_ALIAS
    (tgt_repo / "src").mkdir(parents=True)
    (tgt_repo / "src" / "charge.py").write_text(f"class {_SYMBOL}:\n    pass\n")

    src_repo = tmp_path / _SRC_ALIAS
    src_repo.mkdir()
    src_content = (
        f"from tgt import {_SYMBOL}\n"
        if src_symbol_present
        else "def do_something(): pass\n"
    )
    (src_repo / "caller.py").write_text(src_content)

    domains = _make_domains("src", [_SRC_ALIAS], "tgt", [_TGT_ALIAS])
    resolver = _make_resolver({_TGT_ALIAS: str(tgt_repo), _SRC_ALIAS: str(src_repo)})
    return tgt_repo, src_repo, domains, resolver


# ---------------------------------------------------------------------------
# Pass-through tests
# ---------------------------------------------------------------------------


def test_refuted_verdict_passes_through():
    """REFUTED verdict is returned unchanged — no rg calls made."""
    v = _refuted_verdict()
    result = run_verification_gate(v, "src", "tgt", [], lambda a: "", rg_timeout=5)
    assert result is v


def test_inconclusive_verdict_passes_through():
    """INCONCLUSIVE verdict is returned unchanged."""
    v = _inconclusive_verdict()
    result = run_verification_gate(v, "src", "tgt", [], lambda a: "", rg_timeout=5)
    assert result is v


# ---------------------------------------------------------------------------
# No source paths
# ---------------------------------------------------------------------------


def test_no_resolvable_source_paths_downgrades():
    """Resolver returning empty for all source repos → claude_cited_but_unverifiable."""
    domains = _make_domains("src", [_SRC_ALIAS], "tgt", [_TGT_ALIAS])
    resolver = _make_resolver({})
    v = _confirmed_verdict()
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "claude_cited_but_unverifiable"


# ---------------------------------------------------------------------------
# AC11: repo membership
# ---------------------------------------------------------------------------


def test_ac11_repo_not_in_domain_downgrades(tmp_path):
    """AC11: citation repo_alias not in any domain repos → repo_not_in_domain."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    domains = _make_domains("src", ["repo-a"], "tgt", ["repo-b"])
    resolver = _make_resolver({"repo-a": str(repo_a)})
    v = _confirmed_verdict(
        repo_alias="unknown-repo", file_path="src/foo.py", symbol="Symbol"
    )
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "repo_not_in_domain"


# ---------------------------------------------------------------------------
# AC6: citation file existence
# ---------------------------------------------------------------------------


def test_ac6_file_missing_in_repo_downgrades(tmp_path):
    """AC6: cited file does not exist in repo → claude_cited_but_unverifiable."""
    tgt_repo, _, domains, resolver = _make_two_repo_dirs(
        tmp_path, src_symbol_present=True
    )
    # Delete the cited file so rg invocation targets a non-existent path
    (tgt_repo / "src" / "charge.py").unlink()
    v = _confirmed_verdict()
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "claude_cited_but_unverifiable"


# ---------------------------------------------------------------------------
# AC7: source-side reverse check
# ---------------------------------------------------------------------------


def test_ac7_symbol_absent_from_source_repos_pleaser_effect(tmp_path):
    """AC7: symbol in target file but absent from source repos → pleaser_effect_caught."""
    _, _, domains, resolver = _make_two_repo_dirs(tmp_path, src_symbol_present=False)
    v = _confirmed_verdict()
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "pleaser_effect_caught"


# ---------------------------------------------------------------------------
# AC6 + AC7 both pass
# ---------------------------------------------------------------------------


def test_ac6_and_ac7_both_pass_keeps_confirmed(tmp_path):
    """AC6 symbol found in target file AND AC7 symbol in source → verdict stays CONFIRMED."""
    _, _, domains, resolver = _make_two_repo_dirs(tmp_path, src_symbol_present=True)
    v = _confirmed_verdict()
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "CONFIRMED"
    assert result.action == "auto_backfilled"


# ---------------------------------------------------------------------------
# AC10: timeout
# ---------------------------------------------------------------------------


def test_ac10_rg_timeout_yields_verification_timeout(tmp_path, monkeypatch):
    """AC10: subprocess.TimeoutExpired on rg → INCONCLUSIVE/verification_timeout."""
    _, _, domains, resolver = _make_two_repo_dirs(tmp_path, src_symbol_present=True)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    v = _confirmed_verdict()
    result = run_verification_gate(v, "src", "tgt", domains, resolver, rg_timeout=5)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "verification_timeout"
