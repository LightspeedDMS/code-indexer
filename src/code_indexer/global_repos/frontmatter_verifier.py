"""
Post-backfill structural verification of cidx-meta lifecycle frontmatter.

Story #1067.

Given a cidx-meta ``<alias>.md`` path this module parses the YAML frontmatter
via ``yaml.safe_load`` and validates it against the enforced lifecycle contract
by **calling into** ``UnifiedResponseParser._validate`` and
``UnifiedResponseParser._validate_optional_sections`` — the SINGLE SOURCE OF
TRUTH.  The enum tables and required-key lists are NOT duplicated here.

Public API
----------
``verify_file(path)``   -> VerificationResult (single file)
``verify_batch(directory)`` -> BatchReport (all ``<alias>.md`` files in dir)

Both are non-raising: malformed YAML and missing frontmatter produce structured
FAIL results rather than exceptions.

Placement rationale
-------------------
Placed in ``global_repos/`` alongside ``unified_response_parser.py`` because:
- The files being verified are produced by the global_repos pipeline.
- The validator it calls (UnifiedResponseParser) lives here.
- No server/HTTP/DB concerns — a pure validation utility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from code_indexer.global_repos.unified_response_parser import (
    UnifiedResponseParser,
    UnifiedResponseParseError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """
    Structured pass/fail result for a single ``<alias>.md`` file.

    Attributes:
        passed:     True iff all contract rules are satisfied.
        violations: Human-readable list of violated rules (empty when passed).
    """

    passed: bool
    violations: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        if self.passed:
            return "PASS"
        return f"FAIL — {'; '.join(self.violations)}"


@dataclass
class BatchReport:
    """
    Summary produced by ``verify_batch()`` over a cidx-meta directory.

    Attributes:
        valid_count:   Number of files that passed verification.
        invalid_count: Number of files that failed verification.
        per_file:      Map of filename (stem) -> VerificationResult.
    """

    valid_count: int = 0
    invalid_count: int = 0
    per_file: Dict[str, VerificationResult] = field(default_factory=dict)

    @property
    def total_count(self) -> int:
        """Total number of .md files examined."""
        return self.valid_count + self.invalid_count

    def __str__(self) -> str:
        return (
            f"BatchReport: {self.valid_count} valid, "
            f"{self.invalid_count} invalid "
            f"(total {self.total_count})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter_and_body(
    content: str,
) -> tuple[Optional[dict], str, Optional[str]]:
    """
    Split content into (frontmatter_dict, body, error_message).

    Returns:
        (dict, body_str, None)          on success.
        (None, "",       error_message)  when frontmatter is absent or
                                         YAML is malformed.
    """
    if not content.startswith("---"):
        return None, "", "frontmatter does not parse: no opening '---' delimiter"

    # Find the closing delimiter (search starting after the opening "---")
    close_pos = content.find("---", 3)
    if close_pos == -1:
        return None, "", "frontmatter does not parse: no closing '---' delimiter"

    yaml_text = content[3:close_pos].strip()
    body = content[close_pos + 3 :]
    if body.startswith("\n"):
        body = body[1:]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return None, "", f"frontmatter does not parse: {exc}"

    if parsed is None:
        return None, body, "frontmatter does not parse: YAML block is empty"

    if not isinstance(parsed, dict):
        return (
            None,
            "",
            f"frontmatter does not parse: expected a YAML mapping, "
            f"got {type(parsed).__name__}",
        )

    return parsed, body, None


def _normalize_parser_error(msg: str) -> str:
    """
    Normalise a raw error string from ``UnifiedResponseParser._validate`` so
    that it contains the fully-qualified ``lifecycle.<key>`` path.

    The parser emits ``"missing required lifecycle field: '<key>'"`` without
    the ``lifecycle.`` prefix in the path (the word "lifecycle" appears in the
    prose but not as part of the dotted field path that tests assert).  We
    rewrite those messages to ``"lifecycle.<key>: missing required field"`` so
    callers can reliably check for ``"lifecycle.<key>"`` in the violation text.

    All other messages are returned unchanged.
    """
    import re as _re

    m = _re.match(r"^missing required lifecycle field: '([^']+)'$", msg)
    if m:
        return f"lifecycle.{m.group(1)}: missing required field"
    return msg


def _validate_frontmatter(fm: dict, body: str) -> List[str]:
    """
    Validate a parsed frontmatter dict + body against the lifecycle contract.

    Delegates all schema checks to UnifiedResponseParser so there is exactly
    one source of truth for enum tables and required-key lists.

    Args:
        fm:   Parsed YAML frontmatter dict.
        body: Markdown body text (everything after the closing ``---``).

    Returns:
        List of violation strings (empty = valid).
    """
    violations: List[str] = []

    # -- Delegate required-key + enum validation to the parser --
    # _validate() returns a list of human-readable error strings.
    # Normalise them so every lifecycle-field violation contains the
    # fully-qualified "lifecycle.<key>" path (for deterministic assertions).
    parser_errors = [
        _normalize_parser_error(e) for e in UnifiedResponseParser._validate(fm)
    ]
    violations.extend(parser_errors)

    # -- Validate optional sections (branching / ci / release) --
    # _validate_optional_sections() raises on the FIRST violation; we catch
    # each raise to collect a structured violation string without propagating.
    lifecycle = fm.get("lifecycle")
    if isinstance(lifecycle, dict) and not parser_errors:
        # Only validate optional sections when the required fields are clean
        # (avoids confusing double-errors when lifecycle is already broken).
        try:
            UnifiedResponseParser._validate_optional_sections(lifecycle, raw="")
        except UnifiedResponseParseError as exc:
            # Extract the violation messages from the exception.
            if exc.validation_errors:
                violations.extend(exc.validation_errors)
            else:
                violations.append(str(exc))

        # v4 branch_environment_map validation
        try:
            UnifiedResponseParser._validate_v4_fields(lifecycle, raw="")
        except UnifiedResponseParseError as exc:
            if exc.validation_errors:
                violations.extend(exc.validation_errors)
            else:
                violations.append(str(exc))

    # -- Body must be non-empty --
    if not body.strip():
        violations.append("body: markdown body (after closing ---) is empty")

    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_file(path: Path) -> VerificationResult:
    """
    Verify a single cidx-meta ``<alias>.md`` file.

    Reads the file, parses its YAML frontmatter, and validates it against
    the lifecycle contract enforced by ``UnifiedResponseParser``.

    Args:
        path: Absolute or relative path to an ``<alias>.md`` file.

    Returns:
        VerificationResult with ``passed=True`` and empty violations on
        success, or ``passed=False`` with specific violation messages on
        failure.

    This function NEVER raises — all errors are returned as structured
    violations.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return VerificationResult(
            passed=False,
            violations=[f"frontmatter does not parse: cannot read file: {exc}"],
        )

    fm, body, parse_error = _parse_frontmatter_and_body(content)
    if parse_error is not None:
        return VerificationResult(passed=False, violations=[parse_error])

    assert fm is not None  # guaranteed by parse_error being None
    violations = _validate_frontmatter(fm, body)
    return VerificationResult(passed=(len(violations) == 0), violations=violations)


def verify_batch(directory: Path) -> BatchReport:
    """
    Verify every ``<alias>.md`` file in *directory*.

    Files matching ``*.md`` that do NOT start with ``_`` are verified.
    Non-``.md`` files and ``_``-prefixed files (e.g., sentinel lock files)
    are silently skipped.

    Args:
        directory: Path to a cidx-meta directory (or any directory holding
                   ``<alias>.md`` lifecycle files).

    Returns:
        BatchReport with per-file results and aggregate counts.

    This function NEVER raises — one bad file never aborts the batch.
    """
    report = BatchReport()
    directory = Path(directory)

    for md_file in sorted(directory.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        alias = md_file.stem
        try:
            result = verify_file(md_file)
        except Exception as exc:  # belt-and-suspenders: verify_file should not raise
            logger.warning(
                "verify_batch: unexpected exception on %s: %s",
                md_file,
                exc,
            )
            result = VerificationResult(
                passed=False,
                violations=[f"unexpected error: {exc}"],
            )
        report.per_file[alias] = result
        if result.passed:
            report.valid_count += 1
        else:
            report.invalid_count += 1

    return report
