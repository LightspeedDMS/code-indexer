"""
Snapshot the MCP tool doc corpus to a manifest JSON.

Story #988: MCP Tool Surface Compression - infrastructure script.

Walks tool_docs/**/*.md (excluding _ prefixed files), reads YAML frontmatter
for each file, and outputs a manifest JSON to stdout.

Output format:
  {
    "branch_cut_sha": "<git HEAD short sha>",
    "tool_count": N,
    "tools": [
      {"name": "tool_name", "category": "cat",
       "path": "src/code_indexer/server/mcp/tool_docs/cat/tool_name.md"}
    ]
  }

Tools are sorted alphabetically by name.

Usage:
    PYTHONPATH=./src python3 scripts/mcp/snapshot_tool_doc_corpus.py
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Ensure src/ is on the path when invoked directly
_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

logger = logging.getLogger(__name__)

# Default tool_docs location (relative to repo root)
_TOOL_DOCS_DIR = _REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"

# Timeout for the git rev-parse subprocess used to capture HEAD SHA.
GIT_COMMAND_TIMEOUT_SECONDS = 5


def _get_git_sha() -> str:
    """Return the current git HEAD SHA (short), or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Failed to retrieve git SHA: %s", exc)
    return "unknown"


def _parse_frontmatter(raw: str) -> Optional[Dict[str, Any]]:
    """
    Parse YAML frontmatter from a markdown file.

    Splits on '---' delimiter. Returns parsed dict or None if parsing fails.
    """
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        return yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        logger.warning("YAML parse error: %s", exc)
        return None


def snapshot_corpus(
    tool_docs_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Walk tool_docs/**/*.md and build a manifest of all tools.

    Excludes files whose names start with '_'.
    Tools are sorted alphabetically by name.

    Args:
        tool_docs_root: Directory to walk. Defaults to the project's tool_docs/.

    Returns:
        Dict with keys:
        - branch_cut_sha: short git SHA of HEAD (or 'unknown')
        - tool_count: number of tools found
        - tools: list of dicts with name, category, path
    """
    root = tool_docs_root if tool_docs_root is not None else _TOOL_DOCS_DIR
    tools: List[Dict[str, str]] = []

    for md_file in sorted(root.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        raw = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(raw)
        if not fm or "name" not in fm:
            continue
        tools.append(
            {
                "name": fm["name"],
                "category": fm.get("category", ""),
                "path": str(md_file),
            }
        )

    tools.sort(key=lambda t: t["name"])

    return {
        "branch_cut_sha": _get_git_sha(),
        "tool_count": len(tools),
        "tools": tools,
    }


def main(tool_docs_root: Optional[Path] = None) -> None:
    """Print the tool doc corpus manifest as JSON to stdout."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    manifest = snapshot_corpus(tool_docs_root=tool_docs_root)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
