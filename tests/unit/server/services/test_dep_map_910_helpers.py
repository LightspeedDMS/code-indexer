"""
Story #910 test infrastructure: repair helpers and pure-function unit tests.

Provides run_repair_and_read and extract_body_bytes used by AC1-AC5 test files.
Also contains unit tests for _body_byte_offset, _emit_repos_lines, and
_reemit_frontmatter_from_domain_info that pin their behavior before extraction
to dep_map_repair_phase37.py (Finding #3 of codex review).

Imports shared delimiter constants from test_dep_map_910_builders to avoid
defining them in multiple places.
"""

from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from tests.unit.server.services.test_dep_map_910_builders import (
    _CLOSING_DELIM,
    _CLOSING_DELIM_LEN,
    _OPENING_DELIM_LEN,
)

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry


def extract_body_bytes(raw_bytes: bytes) -> bytes:
    """Return bytes starting immediately after the closing --- line.

    Uses named constants from test_dep_map_910_builders to avoid magic numbers.
    """
    idx = raw_bytes.find(_CLOSING_DELIM, _OPENING_DELIM_LEN)
    if idx == -1:
        return b""
    return raw_bytes[idx + _CLOSING_DELIM_LEN :]


def run_repair_and_read(
    output_dir: Path,
    executor: "DepMapRepairExecutor",
    anomaly: "AnomalyEntry",
    stem: str,
) -> Tuple[str, List[str]]:
    """Run _repair_malformed_yaml and return (repaired_content, errors).

    Eliminates repeated repair + file-read boilerplate in AC field tests.
    """
    fixed: List[str] = []
    errors: List[str] = []
    executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)
    content = (output_dir / f"{stem}.md").read_text(encoding="utf-8")
    return content, errors


# ---------------------------------------------------------------------------
# Unit tests for pure static helpers — pins behavior before extraction (Finding #3)
# ---------------------------------------------------------------------------


class TestBodyByteOffset:
    """_body_byte_offset: returns exact byte position after (close_idx+1)th newline."""

    def test_returns_exact_offset_16_when_close_idx_is_2(self):
        """close_idx=2 means skip 3 newlines; exact offset is 16 in this fixture.

        raw = b'---\\nname: x\\n---\\nbody\\n'
        Newlines at indices: 3, 11, 15. Target = close_idx+1 = 3.
        Third newline is at index 15, so result = 15+1 = 16.
        """
        raw = b"---\nname: x\n---\nbody\n"
        result = DepMapRepairExecutor._body_byte_offset(raw, 2)
        assert result == 16, f"Expected exact offset 16, got {result}"
        assert raw[result:] == b"body\n", f"Suffix sanity: {raw[result:]!r}"

    def test_returns_len_when_not_enough_newlines(self):
        """Returns len(raw_bytes) when there are fewer newlines than target count."""
        raw = b"---\nname: x\n"
        result = DepMapRepairExecutor._body_byte_offset(raw, 5)
        assert result == len(raw), f"Expected {len(raw)}, got {result}"


class TestEmitReposLines:
    """_emit_repos_lines: returns exact YAML block for participating_repos."""

    def test_empty_list_returns_inline_empty(self):
        """Empty repos list returns exactly ['participating_repos: []']."""
        result = DepMapRepairExecutor._emit_repos_lines([])
        assert result == ["participating_repos: []"], f"Got: {result}"

    def test_nonempty_list_returns_exact_block_form(self):
        """Non-empty repos returns exact list: header + one indented entry per repo."""
        result = DepMapRepairExecutor._emit_repos_lines(["repo-a", "repo-b"])
        assert result == [
            "participating_repos:",
            "  - repo-a",
            "  - repo-b",
        ], f"Got: {result}"


class TestReemitFrontmatterFromDomainInfo:
    """_reemit_frontmatter_from_domain_info: exact full output with all fields replaced."""

    def test_all_three_fields_replaced_exact_output(self):
        """Full rewritten content matches exact expected string — spec for extraction."""
        content = (
            "---\n"
            "name: old-name\n"
            "last_analyzed: 2020-01-01\n"
            "participating_repos:\n"
            "  - old-repo\n"
            "---\n"
            "body content\n"
        )
        domain_info = {
            "name": "new-name",
            "last_analyzed": "2024-06-01T12:00:00",
            "participating_repos": ["repo-x", "repo-y"],
        }
        bounds = DepMapRepairExecutor._locate_frontmatter_bounds(content)
        result = DepMapRepairExecutor._reemit_frontmatter_from_domain_info(
            content, bounds, domain_info
        )
        expected = (
            "---\n"
            "name: new-name\n"
            "last_analyzed: 2024-06-01T12:00:00\n"
            "participating_repos:\n"
            "  - repo-x\n"
            "  - repo-y\n"
            "---\n"
            "body content\n"
        )
        assert result == expected, (
            f"Full rewritten content mismatch.\nExpected:\n{expected!r}\nGot:\n{result!r}"
        )
