"""
BIDIRECTIONAL_MISMATCH verification gate (Story #912).

Extracted for MESSI Rule 6 compliance. Single responsibility: run rg-based
verification for CONFIRMED EdgeAuditVerdict instances.

Module-level definitions (exhaustive list):
  logger                           -- standard Python logger
  _SAFE_DOMAIN_RE                  -- compiled safe-domain-name regex
  _DEFAULT_RG_TIMEOUT_S            -- default rg subprocess timeout (int constant)
  RgOutcome                        -- Literal type alias for rg result categories
  _is_safe_citation_file_path      -- validate citation file paths
  _int_env                         -- read positive integer from env var
  _rg_default_timeout              -- resolve rg timeout from env or default
  _get_domain_repos                -- look up participating_repos for a domain
  _resolve_repo_paths              -- resolve repo aliases to filesystem paths
  _downgrade_verdict               -- copy original verdict with INCONCLUSIVE + action
  _run_rg                          -- run rg; return RgOutcome; log on error/timeout
  _verify_citation_repo_membership -- AC11: repo must be in domain sets
  _verify_citation_exists_in_file  -- AC6: symbol must exist in cited file
  _verify_source_side_reference    -- AC7: symbol must appear in source repos
  run_verification_gate            -- public entry point for full verification
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from code_indexer.server.services.dep_map_repair_bidirectional_parser import (
    CitationLine,
    EdgeAuditVerdict,
)

logger = logging.getLogger(__name__)

_SAFE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_DEFAULT_RG_TIMEOUT_S: int = 30

# Richer outcome from _run_rg so callers can map each case to the correct action.
# "match"    -- rg exited 0 (pattern found)
# "no_match" -- rg exited 1 (pattern not found, normal rg behavior)
# "timeout"  -- rg subprocess timed out
# "error"    -- rg exited >1 or could not be invoked (OSError/FileNotFoundError)
RgOutcome = Literal["match", "no_match", "timeout", "error"]


def _is_safe_citation_file_path(file_path: str) -> bool:
    """Return True when file_path contains no path-traversal sequences."""
    return bool(file_path) and ".." not in file_path and not file_path.startswith("/")


def _int_env(name: str, default: int) -> int:
    """Read a positive integer from an environment variable; return default on invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "_int_env: %s=%r is not a valid integer; using default %d",
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "_int_env: %s=%r is not positive; using default %d", name, raw, default
        )
        return default
    return value


def _rg_default_timeout() -> int:
    """Resolve rg timeout from CIDX_BIDI_RG_TIMEOUT env var or default."""
    return _int_env("CIDX_BIDI_RG_TIMEOUT", _DEFAULT_RG_TIMEOUT_S)


def _get_domain_repos(
    domain_name: str, domains_json: List[Dict[str, Any]]
) -> List[str]:
    """Return participating_repos for domain_name, or empty list if not found."""
    for d in domains_json:
        if d.get("name") == domain_name:
            repos = d.get("participating_repos") or []
            return list(repos) if isinstance(repos, list) else []
    return []


def _resolve_repo_paths(
    repos: List[str], repo_path_resolver: Callable[[str], str]
) -> List[str]:
    """Resolve alias names to filesystem paths, logging all failures."""
    paths: List[str] = []
    for alias in repos:
        try:
            p = repo_path_resolver(alias)
            if p:
                paths.append(p)
            else:
                logger.warning(
                    "_resolve_repo_paths: resolver returned empty for alias %r", alias
                )
        except (OSError, RuntimeError, KeyError, ValueError) as exc:
            logger.warning(
                "_resolve_repo_paths: resolver raised for alias %r: %s", alias, exc
            )
    return paths


def _downgrade_verdict(original: EdgeAuditVerdict, action: str) -> EdgeAuditVerdict:
    """Return a copy of original downgraded to INCONCLUSIVE with the given action."""
    return EdgeAuditVerdict(
        verdict="INCONCLUSIVE",
        evidence_type=original.evidence_type,
        citations=original.citations,
        reasoning=original.reasoning,
        action=action,
        dropped_citation_lines=original.dropped_citation_lines,
    )


def _run_rg(
    flags: List[str], pattern: str, targets: List[str], context: str, rg_timeout: int
) -> RgOutcome:
    """Run rg with flags + '--' + pattern + targets. Return a RgOutcome.

    Uses '--' end-of-options separator to prevent option injection from
    user-controlled pattern or path strings.
    Distinguishes match (exit 0), no_match (exit 1), and error (exit >1 or
    invocation failure). Logs all non-zero-match outcomes.
    """
    try:
        cp = subprocess.run(
            ["rg"] + flags + ["--", pattern] + targets,
            capture_output=True,
            text=True,
            timeout=rg_timeout,
        )
        if cp.returncode == 0:
            return "match"
        if cp.returncode == 1:
            logger.warning(
                "Verification: rg no-match (%s): pattern %r", context, pattern
            )
            return "no_match"
        logger.warning(
            "Verification: rg exit %d (%s): %s",
            cp.returncode,
            context,
            cp.stderr.strip(),
        )
        return "error"
    except subprocess.TimeoutExpired as exc:
        logger.warning("Verification: rg timed out (%s): %s", context, exc)
        return "timeout"
    except OSError as exc:
        logger.warning("Verification: rg invocation failed (%s): %s", context, exc)
        return "error"


def _verify_citation_repo_membership(
    citation: CitationLine,
    all_domain_repos: set,
    verdict: EdgeAuditVerdict,
) -> Optional[EdgeAuditVerdict]:
    """AC11: cited repo must be in source or target participating_repos."""
    if citation.repo_alias not in all_domain_repos:
        logger.warning(
            "Verification: repo %r not in expected domain repos", citation.repo_alias
        )
        return _downgrade_verdict(verdict, "repo_not_in_domain")
    return None


def _verify_citation_exists_in_file(
    citation: CitationLine,
    repo_path: str,
    verdict: EdgeAuditVerdict,
    rg_timeout: int,
) -> Optional[EdgeAuditVerdict]:
    """AC6: rg -nF <symbol> <repo_path>/<file> must return match."""
    if not _is_safe_citation_file_path(citation.file_path):
        logger.warning(
            "Verification: unsafe citation file_path rejected: %r", citation.file_path
        )
        return _downgrade_verdict(verdict, "claude_cited_but_unverifiable")
    target_file = str(Path(repo_path) / citation.file_path)
    outcome = _run_rg(
        ["-nF"], citation.symbol_or_token, [target_file], target_file, rg_timeout
    )
    if outcome == "match":
        return None
    if outcome == "timeout":
        return _downgrade_verdict(verdict, "verification_timeout")
    # no_match or error both mean the citation is unverifiable
    return _downgrade_verdict(verdict, "claude_cited_but_unverifiable")


def _verify_source_side_reference(
    citation: CitationLine,
    source_paths: List[str],
    verdict: EdgeAuditVerdict,
    rg_timeout: int,
) -> Optional[EdgeAuditVerdict]:
    """AC7: rg -F <symbol> <source_paths...> must return match. source_paths must be non-empty."""
    outcome = _run_rg(
        ["-F"], citation.symbol_or_token, source_paths, "source-side", rg_timeout
    )
    if outcome == "match":
        return None
    if outcome == "timeout":
        return _downgrade_verdict(verdict, "verification_timeout")
    # no_match -> pleaser effect; error -> treat conservatively as unverifiable
    if outcome == "no_match":
        logger.warning(
            "Verification: pleaser effect — symbol %r absent from source repos",
            citation.symbol_or_token,
        )
        return _downgrade_verdict(verdict, "pleaser_effect_caught")
    return _downgrade_verdict(verdict, "claude_cited_but_unverifiable")


def run_verification_gate(
    verdict: EdgeAuditVerdict,
    source_domain: str,
    target_domain: str,
    domains_json: List[Dict[str, Any]],
    repo_path_resolver: Callable[[str], str],
    rg_timeout: int = 0,
) -> EdgeAuditVerdict:
    """Verify CONFIRMED verdicts with rg. REFUTED/INCONCLUSIVE pass through unchanged.

    AC6: Citation file existence check -> claude_cited_but_unverifiable on failure.
    AC7: Source-side reverse check -> pleaser_effect_caught on no-match.
    AC10: Timeout -> verification_timeout action.
    AC11: Cited repo membership -> repo_not_in_domain on failure.
    rg_timeout=0 reads from CIDX_BIDI_RG_TIMEOUT env var.
    """
    if verdict.verdict != "CONFIRMED":
        return verdict

    effective_timeout = rg_timeout if rg_timeout > 0 else _rg_default_timeout()
    source_repos = _get_domain_repos(source_domain, domains_json)
    target_repos = _get_domain_repos(target_domain, domains_json)
    all_domain_repos = set(source_repos) | set(target_repos)
    source_paths = _resolve_repo_paths(source_repos, repo_path_resolver)

    if not source_paths:
        logger.warning(
            "Verification: no resolvable source paths for domain %r", source_domain
        )
        return _downgrade_verdict(verdict, "claude_cited_but_unverifiable")

    cited_repo_paths_cache: Dict[str, List[str]] = {}
    for citation in verdict.citations:
        downgraded = _verify_citation_repo_membership(
            citation, all_domain_repos, verdict
        )
        if downgraded is not None:
            return downgraded

        if citation.repo_alias not in cited_repo_paths_cache:
            cited_repo_paths_cache[citation.repo_alias] = _resolve_repo_paths(
                [citation.repo_alias], repo_path_resolver
            )
        repo_paths = cited_repo_paths_cache[citation.repo_alias]

        if not repo_paths:
            logger.warning(
                "Verification: no paths for cited repo %r", citation.repo_alias
            )
            return _downgrade_verdict(verdict, "claude_cited_but_unverifiable")

        for repo_path in repo_paths:
            downgraded = _verify_citation_exists_in_file(
                citation, repo_path, verdict, effective_timeout
            )
            if downgraded is not None:
                return downgraded

        downgraded = _verify_source_side_reference(
            citation, source_paths, verdict, effective_timeout
        )
        if downgraded is not None:
            return downgraded

    return verdict
