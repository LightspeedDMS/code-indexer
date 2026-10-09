"""Content seam, phase 5: every filesystem access the server can reach, inventoried.

The repository mount comes off the cidx pods only when nothing the server runs
still touches the repository volume directly. "Are we done" must be answerable
by reading a file, not by searching the code again -- so the access sites live
in a committed inventory, ``content_seam_inventory.json``, one row per
(module, function, operation) with a call count and a classification.

This test keeps the inventory TRUE:

* the scan and the inventory must match exactly. A new access -- or one more
  call in a function already listed -- fails the build until a row says what it
  is. That is the fail-closed part: nothing is skipped because the guard cannot
  tell what a path points at; every syntactic access is listed.
* a row may be ``unclassified`` only if it was present in the baseline. Rows
  added after that must be classified when they are added.
* the rendered summary (``docs/content-seam-inventory.md``) must match the
  inventory, so the checklist people read is the one the build enforces.

Scope is what the server can REACH: the first-party import closure of
``server/`` and ``global_repos/`` -- the same fence as the git seam guard --
plus ``cli.py``, because the server spawns ``cidx index`` as a child process on
the same pod, which the import closure alone would never see.

Regenerate after a change (classifications are kept, new rows are added
unclassified, vanished rows are dropped):

    PYTHONPATH=src python3 tests/unit/server/test_content_seam_inventory.py --write

Classes (see the phase-5 doc in docs/content-seam-*.md for the plan):

    unclassified        not yet looked at (baseline rows only)
    repo-read           reads repository content: migrate to the content seam
    repo-write          writes or mutates the repository tree: migrate to workspace-mcp
    cidx-state-in-repo  cidx's own state stored inside the repository volume: relocate
    migrated            has a delegated path; this local access runs only when local
    local-only          runs only where a local checkout is by design (watch, CLI-only)
    cidx-state          cidx's own data on its own volume: stays
    not-repo            not repository data at all (config, logs, temp, system files)
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src" / "code_indexer"
_INVENTORY = Path(__file__).with_name("content_seam_inventory.json")
_SUMMARY = _ROOT / "docs" / "content-seam-inventory.md"

#: Seeds of the fence. Directories are taken whole; files singly.
_SEEDS = ("server", "global_repos", "cli.py")

CLASSES = (
    "unclassified",
    "repo-read",
    "repo-write",
    "cidx-state-in-repo",
    "migrated",
    "local-only",
    "cidx-state",
    "not-repo",
)
#: Classes that still block the mount from coming off.
OPEN_CLASSES = ("unclassified", "repo-read", "repo-write", "cidx-state-in-repo")

#: Module-level functions that touch the filesystem, by dotted name.
_FUNCTIONS = frozenset(
    {
        "open",
        "os.walk",
        "os.listdir",
        "os.scandir",
        "os.stat",
        "os.lstat",
        "os.path.exists",
        "os.path.isfile",
        "os.path.isdir",
        "os.path.islink",
        "os.path.getsize",
        "os.path.getmtime",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.makedirs",
        "os.mkdir",
        "os.rmdir",
        "os.symlink",
        "os.link",
        "os.utime",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "glob.glob",
        "glob.iglob",
    }
)
_SHUTIL = frozenset(
    {"copy", "copy2", "copyfile", "copytree", "rmtree", "move", "copymode", "copystat"}
)
#: pathlib methods. Matched by name on any receiver, so this over-matches on
#: purpose (fail closed); a non-path receiver is classified not-repo.
_METHODS = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "iterdir",
        "glob",
        "rglob",
        "is_symlink",
        "is_file",
        "is_dir",
        "mkdir",
        "unlink",
        "rmdir",
        "rename",
        "touch",
        "symlink_to",
        "hardlink_to",
        "lstat",
        "exists",
        "stat",
        "open",
        "resolve",
        "samefile",
        "chmod",
        "readlink",
    }
)

Key = Tuple[str, str, str]  # (module, function, op)


# ── the fence ───────────────────────────────────────────────────────────────


def _first_party_imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(_SRC).parent.parts
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = (
                    ".".join(base + tuple(node.module.split(".")))
                    if node.module
                    else ".".join(base)
                )
            elif node.module and node.module.startswith("code_indexer"):
                module = node.module[len("code_indexer") :].lstrip(".")
            else:
                continue
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("code_indexer."):
                    found.add(alias.name[len("code_indexer.") :])
    return {m.strip(".") for m in found if m.strip(".")}


def _module_path(module: str) -> Optional[Path]:
    base = _SRC.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def fenced_files() -> List[Path]:
    """Every first-party module the server pod can run, seeds plus imports."""
    stack: List[Path] = []
    for seed in _SEEDS:
        path = _SRC / seed
        stack.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    seen: Dict[str, Path] = {}
    while stack:
        path = stack.pop()
        rel = path.relative_to(_SRC).as_posix()
        if rel in seen:
            continue
        seen[rel] = path
        for module in _first_party_imports(path):
            nxt = _module_path(module)
            if nxt is not None and nxt.relative_to(_SRC).as_posix() not in seen:
                stack.append(nxt)
    return [seen[k] for k in sorted(seen)]


# ── the scan ────────────────────────────────────────────────────────────────


def _dotted(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def operation(call: ast.Call) -> Optional[str]:
    """The filesystem operation a call performs, or None if it is not one."""
    name = _dotted(call.func)
    if name in _FUNCTIONS:
        return name
    if name and name.startswith("shutil.") and name.split(".", 1)[1] in _SHUTIL:
        return name
    if not isinstance(call.func, ast.Attribute):
        return None
    if name and name.startswith(("os.", "shutil.", "glob.")):
        return None
    attr = call.func.attr
    # Path.replace(target) takes one argument; str.replace(old, new) two.
    if attr == "replace" and len(call.args) == 1 and not call.keywords:
        return ".replace"
    return f".{attr}" if attr in _METHODS else None


def scan_source(module: str, source: str) -> Counter:
    tree = ast.parse(source)
    found: Counter = Counter()
    scopes: List[str] = []

    class _Visitor(ast.NodeVisitor):
        def _scoped(self, node: ast.AST, name: str) -> None:
            scopes.append(name)
            self.generic_visit(node)
            scopes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scoped(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:
            op = operation(node)
            if op is not None:
                found[(module, ".".join(scopes) or "<module>", op)] += 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def scan() -> Counter:
    found: Counter = Counter()
    for path in fenced_files():
        rel = path.relative_to(_SRC).as_posix()
        found.update(scan_source(rel, path.read_text(encoding="utf-8")))
    return found


# ── the inventory ───────────────────────────────────────────────────────────


def load_inventory() -> Dict[Key, dict]:
    rows = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    return {(r["module"], r["function"], r["op"]): r for r in rows}


def merged_rows(
    found: Counter, existing: Dict[Key, dict], *, baseline: bool
) -> List[dict]:
    """The inventory for ``found``, keeping each surviving row's classification."""
    rows = []
    for key in sorted(found):
        old = existing.get(key, {})
        rows.append(
            {
                "module": key[0],
                "function": key[1],
                "op": key[2],
                "count": found[key],
                "class": old.get("class", "unclassified"),
                "baseline": old.get("baseline", baseline),
                "note": old.get("note", ""),
            }
        )
    return rows


def render_summary(rows: Iterable[dict]) -> str:
    rows = list(rows)
    by_class: Counter = Counter(r["class"] for r in rows)
    open_modules: Dict[str, Counter] = {}
    for r in rows:
        if r["class"] in OPEN_CLASSES:
            open_modules.setdefault(r["module"], Counter())[r["class"]] += 1
    lines = [
        "# Content seam inventory (generated -- do not edit)",
        "",
        "Every filesystem access the cidx server pod can reach, from",
        "`tests/unit/server/content_seam_inventory.json`. Regenerate with",
        "`PYTHONPATH=src python3 tests/unit/server/test_content_seam_inventory.py --write`.",
        "The repo mount can come off when no row is in an open class "
        f"({', '.join(OPEN_CLASSES)}).",
        "",
        "| Class | Rows |",
        "|---|---|",
    ]
    lines += [f"| {c} | {by_class.get(c, 0)} |" for c in CLASSES]
    lines += [
        "",
        "## Modules with open rows",
        "",
        "| Module | " + " | ".join(OPEN_CLASSES) + " |",
    ]
    lines += ["|---|" + "---|" * len(OPEN_CLASSES)]
    for module in sorted(open_modules):
        counts = open_modules[module]
        lines.append(
            f"| `{module}` | "
            + " | ".join(str(counts.get(c, 0)) for c in OPEN_CLASSES)
            + " |"
        )
    return "\n".join(lines) + "\n"


def write(*, baseline: bool = False) -> None:
    existing = load_inventory() if _INVENTORY.exists() else {}
    rows = merged_rows(scan(), existing, baseline=baseline)
    # One row per line: a change to the inventory diffs as the rows it touched.
    body = ",\n".join(json.dumps(r, sort_keys=False) for r in rows)
    _INVENTORY.write_text(f"[\n{body}\n]\n", encoding="utf-8")
    _SUMMARY.write_text(render_summary(rows), encoding="utf-8")


# ── the guard ───────────────────────────────────────────────────────────────


def test_every_access_the_server_can_reach_is_in_the_inventory() -> None:
    found = scan()
    inventory = load_inventory()
    missing = sorted(k for k in found if k not in inventory)
    stale = sorted(k for k in inventory if k not in found)
    changed = sorted(
        (k, inventory[k]["count"], found[k])
        for k in found
        if k in inventory and inventory[k]["count"] != found[k]
    )
    assert not (missing or stale or changed), (
        "the content-seam inventory is out of date -- regenerate it and classify "
        "any new row (see this module's docstring).\n"
        f"new: {missing[:20]}\nremoved: {stale[:20]}\ncount changed: {changed[:20]}"
    )


def test_every_row_has_a_known_class() -> None:
    bad = [
        (k, r["class"])
        for k, r in load_inventory().items()
        if r["class"] not in CLASSES
    ]
    assert not bad, f"unknown classes: {bad[:20]}"


def test_only_baseline_rows_may_be_unclassified() -> None:
    """New access must say what it is when it is added, not join the backlog."""
    late = [
        k
        for k, r in load_inventory().items()
        if r["class"] == "unclassified" and not r["baseline"]
    ]
    assert not late, f"classify these new rows: {late[:20]}"


def test_the_summary_matches_the_inventory() -> None:
    rows = sorted(
        load_inventory().values(), key=lambda r: (r["module"], r["function"], r["op"])
    )
    assert _SUMMARY.read_text(encoding="utf-8") == render_summary(rows), (
        "docs/content-seam-inventory.md is stale -- regenerate it"
    )


# ── the guard sees what it claims to ────────────────────────────────────────


class TestScannerSeesEveryShape:
    def _ops(self, source: str) -> Counter:
        return Counter({k[2]: v for k, v in scan_source("m.py", source).items()})

    def test_builtin_open_and_os_functions(self) -> None:
        src = "import os\ndef f(p):\n    open(p); os.walk(p); os.path.exists(p); os.replace(p, p)\n"
        assert set(self._ops(src)) == {
            "open",
            "os.walk",
            "os.path.exists",
            "os.replace",
        }

    def test_shutil_tree_operations(self) -> None:
        src = (
            "import shutil\ndef f(a, b):\n    shutil.rmtree(a); shutil.copytree(a, b)\n"
        )
        assert set(self._ops(src)) == {"shutil.rmtree", "shutil.copytree"}

    def test_pathlib_methods_on_any_receiver(self) -> None:
        src = "def f(p):\n    p.read_bytes(); p.parent.iterdir(); (p / 'x').exists()\n"
        assert set(self._ops(src)) == {".read_bytes", ".iterdir", ".exists"}

    def test_path_replace_is_seen_and_str_replace_is_not(self) -> None:
        src = "def f(p, s):\n    p.replace(p); s.replace('a', 'b')\n"
        assert self._ops(src) == Counter({".replace": 1})

    def test_unrelated_calls_are_not_listed(self) -> None:
        assert not self._ops("def f(d):\n    d.get('x'); print(d); len(d)\n")

    def test_rows_are_keyed_by_enclosing_function_and_counted(self) -> None:
        src = "class C:\n    def m(self, p):\n        p.exists(); p.exists()\n"
        assert scan_source("m.py", src) == Counter({("m.py", "C.m", ".exists"): 2})

    def test_the_fence_reaches_through_imports_and_includes_the_cli(self) -> None:
        fenced = {p.relative_to(_SRC).as_posix() for p in fenced_files()}
        assert "cli.py" in fenced, "the spawned `cidx index` child is out of scope"
        assert "indexing/file_finder.py" in fenced, "the import closure is not followed"
        assert "server/app.py" in fenced


if __name__ == "__main__":
    if "--write" in sys.argv:
        write(baseline="--baseline" in sys.argv)
        print(f"wrote {_INVENTORY} and {_SUMMARY}")
    else:
        print(__doc__)
