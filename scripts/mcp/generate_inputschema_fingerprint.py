"""
Generate SHA-256 fingerprints of inputSchema blocks across all tool docs.

Story #988: MCP Tool Surface Compression - infrastructure script.

Walks tool_docs/**/*.md (excluding _ prefixed files). For each file with
a non-null inputSchema, serializes the inputSchema dict to a canonical YAML
string (sort_keys=True, default_flow_style=False) and computes SHA-256.

Output JSON (to stdout):
  {"tool_name": "sha256hexdigest", ...}

With --compare <baseline.json>:
  Loads baseline, compares fingerprints, reports any drifted tools.
  Exit 1 on drift, exit 0 if identical.

Usage:
    PYTHONPATH=./src python3 scripts/mcp/generate_inputschema_fingerprint.py
    PYTHONPATH=./src python3 scripts/mcp/generate_inputschema_fingerprint.py \\
        --compare reports/mcp/fingerprint_baseline.json
"""

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _fingerprint_schema(schema: Any) -> str:
    """
    Compute a deterministic SHA-256 of an inputSchema dict.

    Serializes the schema to canonical YAML (sort_keys=True,
    default_flow_style=False) then hashes the UTF-8 bytes.
    """
    canonical = yaml.dump(schema, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_fingerprints(tool_docs_root: Optional[Path] = None) -> Dict[str, str]:
    """
    Generate SHA-256 fingerprints for all tool docs with inputSchema.

    Docs where inputSchema is absent (guides) are excluded from the output.
    Docs where inputSchema is null are also excluded (treated as corrupt).

    Args:
        tool_docs_root: Directory to walk. Defaults to project tool_docs/.

    Returns:
        Dict mapping tool name -> 64-char lowercase hex SHA-256 digest.
    """
    root = tool_docs_root if tool_docs_root is not None else _TOOL_DOCS_DIR
    result: Dict[str, str] = {}

    for md_file in sorted(root.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        raw = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(raw)
        if not fm:
            continue
        if _INPUTSCHEMA_KEY not in fm or fm[_INPUTSCHEMA_KEY] is None:
            continue
        name = fm.get("name", md_file.stem)
        result[name] = _fingerprint_schema(fm[_INPUTSCHEMA_KEY])

    return result


def compare_fingerprints(
    current: Dict[str, str],
    baseline_path: Path,
) -> int:
    """
    Compare current fingerprints against a baseline JSON file.

    Reports tools where the fingerprint has changed (drifted), tools
    added since baseline, and tools removed since baseline.

    Args:
        current: Dict of tool name -> sha256 from generate_fingerprints().
        baseline_path: Path to baseline JSON file.

    Returns:
        0 if current matches baseline exactly.
        1 if any drift detected.
    """
    baseline: Dict[str, str] = json.loads(baseline_path.read_text(encoding="utf-8"))

    all_names = sorted(set(current.keys()) | set(baseline.keys()))
    drifted: List[str] = []
    added: List[str] = []
    removed: List[str] = []

    for name in all_names:
        if name in baseline and name not in current:
            removed.append(name)
        elif name not in baseline and name in current:
            added.append(name)
        elif current[name] != baseline[name]:
            drifted.append(name)

    if not drifted and not added and not removed:
        print("Fingerprints match baseline. PASS")
        return 0

    print("Fingerprint drift detected. FAIL")
    for name in drifted:
        print(f"  DRIFTED: {name}")
        print(f"    baseline: {baseline[name]}")
        print(f"    current:  {current[name]}")
    for name in added:
        print(f"  ADDED (new tool): {name}")
    for name in removed:
        print(f"  REMOVED (missing from current): {name}")
    return 1


def main(
    tool_docs_root: Optional[Path] = None,
    args: Optional[List[str]] = None,
) -> None:
    """
    Generate fingerprints and optionally compare against a baseline.

    Args:
        tool_docs_root: Override for tool_docs directory (used in tests).
        args: Argument list (defaults to sys.argv[1:]).
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    argv = args if args is not None else sys.argv[1:]

    fingerprints = generate_fingerprints(tool_docs_root=tool_docs_root)

    # Check for --compare flag
    compare_path: Optional[Path] = None
    for i, arg in enumerate(argv):
        if arg == "--compare" and i + 1 < len(argv):
            compare_path = Path(argv[i + 1])
            break

    if compare_path is not None:
        exit_code = compare_fingerprints(
            current=fingerprints, baseline_path=compare_path
        )
        sys.exit(exit_code)
    else:
        print(json.dumps(fingerprints, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
