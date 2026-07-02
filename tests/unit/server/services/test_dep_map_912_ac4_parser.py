"""
Story #912 AC4: parse_audit_verdict tests.

Verifies that parse_audit_verdict:
- Parses happy-path CONFIRMED/REFUTED/INCONCLUSIVE responses correctly
- Never raises (any parse failure -> _make_unparseable_verdict)
- Validates VERDICT and EVIDENCE_TYPE against allowed sets
- Downgrades CONFIRMED with no valid citations to INCONCLUSIVE
- Puts malformed citation lines in dropped_citation_lines
- Returns action=auto_backfilled for CONFIRMED with valid citations
- Returns action=claude_refuted_pending_operator_approval for REFUTED
- Returns action=inconclusive_manual_review for INCONCLUSIVE
- Returns action=claude_output_unparseable for any parse failure
"""

import pytest

# Import under test (will fail until module is created)
from code_indexer.server.services.dep_map_repair_bidirectional import (
    CitationLine,
    EdgeAuditVerdict,
    parse_audit_verdict,
)


# ---------------------------------------------------------------------------
# Happy-path fixtures
# ---------------------------------------------------------------------------

_CONFIRMED_RESPONSE = """\
VERDICT: CONFIRMED
EVIDENCE_TYPE: code
CITATIONS:
  - billing-repo:src/charge.py:10 PaymentRequest
  - billing-repo:src/charge.py:25 PaymentClient
REASONING: The billing-repo imports PaymentRequest from payment-gateway on line 10.
"""

_REFUTED_RESPONSE = """\
VERDICT: REFUTED
EVIDENCE_TYPE: none
CITATIONS:
REASONING: Searched billing-repo for any reference to payment-gateway symbols. Found nothing.
"""

_INCONCLUSIVE_RESPONSE = """\
VERDICT: INCONCLUSIVE
EVIDENCE_TYPE: service
CITATIONS:
  - billing-repo:config/services.yaml:5 payment-gateway-url
REASONING: Found a URL reference but cannot confirm it is the correct target without more context.
"""


def test_parse_confirmed_verdict():
    """Happy path: CONFIRMED with valid citations returns action=auto_backfilled."""
    result = parse_audit_verdict(_CONFIRMED_RESPONSE)
    assert isinstance(result, EdgeAuditVerdict)
    assert result.verdict == "CONFIRMED"
    assert result.evidence_type == "code"
    assert result.action == "auto_backfilled"
    assert len(result.citations) == 2
    assert result.dropped_citation_lines == ()


def test_parse_confirmed_citations_are_citationline():
    """CONFIRMED citations are CitationLine frozen dataclass instances."""
    result = parse_audit_verdict(_CONFIRMED_RESPONSE)
    c0 = result.citations[0]
    assert isinstance(c0, CitationLine)
    assert c0.repo_alias == "billing-repo"
    assert c0.file_path == "src/charge.py"
    assert c0.line_or_range == "10"
    assert c0.symbol_or_token == "PaymentRequest"


def test_parse_refuted_verdict():
    """Happy path: REFUTED with no citations returns correct action."""
    result = parse_audit_verdict(_REFUTED_RESPONSE)
    assert result.verdict == "REFUTED"
    assert result.evidence_type == "none"
    assert result.action == "claude_refuted_pending_operator_approval"
    assert result.citations == ()
    assert result.dropped_citation_lines == ()


def test_parse_inconclusive_verdict():
    """Happy path: INCONCLUSIVE with partial citations returns correct action."""
    result = parse_audit_verdict(_INCONCLUSIVE_RESPONSE)
    assert result.verdict == "INCONCLUSIVE"
    assert result.evidence_type == "service"
    assert result.action == "inconclusive_manual_review"
    assert len(result.citations) == 1


def test_parse_never_raises_on_empty_input():
    """Empty string must not raise — returns unparseable verdict."""
    result = parse_audit_verdict("")
    assert isinstance(result, EdgeAuditVerdict)
    assert result.action == "claude_output_unparseable"


def test_parse_never_raises_on_garbage_input():
    """Completely garbled input must not raise."""
    result = parse_audit_verdict("!!! not a verdict at all !!!")
    assert isinstance(result, EdgeAuditVerdict)
    assert result.action == "claude_output_unparseable"


def test_parse_never_raises_on_none_input():
    """None input must not raise — tests the 'never raises' contract.

    The type: ignore below is required because the test deliberately passes
    a non-str value to exercise the error boundary and verify the function
    never raises regardless of input type.
    """
    result = parse_audit_verdict(None)  # type: ignore[arg-type]  # deliberate type violation to test never-raises contract
    assert isinstance(result, EdgeAuditVerdict)
    assert result.action == "claude_output_unparseable"


def test_invalid_verdict_returns_unparseable():
    """Unknown VERDICT value triggers unparseable, not a hard error."""
    bad = "VERDICT: MAYBE\nEVIDENCE_TYPE: code\nCITATIONS:\nREASONING: whatever\n"
    result = parse_audit_verdict(bad)
    assert result.action == "claude_output_unparseable"


def test_invalid_evidence_type_returns_unparseable():
    """Unknown EVIDENCE_TYPE value triggers unparseable."""
    bad = "VERDICT: CONFIRMED\nEVIDENCE_TYPE: gossip\nCITATIONS:\n  - r:f:1 s\nREASONING: ok\n"
    result = parse_audit_verdict(bad)
    assert result.action == "claude_output_unparseable"


def test_confirmed_with_no_citations_downgrades_to_inconclusive():
    """CONFIRMED + zero valid citations -> INCONCLUSIVE + claude_output_unparseable."""
    no_cit = "VERDICT: CONFIRMED\nEVIDENCE_TYPE: code\nCITATIONS:\nREASONING: ok\n"
    result = parse_audit_verdict(no_cit)
    assert result.verdict == "INCONCLUSIVE"
    assert result.action == "claude_output_unparseable"


def test_malformed_citation_goes_to_dropped():
    """Citation lines that start with '- ' but fail full regex go to dropped_citation_lines."""
    malformed = (
        "VERDICT: REFUTED\n"
        "EVIDENCE_TYPE: none\n"
        "CITATIONS:\n"
        "  - this line has no colon structure\n"
        "REASONING: nothing found\n"
    )
    result = parse_audit_verdict(malformed)
    assert len(result.dropped_citation_lines) == 1
    assert "this line has no colon structure" in result.dropped_citation_lines[0]


def test_mixed_valid_and_malformed_citations():
    """Valid citations parse; malformed ones (matching '- ' prefix but failing full regex) go to dropped_citation_lines."""
    mixed = (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: code\n"
        "CITATIONS:\n"
        "  - good-repo:src/foo.py:42 MySymbol\n"
        "  - BAD LINE NO STRUCTURE\n"
        "  - good-repo:src/bar.py:10 OtherSymbol\n"
        "REASONING: evidence found\n"
    )
    result = parse_audit_verdict(mixed)
    # "  - BAD LINE NO STRUCTURE" starts with '- ' after leading whitespace strip,
    # so it IS attempted as a citation but fails the <repo>:<file>:<line> <symbol> regex
    # and lands in dropped_citation_lines.
    assert len(result.citations) == 2
    assert len(result.dropped_citation_lines) == 1
    assert "BAD LINE NO STRUCTURE" in result.dropped_citation_lines[0]
    assert result.action == "auto_backfilled"


def test_citation_regex_requires_colon_separated_fields():
    """A line matching '- ' prefix but missing file/line colons goes to dropped."""
    malformed_cit = (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: code\n"
        "CITATIONS:\n"
        "  - repo-only-no-file-or-line symbol\n"
        "REASONING: something\n"
    )
    result = parse_audit_verdict(malformed_cit)
    # repo-only-no-file-or-line doesn't match <repo>:<file>:<line> <symbol>
    assert len(result.dropped_citation_lines) == 1
    # With no valid citations, CONFIRMED downgrades to INCONCLUSIVE
    assert result.verdict == "INCONCLUSIVE"


def test_reasoning_is_captured():
    """REASONING line content is captured in the verdict."""
    result = parse_audit_verdict(_CONFIRMED_RESPONSE)
    assert "billing-repo imports PaymentRequest" in result.reasoning


def test_missing_verdict_line_is_unparseable():
    """Missing VERDICT line -> unparseable."""
    no_verdict = "EVIDENCE_TYPE: code\nCITATIONS:\nREASONING: ok\n"
    result = parse_audit_verdict(no_verdict)
    assert result.action == "claude_output_unparseable"


def test_missing_evidence_type_is_unparseable():
    """Missing EVIDENCE_TYPE line -> unparseable."""
    no_et = "VERDICT: CONFIRMED\nCITATIONS:\n  - r:f:1 s\nREASONING: ok\n"
    result = parse_audit_verdict(no_et)
    assert result.action == "claude_output_unparseable"


def test_edge_case_verdict_with_trailing_whitespace():
    """VERDICT with trailing spaces is accepted."""
    response = (
        "VERDICT: REFUTED   \nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: nothing\n"
    )
    result = parse_audit_verdict(response)
    assert result.verdict == "REFUTED"


def test_edge_case_evidence_type_contract():
    """EVIDENCE_TYPE: contract is valid."""
    response = (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: contract\n"
        "CITATIONS:\n"
        "  - repo-x:schema/order.proto:1 OrderMessage\n"
        "REASONING: shared proto file found\n"
    )
    result = parse_audit_verdict(response)
    assert result.evidence_type == "contract"
    assert result.action == "auto_backfilled"


def test_edge_case_evidence_type_config():
    """EVIDENCE_TYPE: config is valid."""
    response = (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: config\n"
        "CITATIONS:\n"
        "  - repo-x:config/env.yaml:3 PAYMENT_API_KEY\n"
        "REASONING: shared config key found\n"
    )
    result = parse_audit_verdict(response)
    assert result.evidence_type == "config"


def test_verdict_fields_are_frozen():
    """EdgeAuditVerdict is frozen (immutable).

    The type: ignore below is intentional: the assignment is statically
    invalid on a frozen dataclass, and mypy/pyright flag it with [misc].
    We suppress the check here because the point of the test IS to attempt
    an invalid mutation and confirm a runtime error is raised.
    """
    result = parse_audit_verdict(_REFUTED_RESPONSE)
    with pytest.raises((AttributeError, TypeError)):
        result.verdict = "CONFIRMED"  # type: ignore[misc]  # deliberate frozen-dataclass mutation to verify immutability


def test_citations_is_tuple():
    """citations is always a tuple, never a list."""
    result = parse_audit_verdict(_CONFIRMED_RESPONSE)
    assert isinstance(result.citations, tuple)


def test_dropped_citation_lines_is_tuple():
    """dropped_citation_lines is always a tuple, never a list."""
    result = parse_audit_verdict(_REFUTED_RESPONSE)
    assert isinstance(result.dropped_citation_lines, tuple)
