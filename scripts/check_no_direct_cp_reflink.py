#!/usr/bin/env python3
"""Story #1034 AC15: prevent re-orphaning of CloneBackend abstraction.

Scans production code for direct subprocess.run([..."cp"...reflink...]) calls.
Excludes the abstraction itself (clone_backend.py, snapshot_manager.py) and
the intentional CLI fallback path (golden_repo_manager.py).
Exits 1 if any unauthorized direct cp+reflink call is found.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD_PATHS = [
    ROOT / "src" / "code_indexer" / "server",
    ROOT / "src" / "code_indexer" / "global_repos",
]
# Allowlist: files that are permitted to contain direct cp+reflink calls.
# clone_backend.py and snapshot_manager.py ARE the abstraction — they own the cp call.
# golden_repo_manager.py contains an intentional CLI fallback (no snapshot_manager injected).
ALLOWLIST = {
    ROOT / "src" / "code_indexer" / "server" / "storage" / "shared" / "clone_backend.py",
    ROOT / "src" / "code_indexer" / "server" / "storage" / "shared" / "snapshot_manager.py",
    ROOT / "src" / "code_indexer" / "server" / "repositories" / "golden_repo_manager.py",
}

pattern = re.compile(r'subprocess\.run\(.*?["\']cp["\'].*?reflink', re.DOTALL)
violations = []
for prod_path in PROD_PATHS:
    for py in prod_path.rglob("*.py"):
        if py in ALLOWLIST:
            continue
        text = py.read_text()
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            violations.append(f"{py}:{line}")

if violations:
    print(
        "Anti-Orphan violations (Story #1034 AC15): "
        "direct subprocess.run with cp+reflink found in production code:",
        file=sys.stderr,
    )
    for v in violations:
        print(f"  {v}", file=sys.stderr)
    print(
        "Use VersionedSnapshotManager.create_snapshot() or "
        "CloneBackend.create_clone_at_path() instead.",
        file=sys.stderr,
    )
    sys.exit(1)
print("Anti-Orphan check: PASS (zero direct cp+reflink in production code)")
sys.exit(0)
