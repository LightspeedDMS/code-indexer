"""
Verify that every tool in a snapshot manifest has a slim_description field.

Story #988: MCP Tool Surface Compression - infrastructure script.

Reads a manifest JSON file produced by snapshot_tool_doc_corpus.py.
For each tool, reads its .md file and checks for a non-empty
slim_description field in the YAML frontmatter.

Exit 0 if 100% of tools have slim_description.
Exit 1 if any tools are missing slim_description.

Prints summary:
  "N/M tools have slim_description. [PASS|FAIL]"
  Plus a list of missing tool names when FAIL.

Usage:
    PYTHONPATH=./src python3 scripts/mcp/verify_slim_coverage.py [manifest_path]

Default manifest path: reports/mcp/slim_manifest_pre_s3.json
"""

import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import yaml

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST = _REPO_ROOT / "reports" / "mcp" / "slim_manifest_pre_s3.json"


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


def verify_coverage(manifest_path: Optional[Path] = None) -> int:
    """
    Check slim_description coverage across all tools in the manifest.

    Args:
        manifest_path: Path to the manifest JSON file. Defaults to
            reports/mcp/slim_manifest_pre_s3.json.

    Returns:
        0 if all tools have slim_description, 1 otherwise.
    """
    path = manifest_path if manifest_path is not None else _DEFAULT_MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tools = manifest.get("tools", [])
    total = len(tools)

    if total == 0:
        print("0/0 tools have slim_description. PASS")
        return 0

    missing: List[str] = []
    for tool in tools:
        tool_path = Path(tool["path"])
        if not tool_path.exists():
            missing.append(tool["name"])
            continue
        raw = tool_path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(raw)
        slim = fm.get("slim_description") if fm else None
        if not slim:
            missing.append(tool["name"])

    covered = total - len(missing)
    if missing:
        print(f"{covered}/{total} tools have slim_description. FAIL")
        for name in missing:
            print(f"  MISSING: {name}")
        return 1

    print(f"{covered}/{total} tools have slim_description. PASS")
    return 0


def main(manifest_path: Optional[Path] = None) -> None:
    """Run slim_description coverage verification and exit with result code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # Allow manifest path override from argv
    if manifest_path is None and len(sys.argv) > 1:
        manifest_path = Path(sys.argv[1])
    exit_code = verify_coverage(manifest_path=manifest_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
