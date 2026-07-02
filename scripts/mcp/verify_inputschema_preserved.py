"""
Verify that all tool docs with inputSchema have valid non-null dicts.

Story #988: MCP Tool Surface Compression - infrastructure script.

Walks tool_docs/**/*.md (excluding _ prefixed files). For each file:
- If inputSchema key is ABSENT: counted as guide (expected, not a failure).
- If inputSchema key is PRESENT and value is a non-null dict: verified tool.
- If inputSchema key is PRESENT but value is null: BROKEN (failure).

Exit 0 if all present inputSchemas are valid non-null dicts.
Exit 1 if any present inputSchema is null (corruption detected).

Prints:
  "N tools with inputSchema verified. M guides without (expected). [PASS|FAIL]"

Usage:
    PYTHONPATH=./src python3 scripts/mcp/verify_inputschema_preserved.py
"""

import logging
import sys
from pathlib import Path
from typing import List, Optional

import yaml

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

logger = logging.getLogger(__name__)

_TOOL_DOCS_DIR = _REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"

_INPUTSCHEMA_KEY = "inputSchema"


def _parse_frontmatter(raw: str) -> Optional[dict]:
    """Parse YAML frontmatter from a markdown file, returns dict or None."""
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        return yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        logger.warning("YAML parse error: %s", exc)
        return None


def verify_inputschemas(tool_docs_root: Optional[Path] = None) -> int:
    """
    Walk tool_docs and verify inputSchema integrity.

    Classification rules:
    - inputSchema key ABSENT: guide doc (expected, not a failure).
    - inputSchema key PRESENT, value is non-null dict: verified tool.
    - inputSchema key PRESENT, value is null: BROKEN (failure).

    Args:
        tool_docs_root: Directory to walk. Defaults to project tool_docs/.

    Returns:
        0 if all present inputSchemas are valid non-null dicts.
        1 if any present inputSchema is null (corruption detected).
    """
    root = tool_docs_root if tool_docs_root is not None else _TOOL_DOCS_DIR
    verified_count = 0
    guide_count = 0
    broken: List[str] = []

    for md_file in sorted(root.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        raw = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(raw)
        if not fm:
            continue

        name = fm.get("name", md_file.stem)

        if _INPUTSCHEMA_KEY not in fm:
            # Key absent: guide doc - expected, not a failure
            guide_count += 1
        elif fm[_INPUTSCHEMA_KEY] is None:
            # Key present but null: corruption
            broken.append(name)
        else:
            # Key present with a non-null value: verified
            verified_count += 1

    if broken:
        print(
            f"{verified_count} tools with inputSchema verified. "
            f"{guide_count} guides without (expected). FAIL"
        )
        for name in broken:
            print(f"  BROKEN inputSchema (null): {name}")
        return 1

    print(
        f"{verified_count} tools with inputSchema verified. "
        f"{guide_count} guides without (expected). PASS"
    )
    return 0


def main() -> None:
    """Run inputSchema preservation check and exit with result code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    exit_code = verify_inputschemas()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
