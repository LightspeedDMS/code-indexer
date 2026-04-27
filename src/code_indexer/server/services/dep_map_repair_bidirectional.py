"""
BIDIRECTIONAL_MISMATCH audit pipeline (Story #912).

Public entry point for verifying and optionally backfilling cross-domain
dependency edges that are claimed as outgoing by one side but not confirmed
by the target side's incoming table.

Module-level definitions (exhaustive list):
  logger                           -- standard Python logger
  _DEFAULT_CLAUDE_SHELL_TIMEOUT_S  -- int constant, default shell timeout (270 s)
  _DEFAULT_CLAUDE_OUTER_TIMEOUT_S  -- int constant, default outer timeout (330 s)
  _OUTGOING_MIN_COLS_FULL          -- int constant, 6 (need dep_type/why/evidence)
  _OUTGOING_SECTION                -- str constant for outgoing section heading
  _OUTGOING_HEADER_SENTINEL        -- str constant for outgoing header row sentinel
  _COL_OUT_TARGET_DOMAIN           -- int constant, column index 2 in outgoing table
  _COL_OUT_DEP_TYPE                -- int constant, column index 3 in outgoing table
  _COL_OUT_WHY                     -- int constant, column index 4 in outgoing table
  _COL_OUT_EVIDENCE                -- int constant, column index 5 in outgoing table
  _BIDI_MESSAGE_RE                 -- compiled regex to extract src/tgt from anomaly message
  _PROMPT_TEMPLATE_PATH            -- Path constant pointing to bidirectional_mismatch_audit.md
  CitationLine                     -- re-exported from parser module
  EdgeAuditVerdict                 -- re-exported from parser module
  parse_audit_verdict              -- re-exported from parser module
  run_verification_gate            -- re-exported from verify module
  _int_env                         -- read positive integer from env var
  _claude_shell_timeout            -- resolve shell timeout from env or default
  _claude_outer_timeout            -- resolve outer timeout from env or default
  _resolved_claude_timeouts        -- return (shell, outer) ensuring outer > shell
  _extract_src_tgt_from_message    -- parse (src, tgt) from anomaly message string
  _load_outgoing_row_for_target    -- find (dep_type, why, evidence) in source outgoing table
  _render_audit_prompt             -- load template_path and format with edge context
  _invoke_claude_audit             -- delegate to injected invoke_fn
  _append_bidi_journal_entry       -- write one entry to RepairJournal; logs on error
  (backfill cluster imported from dep_map_repair_bidirectional_backfill):
  _is_safe_domain_name             -- no path-traversal chars in domain name
  _get_domain_repos                -- return participating_repos list for a domain name
  backfill_target_mirror_row       -- full RMW on target .md under domain lock
  _build_failure_verdict           -- construct INCONCLUSIVE verdict for Claude failure
  _handle_claude_failure           -- journal failure entry and append to errors
  _run_backfill_if_confirmed       -- conditionally backfill and collect file_writes
  _audit_one_impl                  -- inner audit steps after src/tgt extraction
  audit_one_bidirectional_mismatch -- per-anomaly orchestration, never raises
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.server.services.dep_map_repair_bidirectional_backfill import (
    _is_safe_domain_name,
    _get_domain_repos,
    backfill_target_mirror_row,
)
from code_indexer.server.services.dep_map_repair_bidirectional_parser import (
    CitationLine,
    EdgeAuditVerdict,
    _make_invalid_verdict,
    parse_audit_verdict,
)
from code_indexer.server.services.dep_map_repair_bidirectional_verify import (
    run_verification_gate,
)
from code_indexer.server.services.dep_map_parser_tables import (
    iter_table_rows,
)
from code_indexer.server.services.dep_map_repair_phase37 import (
    Action,
    JournalEntry,
    RepairJournal,
)

__all__ = [
    "CitationLine",
    "EdgeAuditVerdict",
    "parse_audit_verdict",
    "run_verification_gate",
    "backfill_target_mirror_row",
    "audit_one_bidirectional_mismatch",
]

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_SHELL_TIMEOUT_S: int = 270
_DEFAULT_CLAUDE_OUTER_TIMEOUT_S: int = 330

_OUTGOING_MIN_COLS_FULL: int = 6
_OUTGOING_SECTION: str = "### Outgoing Dependencies"
_OUTGOING_HEADER_SENTINEL: str = "This Repo"

_COL_OUT_TARGET_DOMAIN: int = 2
_COL_OUT_DEP_TYPE: int = 3
_COL_OUT_WHY: int = 4
_COL_OUT_EVIDENCE: int = 5

_BIDI_MESSAGE_RE = re.compile(r"bidirectional mismatch:\s*(\S+)→(\S+)\s+declared")

_PROMPT_TEMPLATE_PATH: Path = (
    Path(__file__).parent.parent / "mcp" / "prompts" / "bidirectional_mismatch_audit.md"
)


def _int_env(name: str, default: int) -> int:
    """Read a positive integer from an environment variable; return default on invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "_int_env: %s=%r not a valid integer; using default %d", name, raw, default
        )
        return default
    if value <= 0:
        logger.warning(
            "_int_env: %s=%r not positive; using default %d", name, raw, default
        )
        return default
    return value


def _claude_shell_timeout() -> int:
    """Resolve shell timeout from CIDX_BIDI_CLAUDE_SHELL_TIMEOUT env or default."""
    return _int_env("CIDX_BIDI_CLAUDE_SHELL_TIMEOUT", _DEFAULT_CLAUDE_SHELL_TIMEOUT_S)


def _claude_outer_timeout() -> int:
    """Resolve outer timeout from CIDX_BIDI_CLAUDE_OUTER_TIMEOUT env or default."""
    return _int_env("CIDX_BIDI_CLAUDE_OUTER_TIMEOUT", _DEFAULT_CLAUDE_OUTER_TIMEOUT_S)


def _resolved_claude_timeouts() -> Tuple[int, int]:
    """Return (shell_timeout, outer_timeout) with outer strictly greater than shell.

    Falls back to defaults when the resolved outer is not > shell.
    """
    shell = _claude_shell_timeout()
    outer = _claude_outer_timeout()
    if outer <= shell:
        logger.warning(
            "_resolved_claude_timeouts: outer (%d) <= shell (%d); resetting to defaults",
            outer,
            shell,
        )
        return _DEFAULT_CLAUDE_SHELL_TIMEOUT_S, _DEFAULT_CLAUDE_OUTER_TIMEOUT_S
    return shell, outer


def _extract_src_tgt_from_message(message: str) -> Optional[Tuple[str, str]]:
    """Return (src, tgt) from a BIDIRECTIONAL_MISMATCH anomaly message.

    Expected: "bidirectional mismatch: {src}→{tgt} declared outgoing by ..."
    Returns None when the message does not match.
    """
    m = _BIDI_MESSAGE_RE.search(message)
    if not m:
        logger.warning("_extract_src_tgt_from_message: no match in %r", message)
        return None
    return m.group(1), m.group(2)


def _load_outgoing_row_for_target(
    source_md_path: Path,
    target_domain: str,
) -> Tuple[str, str, str]:
    """Return (dep_type, why, evidence) from first outgoing row targeting target_domain.

    Returns ("", "", "") when file cannot be read or no matching row is found.
    """
    try:
        content = source_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "_load_outgoing_row_for_target: cannot read %s: %s", source_md_path, exc
        )
        return "", "", ""
    try:
        for cells in iter_table_rows(
            content,
            _OUTGOING_SECTION,
            _OUTGOING_MIN_COLS_FULL,
            _OUTGOING_HEADER_SENTINEL,
        ):
            if cells[_COL_OUT_TARGET_DOMAIN] == target_domain:
                return (
                    cells[_COL_OUT_DEP_TYPE],
                    cells[_COL_OUT_WHY],
                    cells[_COL_OUT_EVIDENCE],
                )
    except ValueError as exc:
        logger.warning(
            "_load_outgoing_row_for_target: error scanning %s: %s", source_md_path, exc
        )
    return "", "", ""


def _render_audit_prompt(
    template_path: Path,
    source_domain: str,
    target_domain: str,
    source_repos: List[str],
    target_repos: List[str],
    dep_type: str,
    why: str,
    evidence: str,
) -> Optional[str]:
    """Load template_path and format it with the given edge context.

    Returns None when template_path cannot be read.
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("_render_audit_prompt: cannot read %s: %s", template_path, exc)
        return None
    return template.format(
        source_domain=source_domain,
        source_repos=", ".join(source_repos) if source_repos else "(none)",
        target_domain=target_domain,
        target_repos=", ".join(target_repos) if target_repos else "(none)",
        dep_type=dep_type or "(unknown)",
        claimed_why=why or "(not specified)",
        claimed_evidence=evidence or "(not specified)",
    )


def _invoke_claude_audit(
    source_path: str,
    prompt: str,
    shell_timeout: int,
    outer_timeout: int,
    invoke_fn: Callable[[str, str, int, int], Tuple[bool, str]],
) -> Tuple[bool, str]:
    """Delegate to invoke_fn. Returns (success, raw_output). Logs on exception."""
    try:
        return invoke_fn(source_path, prompt, shell_timeout, outer_timeout)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as exc:
        logger.warning("_invoke_claude_audit: invoke_fn raised: %s", exc)
        return False, str(exc)


def _append_bidi_journal_entry(
    journal: Optional[RepairJournal],
    source_domain: str,
    target_domain: str,
    source_repos: List[str],
    target_repos: List[str],
    verdict_obj: EdgeAuditVerdict,
    file_writes: List[str],
    raw_response: str,
    errors: List[str],
    effective_mode: str = "enabled",
) -> None:
    """Append one BIDIRECTIONAL_MISMATCH entry to RepairJournal. Logs on failure.

    No-op when journal is None (dry_run=True path).
    effective_mode: label written to journal entry ('enabled' or 'dry_run').
    """
    if journal is None:
        return
    try:
        citations_list = [
            f"{c.repo_alias}:{c.file_path}:{c.line_or_range} {c.symbol_or_token}"
            for c in verdict_obj.citations
        ]
        entry = JournalEntry(
            anomaly_type="BIDIRECTIONAL_MISMATCH",
            source_domain=source_domain,
            target_domain=target_domain,
            source_repos=list(source_repos),
            target_repos=list(target_repos),
            verdict=verdict_obj.verdict,
            action=Action(verdict_obj.action).value,
            citations=citations_list,
            file_writes=[{"path": fw} for fw in file_writes],
            claude_response_raw=raw_response,
            effective_mode=effective_mode,
        )
        journal.append(entry)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        logger.warning("_append_bidi_journal_entry: failed: %s", exc)
        errors.append(f"_append_bidi_journal_entry: {exc}")


def _build_failure_verdict(raw_response: str) -> EdgeAuditVerdict:
    """Construct an INCONCLUSIVE verdict for a Claude invocation failure."""
    return _make_invalid_verdict(f"claude invocation failed: {raw_response}")


def _handle_claude_failure(
    journal: Optional[RepairJournal],
    src: str,
    tgt: str,
    source_repos: List[str],
    target_repos: List[str],
    raw_response: str,
    errors: List[str],
    effective_mode: str = "enabled",
) -> None:
    """Journal a Claude invocation failure and append an error message."""
    failure_verdict = _build_failure_verdict(raw_response)
    errors.append(
        f"audit_one_bidirectional_mismatch: Claude invocation failed for {src}→{tgt}: {raw_response}"
    )
    _append_bidi_journal_entry(
        journal,
        src,
        tgt,
        source_repos,
        target_repos,
        failure_verdict,
        [],
        raw_response,
        errors,
        effective_mode=effective_mode,
    )


def _run_backfill_if_confirmed(
    output_dir: Path,
    verdict_obj: EdgeAuditVerdict,
    src: str,
    tgt: str,
    source_repos: List[str],
    dep_type: str,
    why: str,
    evidence: str,
    fixed: List[str],
    errors: List[str],
    dry_run: bool = False,
    would_be_writes: Optional[List] = None,
) -> List[str]:
    """Backfill target .md when verdict is CONFIRMED. Returns file_writes list.

    dry_run=True: delegates write-gating to backfill_target_mirror_row; no actual write.
    """
    if verdict_obj.verdict != "CONFIRMED":
        return []
    target_md = output_dir / f"{tgt}.md"
    ok = backfill_target_mirror_row(
        output_dir,
        tgt,
        src,
        source_repos,
        dep_type,
        why,
        evidence,
        errors,
        dry_run=dry_run,
        would_be_writes=would_be_writes,
    )
    if ok:
        fixed.append(f"BIDIRECTIONAL_MISMATCH: backfilled {src}→{tgt} in {tgt}.md")
        return [str(target_md)]
    return []


def _audit_one_impl(
    output_dir: Path,
    anomaly: Any,
    domains_json: List[Dict[str, Any]],
    invoke_claude_fn: Callable[[str, str, int, int], Tuple[bool, str]],
    repo_path_resolver: Callable[[str], str],
    journal: RepairJournal,
    fixed: List[str],
    errors: List[str],
    prompt_template_path: Optional[Path] = None,
    dry_run: bool = False,
    effective_mode: str = "enabled",
    would_be_writes: Optional[List] = None,
    extra_verdict_counts: Optional[Dict[str, Any]] = None,
    extra_action_counts: Optional[Dict[str, Any]] = None,
) -> None:
    """Inner audit steps — separated so audit_one_bidirectional_mismatch wraps it."""
    pair = _extract_src_tgt_from_message(anomaly.message)
    if pair is None:
        errors.append(
            f"audit_one_bidirectional_mismatch: cannot parse message: {anomaly.message!r}"
        )
        return
    src, tgt = pair
    if not _is_safe_domain_name(src) or not _is_safe_domain_name(tgt):
        errors.append(
            f"audit_one_bidirectional_mismatch: unsafe domain in ({src!r}, {tgt!r})"
        )
        return
    source_repos = _get_domain_repos(src, domains_json)
    target_repos = _get_domain_repos(tgt, domains_json)
    source_md = output_dir / f"{src}.md"
    dep_type, why, evidence = _load_outgoing_row_for_target(source_md, tgt)
    effective_template = (
        prompt_template_path
        if prompt_template_path is not None
        else _PROMPT_TEMPLATE_PATH
    )
    prompt = _render_audit_prompt(
        effective_template,
        src,
        tgt,
        source_repos,
        target_repos,
        dep_type,
        why,
        evidence,
    )
    if prompt is None:
        errors.append(
            f"audit_one_bidirectional_mismatch: cannot render prompt for {src}→{tgt}"
        )
        return
    shell_timeout, outer_timeout = _resolved_claude_timeouts()
    success, raw_response = _invoke_claude_audit(
        str(output_dir), prompt, shell_timeout, outer_timeout, invoke_claude_fn
    )
    if not success:
        _handle_claude_failure(
            journal,
            src,
            tgt,
            source_repos,
            target_repos,
            raw_response,
            errors,
            effective_mode=effective_mode,
        )
        return
    verdict_obj = parse_audit_verdict(raw_response)
    verdict_obj = run_verification_gate(
        verdict_obj, src, tgt, domains_json, repo_path_resolver
    )
    if dry_run:
        if extra_verdict_counts is not None:
            extra_verdict_counts[verdict_obj.verdict] = (
                extra_verdict_counts.get(verdict_obj.verdict, 0) + 1
            )
        if extra_action_counts is not None:
            extra_action_counts[verdict_obj.action] = (
                extra_action_counts.get(verdict_obj.action, 0) + 1
            )
    file_writes = _run_backfill_if_confirmed(
        output_dir,
        verdict_obj,
        src,
        tgt,
        source_repos,
        dep_type,
        why,
        evidence,
        fixed,
        errors,
        dry_run=dry_run,
        would_be_writes=would_be_writes,
    )
    _append_bidi_journal_entry(
        journal,
        src,
        tgt,
        source_repos,
        target_repos,
        verdict_obj,
        file_writes,
        raw_response,
        errors,
        effective_mode=effective_mode,
    )


def audit_one_bidirectional_mismatch(
    output_dir: Path,
    anomaly: Any,
    domains_json: List[Dict[str, Any]],
    invoke_claude_fn: Callable[[str, str, int, int], Tuple[bool, str]],
    repo_path_resolver: Callable[[str], str],
    journal: RepairJournal,
    fixed: List[str],
    errors: List[str],
    prompt_template_path: Optional[Path] = None,
    dry_run: bool = False,
    effective_mode: str = "enabled",
    would_be_writes: Optional[List] = None,
    extra_verdict_counts: Optional[Dict[str, Any]] = None,
    extra_action_counts: Optional[Dict[str, Any]] = None,
) -> None:
    """Audit one BIDIRECTIONAL_MISMATCH anomaly. Never raises.

    AC2: single Claude invocation per anomaly via injected invoke_claude_fn.
    AC6: citation file existence verified by run_verification_gate.
    AC7: source-side reverse check by run_verification_gate.
    AC8: REFUTED -> journal only, no file write.
    AC9: INCONCLUSIVE -> journal only, no file write.
    AC10: timeout -> verification_timeout action in journal.
    AC11: repo membership verified by run_verification_gate.
    AC12: prompt_template_path overrides _PROMPT_TEMPLATE_PATH when provided.
    dry_run=True: Claude+ripgrep verification still runs; backfill write is skipped.
    effective_mode: label written to journal entries ('enabled' or 'dry_run').
    extra_verdict_counts / extra_action_counts: mutated in-place (when not None) to
    surface INCONCLUSIVE/REFUTED verdicts that never produce fixed[] entries.
    """
    try:
        _audit_one_impl(
            output_dir,
            anomaly,
            domains_json,
            invoke_claude_fn,
            repo_path_resolver,
            journal,
            fixed,
            errors,
            prompt_template_path=prompt_template_path,
            dry_run=dry_run,
            effective_mode=effective_mode,
            would_be_writes=would_be_writes,
            extra_verdict_counts=extra_verdict_counts,
            extra_action_counts=extra_action_counts,
        )
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        logger.warning("audit_one_bidirectional_mismatch: unexpected error: %s", exc)
        errors.append(f"audit_one_bidirectional_mismatch: {exc}")
