"""
Story #910 AC5: Body bytes preserved — mixed line endings and complex body content.

Verifies that after a surgical frontmatter re-emit, the body bytes (everything
after the closing --- delimiter) are byte-identical to the pre-repair state.
Tests use read_bytes() directly, not text round-trips.

TestAC5BodyBytesPreserved (2 methods):
  test_mixed_line_endings_body_bytes_identical
    Body contains \r\n and \n mixed; bytes must survive unchanged.
  test_code_fence_and_table_body_bytes_identical
    Body contains triple-backtick code fence and pipe-table; bytes unchanged.
"""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_yaml_anomaly,
)
from tests.unit.server.services.test_dep_map_910_helpers import extract_body_bytes

if TYPE_CHECKING:
    pass

_DOMAIN_INFO = {
    "name": "domain-q",
    "last_analyzed": "2024-06-01T12:00:00",
    "participating_repos": ["repo-p"],
}

# Malformed frontmatter block (missing colon on last_analyzed) — same for both tests.
_MALFORMED_FM = (
    b"---\n"
    b"name: wrong-name\n"
    b"last_analyzed 2024-01-15\n"
    b"participating_repos:\n"
    b"  - repo-old\n"
    b"---"
)


def _write_raw_file(output_dir: Path, stem: str, body_bytes: bytes) -> Path:
    """Write a domain .md file as raw bytes (frontmatter + body_bytes)."""
    path = output_dir / f"{stem}.md"
    path.write_bytes(_MALFORMED_FM + body_bytes)
    return path


@pytest.fixture()
def ac5_base(tmp_path) -> Path:
    """Return output_dir with _domains.json set up for domain-q."""
    # cast: tmp_path is Any in older pytest stubs; narrow to Path before composition.
    output_dir = cast(Path, tmp_path) / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    make_domains_json(output_dir, [_DOMAIN_INFO])
    return output_dir


class TestAC5BodyBytesPreserved:
    """AC5: body bytes byte-identical after repair regardless of content complexity."""

    def test_mixed_line_endings_body_bytes_identical(self, ac5_base):
        """Body with mixed \\r\\n and \\n line endings is byte-identical after repair."""
        output_dir = ac5_base
        # Body with intentionally mixed line endings
        mixed_body = (
            b"\r\n"
            b"## Overview\r\n"
            b"\r\n"
            b"Line with CRLF ending.\r\n"
            b"Line with LF ending.\n"
            b"Another CRLF line.\r\n"
        )
        md_path = _write_raw_file(output_dir, "domain-q", mixed_body)
        original_body_bytes = extract_body_bytes(md_path.read_bytes())

        executor = make_executor_910()
        anomaly = make_malformed_yaml_anomaly("domain-q.md")
        executor._repair_malformed_yaml(output_dir, anomaly, [], [])

        repaired_body_bytes = extract_body_bytes(md_path.read_bytes())
        assert repaired_body_bytes == original_body_bytes, (
            f"Mixed-line-ending body bytes changed.\n"
            f"Original: {original_body_bytes!r}\n"
            f"Repaired: {repaired_body_bytes!r}"
        )

    def test_code_fence_and_table_body_bytes_identical(self, ac5_base):
        """Body with code fence, pipe table, and escaped backslashes is unchanged."""
        output_dir = ac5_base
        # Body with triple-backtick code fence, pipe table, trailing \r\n
        complex_body = (
            b"\n"
            b"## Overview\n"
            b"\n"
            b"```python\n"
            b"def foo():\n"
            b"    return 'bar'\n"
            b"```\n"
            b"\n"
            b"| Column A | Column B | Notes |\n"
            b"|----------|----------|-------|\n"
            b"| val1     | val2     | a\\\\b |\n"
            b"\n"
            b"Trailing line.\r\n"
        )
        md_path = _write_raw_file(output_dir, "domain-q", complex_body)
        original_body_bytes = extract_body_bytes(md_path.read_bytes())

        executor = make_executor_910()
        anomaly = make_malformed_yaml_anomaly("domain-q.md")
        executor._repair_malformed_yaml(output_dir, anomaly, [], [])

        repaired_body_bytes = extract_body_bytes(md_path.read_bytes())
        assert repaired_body_bytes == original_body_bytes, (
            f"Code-fence/table body bytes changed.\n"
            f"Original: {original_body_bytes!r}\n"
            f"Repaired: {repaired_body_bytes!r}"
        )
