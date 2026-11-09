"""
BIDIRECTIONAL_MISMATCH audit verdict parser (Story #912).

Extracted for MESSI Rule 6 compliance. Single responsibility: parse raw
Claude output into EdgeAuditVerdict.

Module-level definitions (exhaustive list):
  logger               -- standard Python logger
  VALID_VERDICTS       -- frozenset of accepted VERDICT values
  VALID_EVIDENCE_TYPES -- frozenset of accepted EVIDENCE_TYPE values
  _CITATION_RE         -- compiled citation regex
  CitationLine         -- frozen dataclass for one parsed citation
  EdgeAuditVerdict     -- frozen dataclass for a complete parse result
  _make_invalid_verdict -- construct an unparseable sentinel verdict
  _parse_citations     -- parse raw citation lines into (valid, dropped)
  parse_audit_verdict  -- NEVER-raises public entry point
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_VERDICTS: frozenset = frozenset({"CONFIRMED", "REFUTED", "INCONCLUSIVE"})
VALID_EVIDENCE_TYPES: frozenset = frozenset(
    {"code", "service", "contract", "config", "none"}
)

_CITATION_RE = re.compile(
    r"^\s*-\s+(?P<repo>[A-Za-z0-9_./-]+):(?P<file>[^\s:]+):(?P<line>[^\s]+)\s+(?P<symbol>\S.*)$"
)


@dataclass(frozen=True)
class CitationLine:
    """One parsed citation from the Claude output."""

    repo_alias: str
    file_path: str
    line_or_range: str
    symbol_or_token: str


@dataclass(frozen=True)
class EdgeAuditVerdict:
    """Result of parsing one Claude audit response."""

    verdict: str
    evidence_type: str
    citations: tuple  # tuple[CitationLine, ...]
    reasoning: str
    action: str
    dropped_citation_lines: tuple  # tuple[str, ...]


def _make_invalid_verdict(reason: str) -> EdgeAuditVerdict:
    """Construct an INCONCLUSIVE sentinel verdict for any parse failure."""
    return EdgeAuditVerdict(
        verdict="INCONCLUSIVE",
        evidence_type="none",
        citations=(),
        reasoning=reason,
        action="claude_output_unparseable",
        dropped_citation_lines=(),
    )


def _parse_citations(
    lines: List[str],
) -> Tuple[Tuple[CitationLine, ...], Tuple[str, ...]]:
    """Parse raw citation lines into (valid_citations, dropped_lines).

    Lines matching '-' prefix but failing the full regex go into dropped.
    Non-empty lines without a leading '-' are also added to dropped so
    no citation-block content is silently discarded.
    Empty lines are ignored (separator rows between citations).
    """
    valid: List[CitationLine] = []
    dropped: List[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue  # blank separator — intentionally ignored
        if not stripped.startswith("-"):
            # Non-empty line without '-' prefix inside CITATIONS block.
            # Surface it in dropped so callers can diagnose unexpected content.
            logger.warning(
                "_parse_citations: non-citation line in CITATIONS block: %r", stripped
            )
            dropped.append(stripped)
            continue
        m = _CITATION_RE.match(raw)
        if m:
            valid.append(
                CitationLine(
                    repo_alias=m.group("repo"),
                    file_path=m.group("file"),
                    line_or_range=m.group("line"),
                    symbol_or_token=m.group("symbol").strip(),
                )
            )
        else:
            dropped.append(stripped)
    return tuple(valid), tuple(dropped)


def parse_audit_verdict(raw_response: Any) -> EdgeAuditVerdict:
    """Parse Claude's raw output into an EdgeAuditVerdict. NEVER raises."""
    try:
        if not isinstance(raw_response, str):
            reason = f"raw_response is not a str: {type(raw_response).__name__}"
            logger.warning("parse_audit_verdict: %s", reason)
            return _make_invalid_verdict(reason)
        verdict: Optional[str] = None
        evidence_type: Optional[str] = None
        reasoning_parts: List[str] = []
        citation_lines: List[str] = []
        in_citations = False
        for line in raw_response.splitlines():
            if line.startswith("VERDICT:"):
                verdict = line[len("VERDICT:") :].strip()
                in_citations = False
            elif line.startswith("EVIDENCE_TYPE:"):
                evidence_type = line[len("EVIDENCE_TYPE:") :].strip()
                in_citations = False
            elif line.startswith("CITATIONS:"):
                in_citations = True
            elif line.startswith("REASONING:"):
                in_citations = False
                reasoning_parts.append(line[len("REASONING:") :].strip())
            elif in_citations:
                citation_lines.append(line)
            elif reasoning_parts:
                reasoning_parts.append(line.strip())
        if verdict is None:
            logger.warning("parse_audit_verdict: missing VERDICT line")
            return _make_invalid_verdict("missing VERDICT line")
        if evidence_type is None:
            logger.warning("parse_audit_verdict: missing EVIDENCE_TYPE line")
            return _make_invalid_verdict("missing EVIDENCE_TYPE line")
        if verdict not in VALID_VERDICTS:
            logger.warning("parse_audit_verdict: invalid VERDICT %r", verdict)
            return _make_invalid_verdict(f"invalid VERDICT: {verdict!r}")
        if evidence_type not in VALID_EVIDENCE_TYPES:
            logger.warning(
                "parse_audit_verdict: invalid EVIDENCE_TYPE %r", evidence_type
            )
            return _make_invalid_verdict(f"invalid EVIDENCE_TYPE: {evidence_type!r}")
        valid_cits, dropped = _parse_citations(citation_lines)
        reasoning = " ".join(p for p in reasoning_parts if p)
        if verdict == "CONFIRMED" and not valid_cits:
            logger.warning(
                "parse_audit_verdict: CONFIRMED with no valid citations — downgrading to INCONCLUSIVE"
            )
            return EdgeAuditVerdict(
                verdict="INCONCLUSIVE",
                evidence_type=evidence_type,
                citations=(),
                reasoning=reasoning,
                action="claude_output_unparseable",
                dropped_citation_lines=dropped,
            )
        action = (
            "auto_backfilled"
            if verdict == "CONFIRMED"
            else (
                "claude_refuted_pending_operator_approval"
                if verdict == "REFUTED"
                else "inconclusive_manual_review"
            )
        )
        return EdgeAuditVerdict(
            verdict=verdict,
            evidence_type=evidence_type,
            citations=valid_cits,
            reasoning=reasoning,
            action=action,
            dropped_citation_lines=dropped,
        )
    except (TypeError, AttributeError, ValueError) as exc:
        logger.warning("parse_audit_verdict: unexpected error: %s", exc)
        return _make_invalid_verdict(f"unexpected error: {exc}")
