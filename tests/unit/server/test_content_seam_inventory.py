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

Local GIT is a repository access too, and the one the mount-less reconcile bug
(MR !155) went through: ``GitTopologyService`` asked local git whether the
directory was a repository, got "no" on a pod with no ``.git``, and reconcile
deleted every chunk. So the scan also lists, per function:

* ``git <verb>`` -- a raw spawn of git (argv read by the git seam guard's own
  reader, ``test_git_seam_no_raw_subprocess``); ``git <unreadable argv>`` when
  a spawn's argv cannot be read and so might be git;
* ``run_git_command <verb>`` -- the git chokepoint. It delegates MUTATIONS to
  git-mcp when an invoker is installed; a READ still runs local git;
* ``git.Repo`` -- GitPython, which runs local git;
* ``<op> .git`` -- any filesystem op on a ``.git`` path: a repository probe.

Two rules then hold the indexing and query paths (``_INDEX_QUERY_PATHS``) shut:

* **Ratchet.** An open row there carries a ``ceiling``: the count it had when
  the ratchet was taken. A new open row (no ceiling) or a count above its
  ceiling fails. The way past is the seam, not a reclassification: ask the
  content backend, the pass manifest or the git backend, and the row becomes
  ``migrated``. Regenerating lowers a ceiling to the current count, never
  raises one.
* **Evidence.** A ``migrated`` row there must show its seam: the function
  names a backend or manifest (``_SEAM_EVIDENCE``), or the row's ``gate``
  names the function in the same module that does and is the only way in.
  Deleting the delegated branch of a migrated function -- reverting
  ``GitTopologyService.is_git_available`` to local-only git -- fails here even
  though the call count is unchanged.

Regenerate after a change (classifications are kept, new rows are added
unclassified, vanished rows are dropped):

    PYTHONPATH=src python3 -m tests.unit.server.test_content_seam_inventory --write

Classes (see the phase-5 doc in docs/content-seam-*.md for the plan):

    unclassified        not yet looked at (baseline rows only)
    repo-read           reads repository content: migrate to the content seam
    repo-write          writes or mutates the repository tree: migrate to workspace-mcp
    cidx-state-in-repo  cidx's own state stored inside the repository volume: relocate
    migrated            has a delegated path; this local access runs only when local
    refused             a delegated server refuses the feature, loudly, before this
                        access (``refuse_when_delegated``): it runs only when local
    local-only          runs only where a local checkout is by design (watch, CLI-only)
    cidx-state          cidx's own data on its own volume: stays
    not-repo            not repository data at all (config, logs, temp, system files)
"""

from __future__ import annotations

import ast
import functools
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

from tests.unit.server.test_git_seam_no_raw_subprocess import (
    _call_kind,
    _direct_import_aliases,
    _git_argv,
    _verb,
)

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
    "refused",
    "local-only",
    "cidx-state",
    "not-repo",
)
#: Classes that still block the mount from coming off.
OPEN_CLASSES = ("unclassified", "repo-read", "repo-write", "cidx-state-in-repo")

#: Open REPOSITORY rows: the pod reading or writing the repository itself.
REPOSITORY_CLASSES = ("unclassified", "repo-read", "repo-write")

#: Work streams that own their open repository rows elsewhere: activated repos
#: (feat/activated-repos-content-seam) and the SCIP generator (scip/).
#: Everywhere else the count of open repository rows is ZERO --
#: test_no_open_repository_rows_outside_the_excluded_areas. A row joins an
#: area ONLY by its explicit ``area`` field, with a note saying why: never by
#: a name that merely contains "activat" or "scip".
EXCLUDED_AREAS = ("activation", "scip")

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
        "os.open",
        "os.access",
        "os.readlink",
        "os.statvfs",
        "os.chdir",
        "os.path.realpath",
        "os.path.lexists",
        "os.path.ismount",
        "os.path.getctime",
        "os.path.getatime",
        "os.path.samefile",
        "io.open",
        "codecs.open",
        "tarfile.open",
        "zipfile.ZipFile",
        # A temp file can be created INSIDE a repository (atomic writes pass
        # dir=), so tempfile creators are inventoried like any other create.
        "tempfile.mkstemp",
        "tempfile.mkdtemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "tempfile.TemporaryFile",
        "tempfile.SpooledTemporaryFile",
        # GitPython: opening a Repo runs local git against a local .git.
        "git.Repo",
        "git.Repo.init",
        "git.Repo.clone_from",
    }
)

#: Calls into the same modules that do NOT touch a path: string-only path
#: arithmetic, process and environment queries, in-memory streams, and
#: operations on an already-open descriptor (the ``os.open`` that produced it is
#: what is inventoried). Every call into GOVERNED_MODULES must be in this set or
#: in _FUNCTIONS -- test_every_module_call_is_accounted_for keeps the scanner
#: closed instead of trusting a hand-picked list.
KNOWN_NOT_FS = frozenset(
    {
        "os.path.join",
        "os.path.basename",
        "os.path.dirname",
        "os.path.split",
        "os.path.splitext",
        "os.path.normpath",
        "os.path.relpath",
        "os.path.isabs",
        "os.path.abspath",
        "os.path.expanduser",
        "os.path.expandvars",
        "os.path.commonpath",
        "os.path.commonprefix",
        "os.fspath",
        "os.getcwd",
        "os.getenv",
        "os.putenv",
        "os.getpid",
        "os.getppid",
        "os.getpgid",
        "os.getuid",
        "os.geteuid",
        "os.getgid",
        "os.getegid",
        "os.getlogin",
        "os.getloadavg",
        "os.cpu_count",
        "os.uname",
        "os.urandom",
        "os.strerror",
        "os.umask",
        "os.kill",
        "os.killpg",
        "os.execv",
        "os.execvp",
        "os.pipe",
        "os.close",
        "os.fsync",
        "os.fdatasync",
        "os.read",
        "os.write",
        "os.fstat",
        "os.fchmod",
        "os.fchown",
        "os.fdopen",
        "os.dup",
        "os.dup2",
        "os.set_blocking",
        "os.waitpid",
        "os.environ.get",
        "os.environ.copy",
        "os.environ.setdefault",
        "os.environ.pop",
        "os.environ.items",
        "os.environ.keys",
        "os.environ.update",
        "io.BytesIO",
        "io.StringIO",
        "io.TextIOWrapper",
        "io.BufferedReader",
        "codecs.getincrementaldecoder",
        "codecs.lookup",
        "shutil.which",
        "shutil.ignore_patterns",
        "shutil.get_terminal_size",
        "shutil.copyfileobj",
        "tempfile.gettempdir",
        "tempfile.gettempdirb",
    }
)

#: Modules whose every call must be classified one way or the other.
GOVERNED_MODULES = (
    "os",
    "os.path",
    "os.environ",
    "shutil",
    "io",
    "glob",
    "tarfile",
    "zipfile",
    "codecs",
    "tempfile",
)
_SHUTIL = frozenset(
    {
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "rmtree",
        "move",
        "copymode",
        "copystat",
        "disk_usage",
        "chown",
        "make_archive",
        "unpack_archive",
    }
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
        "is_mount",
        "owner",
        "group",
        "walk",
        "copy",
        "move",
    }
)

#: The git chokepoint (utils/git_runner.py). Mutations through it go to
#: git-mcp when an invoker is installed; reads run local git.
_GIT_CHOKEPOINTS = frozenset({"run_git_command", "run_git_command_with_retry"})
#: Op prefixes that run git on this machine, and the suffix of a .git probe.
_LOCAL_GIT_OPS = ("git ", "git.", "run_git_command")
_GIT_DIR_SUFFIX = " .git"

#: Where indexing (``cidx index``, reconcile, refresh) and query run. Open rows
#: here are ratcheted and migrated rows must show their seam -- see the
#: module docstring. Prefixes of paths relative to src/code_indexer.
_INDEX_QUERY_PATHS = (
    "indexing/",
    "services/",
    "storage/",
    "search/",
    "server/query/",
)

#: An identifier that shows a function asks a seam: a content or git backend,
#: the pass manifest, prefetched content, or the multimodal path's
#: ``image_source`` (an ImagePrefetcher over the content backend).
_SEAM_EVIDENCE = re.compile(
    r"backend|manifest|delegat|content_source|image_source|prefetch", re.I
)
#: An identifier that shows a function REFUSES under delegated: the refusal
#: helpers (``refuse_when_delegated``, ``delegated_refusal``,
#: ``refuse_analysis`` ...) or the refusal error itself. Naming a backend is
#: not refusing.
_REFUSAL_EVIDENCE = re.compile(r"refus|DelegatedRepositoryUnsupported", re.I)

#: Programs that read or write a directory tree: a spawn of one is a
#: repository access like any filesystem call (``spawn <program>``), whatever
#: path its argv names. Other programs (installers, systemctl, the ``cidx``
#: child whose own accesses are scanned through ``cli.py``) are not listed.
_TREE_TOOLS = frozenset(
    {"rg", "grep", "find", "cp", "mv", "rm", "rsync", "tar", "du", "ls", "cat"}
)

Key = Tuple[str, str, str]  # (module, function, op)


def in_index_query_path(module: str) -> bool:
    return module.startswith(_INDEX_QUERY_PATHS)


def area_of(row: dict) -> Optional[str]:
    """The excluded work stream (EXCLUDED_AREAS) that owns *row*, or None."""
    return str(row["area"]) if row.get("area") else None


def is_local_git(op: str) -> bool:
    """A row that runs git here or probes a .git directory."""
    return op.startswith(_LOCAL_GIT_OPS) or op.endswith(_GIT_DIR_SUFFIX)


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


@functools.lru_cache(maxsize=None)
def fenced_files() -> Tuple[Path, ...]:
    """Every first-party module the server pod can run, seeds plus imports.

    Cached: three guards walk the same fence, and the closure is a third of
    their runtime.
    """
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
        # Importing a.b.c RUNS a/__init__.py and a/b/__init__.py too, so every
        # ancestor package's __init__ is in the fence -- and whatever it imports.
        for parent in path.relative_to(_SRC).parents:
            init = _SRC / parent / "__init__.py"
            if parent != Path(".") and init.is_file():
                stack.append(init)
        for module in _first_party_imports(path):
            nxt = _module_path(module)
            if nxt is not None and nxt.relative_to(_SRC).as_posix() not in seen:
                stack.append(nxt)
    return tuple(seen[k] for k in sorted(seen))


# ── the scan ────────────────────────────────────────────────────────────────


def _dotted(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def import_aliases(tree: ast.Module) -> Dict[str, str]:
    """Local name -> real dotted name for this module's imports, anywhere in it.

    So `import os as _os`, `from os import remove` and `from os.path import
    exists` resolve to os.*, os.remove and os.path.exists before matching --
    matching the literal dotted text alone let every aliased call through.
    """
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def resolved_name(func: ast.AST, aliases: Dict[str, str]) -> Optional[str]:
    name = _dotted(func)
    if not name:
        return None
    head, _, rest = name.partition(".")
    real = aliases.get(head)
    if real is None:
        return name
    return f"{real}.{rest}" if rest else real


def operation(
    call: ast.Call, aliases: Optional[Dict[str, str]] = None
) -> Optional[str]:
    """The filesystem operation a call performs, or None if it is not one."""
    name = resolved_name(call.func, aliases or {})
    if name in _FUNCTIONS:
        return name
    if name and name.startswith("shutil.") and name.split(".", 1)[1] in _SHUTIL:
        return name
    if not isinstance(call.func, ast.Attribute):
        return None
    if name and name.split(".")[0] in {m.split(".")[0] for m in GOVERNED_MODULES}:
        return None
    attr = call.func.attr
    # Path.replace(target) takes one argument; str.replace(old, new) two.
    if attr == "replace" and len(call.args) == 1 and not call.keywords:
        return ".replace"
    return f".{attr}" if attr in _METHODS else None


def _is_git_dir_literal(value: object) -> bool:
    return isinstance(value, str) and (
        value == ".git"
        or value.startswith(".git/")
        or value.endswith("/.git")
        or "/.git/" in value
    )


def _git_dir_names(scope: ast.AST) -> Set[str]:
    """Names this scope binds to a value built from a ``.git`` literal."""
    names: Set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _mentions_git_dir(value, set()):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _mentions_git_dir(node: ast.AST, git_names: Set[str]) -> bool:
    return any(
        (isinstance(n, ast.Constant) and _is_git_dir_literal(n.value))
        or (isinstance(n, ast.Name) and n.id in git_names)
        for n in ast.walk(node)
    )


def git_operation(
    call: ast.Call,
    tree: ast.Module,
    scope: ast.AST,
    spawn_aliases: Dict[str, str],
) -> Optional[str]:
    """The local-git operation a call performs, or None.

    Raw spawns are read with the git seam guard's own argv reader, so the two
    guards agree on what a git call is; a spawn whose argv it cannot read is
    listed as possibly git rather than skipped.
    """
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name in _GIT_CHOKEPOINTS:
        argv, _ = _git_argv(call, "list", scope, tree)
        verb = _verb(argv) if argv and argv[0] == "git" else None
        return f"{name} {verb or '<dynamic>'}"
    kind = _call_kind(call, spawn_aliases)
    if kind is None:
        return None
    argv, builders = _git_argv(call, kind, scope, tree)
    if argv is None:
        return None
    if builders is not None:
        return "git <unreadable argv>"
    return f"git {_verb(argv) or '<dynamic>'}"


def tree_tool_operation(call: ast.Call, spawn_aliases: Dict[str, str]) -> Optional[str]:
    """``spawn <program>`` for a spawn whose literal program is a tree tool."""
    kind = _call_kind(call, spawn_aliases)
    if kind not in ("list", "varargs", "string") or not call.args:
        return None
    first = call.args[0]
    program: Optional[str] = None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        # One string is a shell command line (or a lone program name).
        words = first.value.split()
        program = words[0] if words else None
    elif isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            program = head.value
    if program is None or Path(program).name not in _TREE_TOOLS:
        return None
    return f"spawn {Path(program).name}"


def scan_source(module: str, source: str) -> Counter:
    tree = ast.parse(source)
    aliases = import_aliases(tree)
    spawn_aliases = _direct_import_aliases(tree)
    found: Counter = Counter()
    scopes: List[str] = []
    # The innermost function (argv is resolved in it) and the names it binds
    # to a .git path, alongside the dotted scope name.
    functions: List[ast.AST] = [tree]
    git_names: List[Set[str]] = [set()]

    class _Visitor(ast.NodeVisitor):
        def _scoped(self, node: ast.AST, name: str) -> None:
            scopes.append(name)
            self.generic_visit(node)
            scopes.pop()

        def _function(self, node: ast.AST, name: str) -> None:
            functions.append(node)
            git_names.append(_git_dir_names(node))
            self._scoped(node, name)
            functions.pop()
            git_names.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._function(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._function(node, node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scoped(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:
            where = ".".join(scopes) or "<module>"
            op = operation(node, aliases)
            if op is not None:
                if _mentions_git_dir(node, git_names[-1]):
                    op = f"{op}{_GIT_DIR_SUFFIX}"
                found[(module, where, op)] += 1
            git_op = git_operation(node, tree, functions[-1], spawn_aliases)
            if git_op is not None:
                found[(module, where, git_op)] += 1
            tool_op = tree_tool_operation(node, spawn_aliases)
            if tool_op is not None:
                found[(module, where, tool_op)] += 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


@functools.lru_cache(maxsize=None)
def _scan_cached() -> Counter:
    found: Counter = Counter()
    for path in fenced_files():
        rel = path.relative_to(_SRC).as_posix()
        found.update(scan_source(rel, path.read_text(encoding="utf-8")))
    return found


def scan() -> Counter:
    return Counter(_scan_cached())


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
        row = {
            "module": key[0],
            "function": key[1],
            "op": key[2],
            "count": found[key],
            "class": old.get("class", "unclassified"),
            "baseline": old.get("baseline", baseline),
            "note": old.get("note", ""),
        }
        # The ratchet only goes down: a smaller count lowers the ceiling, a
        # larger one leaves it where it was, for the guard to fail on.
        if "ceiling" in old:
            row["ceiling"] = min(old["ceiling"], found[key])
        for carried in ("gate", "area"):
            if carried in old:
                row[carried] = old[carried]
        rows.append(row)
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
        "`PYTHONPATH=src python3 -m tests.unit.server.test_content_seam_inventory --write`.",
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
    local_git = [
        r
        for r in rows
        if in_index_query_path(r["module"])
        and r["class"] in OPEN_CLASSES
        and is_local_git(r["op"])
    ]
    lines += [
        "",
        "## Local git and .git probes still open in the indexing and query paths",
        "",
        "Ratcheted (see the test's docstring): this list may only shrink.",
        "",
        "| Module | Function | Op | Note |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{r['module']}` | `{r['function']}` | `{r['op']}` | {r['note']} |"
        for r in local_git
    ]
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


def test_no_new_open_rows_in_the_indexing_and_query_paths() -> None:
    """A new local probe where indexing or query runs fails the build.

    This is the !155 bug class: a local `git`, `.exists()` or `.stat()` that
    answers "nothing here" on a pod without the repository, and an index
    emptied on that answer. Classifying the new row repo-read is not enough
    there; it has to go through the seam.
    """
    over = [
        (k, r["class"], r["count"], r.get("ceiling"))
        for k, r in sorted(load_inventory().items())
        if in_index_query_path(k[0])
        and r["class"] in OPEN_CLASSES
        and r["count"] > r.get("ceiling", 0)
    ]
    assert not over, (
        "new direct repository access in the indexing/query path -- read through "
        "the content backend, the pass manifest or the git backend instead "
        "(module, function, op), class, count, ceiling:\n"
        + "\n".join(f"  {o}" for o in over)
    )


def test_no_open_repository_rows_outside_the_excluded_areas() -> None:
    """The goal state: outside activation and SCIP (rows with an explicit area),
    nothing the server runs reads or writes the repository on this pod.

    Each such access is migrated onto a seam, refused under delegated, or
    proven unreachable there (local-only), or is not repository data at all.
    A new direct repository access anywhere else fails here, whatever its
    count -- classify it as what it is, and if that is repo-read/repo-write,
    route it through the content backend, the content writer or the git
    backend, or refuse the feature under delegated (``refuse_when_delegated``).
    """
    left = sorted(
        (k, r["class"])
        for k, r in load_inventory().items()
        if r["class"] in REPOSITORY_CLASSES and area_of(r) is None
    )
    assert not left, (
        f"{len(left)} open repository rows outside {EXCLUDED_AREAS}:\n"
        + "\n".join(f"  {k} {c}" for k, c in left[:40])
    )


def test_explicit_areas_are_known_and_explained() -> None:
    """An ``area`` hands a row to another work stream: it must name one, and
    say why the row belongs there (its note)."""
    bad = [
        (k, r.get("area"))
        for k, r in load_inventory().items()
        if "area" in r and (r["area"] not in EXCLUDED_AREAS or not r["note"])
    ]
    assert not bad, f"rows with an unknown or unexplained area: {bad[:20]}"


@functools.lru_cache(maxsize=None)
def _function_node(module: str, function: str) -> Optional[ast.AST]:
    node: ast.AST = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    for part in [] if function == "<module>" else function.split("."):
        found = next(
            (
                child
                for child in ast.iter_child_nodes(node)
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                and child.name == part
            ),
            None,
        )
        if found is None:
            return None
        node = found
    return node


def _identifiers(node: ast.AST) -> Iterable[str]:
    """Names, attributes, parameters -- and the attribute a getattr() reads.

    Not every string: a docstring that says "manifest" is not a branch on one.
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr
        elif isinstance(n, ast.arg):
            yield n.arg
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("getattr", "hasattr")
            and len(n.args) >= 2
            and isinstance(n.args[1], ast.Constant)
            and isinstance(n.args[1].value, str)
        ):
            yield n.args[1].value


def _evidence(module: str, function: str, pattern: "re.Pattern[str]") -> List[str]:
    """Identifiers in *function* that match *pattern*.

    *function* may be ``other/module.py:Class.method`` for a gate that lives
    in another module.
    """
    if ":" in function:
        module, function = function.split(":", 1)
    node = _function_node(module, function)
    if node is None:
        return []
    return sorted({i for i in _identifiers(node) if pattern.search(i)})


def seam_evidence(module: str, function: str) -> List[str]:
    """Identifiers in *function* that name a seam (_SEAM_EVIDENCE)."""
    return _evidence(module, function, _SEAM_EVIDENCE)


def refusal_evidence(module: str, function: str) -> List[str]:
    """Identifiers in *function* that refuse under delegated (_REFUSAL_EVIDENCE)."""
    return _evidence(module, function, _REFUSAL_EVIDENCE)


def test_refused_rows_show_their_refusal() -> None:
    """`refused` is a claim that a delegated branch refuses before the access.

    Checked anywhere, not only in the indexing and query paths: the function
    (or the ``gate`` the row names) must call a refusal
    (``_REFUSAL_EVIDENCE``). Naming a backend or the delegated predicate is
    not enough: that is a branch, not a refusal.
    """
    unproven = [
        (k, r.get("gate"))
        for k, r in sorted(load_inventory().items())
        if r["class"] == "refused" and not refusal_evidence(k[0], r.get("gate", k[1]))
    ]
    assert not unproven, (
        "rows classified refused whose function (or gate) no longer refuses "
        "under delegated ((module, function, op), gate):\n"
        + "\n".join(f"  {u}" for u in unproven)
    )


def test_migrated_rows_in_the_indexing_and_query_paths_show_their_seam() -> None:
    """`migrated` is a claim about code: that a delegated branch exists.

    Checked, not trusted: the function itself (or the `gate` the row names)
    must ask a backend or the manifest. Removing the delegated branch from a
    migrated function leaves every count unchanged -- this is what fails.
    """
    unproven = [
        (k, r.get("gate"))
        for k, r in sorted(load_inventory().items())
        if in_index_query_path(k[0])
        and r["class"] == "migrated"
        and not seam_evidence(k[0], r.get("gate", k[1]))
    ]
    assert not unproven, (
        "rows classified migrated whose function no longer asks a seam -- "
        "restore the delegated branch, or reclassify the row as the open access "
        "it now is ((module, function, op), gate):\n"
        + "\n".join(f"  {u}" for u in unproven)
    )


# ── the guard sees what it claims to ────────────────────────────────────────


def unaccounted_module_calls() -> Counter:
    """Calls into GOVERNED_MODULES that are neither inventoried nor known not-fs."""
    governed_heads = {m.split(".")[0] for m in GOVERNED_MODULES}
    unknown: Counter = Counter()
    for path in fenced_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = resolved_name(node.func, aliases)
            if not name or name.split(".")[0] not in governed_heads:
                continue
            if name.rsplit(".", 1)[0] not in GOVERNED_MODULES:
                continue
            if operation(node, aliases) is None and name not in KNOWN_NOT_FS:
                unknown[name] += 1
    return unknown


def test_every_module_call_is_accounted_for() -> None:
    """The scanner is closed over its modules: a call it does not recognise
    fails here rather than silently staying out of the inventory."""
    unknown = unaccounted_module_calls()
    assert not unknown, (
        "calls the scanner neither inventories nor knows to be non-filesystem -- "
        f"add each to _FUNCTIONS or KNOWN_NOT_FS: {dict(unknown)}"
    )


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

    def test_aliased_imports_resolve_before_matching(self) -> None:
        src = (
            "import os as _os\nfrom os import remove\nfrom os.path import exists\n"
            "from shutil import rmtree as nuke\n"
            "def f(p):\n    _os.listdir(p); remove(p); exists(p); nuke(p)\n"
        )
        assert set(self._ops(src)) == {
            "os.listdir",
            "os.remove",
            "os.path.exists",
            "shutil.rmtree",
        }

    def test_fd_opens_realpath_and_tempfiles_are_listed(self) -> None:
        src = (
            "import os, tempfile\n"
            "def f(p):\n    os.open(p, 0); os.path.realpath(p); tempfile.mkstemp(dir=p)\n"
        )
        assert set(self._ops(src)) == {
            "os.open",
            "os.path.realpath",
            "tempfile.mkstemp",
        }

    def test_path_arithmetic_is_not_listed(self) -> None:
        src = "import os\ndef f(p):\n    os.path.join(p, 'x'); os.path.dirname(p)\n"
        assert not self._ops(src)

    def test_the_fence_reaches_through_imports_and_includes_the_cli(self) -> None:
        fenced = {p.relative_to(_SRC).as_posix() for p in fenced_files()}
        assert "indexing/__init__.py" in fenced, "ancestor package inits are skipped"
        assert "indexing/processor.py" in fenced, (
            "an __init__'s own imports are skipped"
        )
        assert "cli.py" in fenced, "the spawned `cidx index` child is out of scope"
        assert "indexing/file_finder.py" in fenced, "the import closure is not followed"
        assert "server/app.py" in fenced

    def test_local_git_is_listed_by_verb(self) -> None:
        src = (
            "import subprocess\nfrom git import Repo\n"
            "from code_indexer.utils.git_runner import run_git_command\n"
            "def f(p, argv):\n"
            "    subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=p)\n"
            "    cmd = ['git', 'ls-tree', '-r', 'HEAD']\n"
            "    subprocess.run(cmd, cwd=p)\n"
            "    run_git_command(['git', 'diff', '--name-only'], cwd=p)\n"
            "    run_git_command(argv, cwd=p)\n"
            "    Repo(p)\n"
        )
        assert set(self._ops(src)) == {
            "git rev-parse",
            "git ls-tree",
            "run_git_command diff",
            "run_git_command <dynamic>",
            "git.Repo",
        }

    def test_a_spawn_that_might_be_git_is_listed(self) -> None:
        src = "import subprocess\ndef f(prog):\n    subprocess.run([prog, 'status'])\n"
        assert set(self._ops(src)) == {"git <unreadable argv>"}

    def test_non_git_spawns_and_asyncio_run_are_not_listed(self) -> None:
        src = (
            "import asyncio, subprocess\n"
            "def f(coro):\n    subprocess.run(['cidx', 'index']); asyncio.run(coro)\n"
        )
        assert not self._ops(src)

    def test_a_tree_tool_spawn_is_listed(self) -> None:
        src = (
            "import subprocess\n"
            "def f(src, dst, root):\n"
            "    subprocess.run(['cp', '-a', src, dst], check=True)\n"
            "    subprocess.Popen(('/usr/bin/rg', '--json', 'x', root))\n"
            "    subprocess.check_output('find . -name x', shell=True)\n"
            "    subprocess.run(['systemctl', 'restart', 'cidx'])\n"
        )
        assert self._ops(src) == Counter(
            {"spawn cp": 1, "spawn rg": 1, "spawn find": 1}
        )

    def test_a_dot_git_probe_is_its_own_op(self) -> None:
        src = (
            "import os\n"
            "def f(p):\n"
            "    (p / '.git').exists()\n"
            "    git_dir = os.path.join(p, '.git')\n"
            "    os.path.isdir(git_dir)\n"
            "    (p / '.gitignore').exists()\n"
        )
        assert self._ops(src) == Counter(
            {".exists .git": 1, "os.path.isdir .git": 1, ".exists": 1}
        )


class TestTheIndexingAndQueryRules:
    def test_a_ceiling_only_goes_down(self) -> None:
        key = ("services/x.py", "f", ".exists")
        old = {key: {"class": "repo-read", "baseline": True, "note": "", "ceiling": 2}}
        assert merged_rows(Counter({key: 1}), old, baseline=False)[0]["ceiling"] == 1
        assert merged_rows(Counter({key: 3}), old, baseline=False)[0]["ceiling"] == 2

    def test_a_new_row_gets_no_ceiling(self) -> None:
        key = ("services/x.py", "f", "git status")
        assert "ceiling" not in merged_rows(Counter({key: 1}), {}, baseline=False)[0]

    def test_the_paths_cover_indexing_and_query(self) -> None:
        for module in (
            "services/smart_indexer.py",
            "services/git_topology_service.py",
            "indexing/file_finder.py",
            "storage/filesystem_vector_store.py",
            "server/query/semantic_query_manager.py",
        ):
            assert in_index_query_path(module), module
        assert not in_index_query_path("server/routers/git.py")

    def test_evidence_is_a_branch_not_a_docstring(self) -> None:
        tree = ast.parse(
            "def has(self):\n    if self._git_backend is not None:\n        pass\n"
            "def attr(self):\n    return getattr(self, 'repo_manifest', None)\n"
            'def doc():\n    """Uses the delegated manifest."""\n'
        )
        found = {
            f.name: sorted(i for i in _identifiers(f) if _SEAM_EVIDENCE.search(i))
            for f in tree.body
            if isinstance(f, ast.FunctionDef)
        }
        assert found == {"has": ["_git_backend"], "attr": ["repo_manifest"], "doc": []}

    def test_refusal_evidence_is_a_refusal_not_a_backend(self) -> None:
        tree = ast.parse(
            "def refuses():\n    refuse_when_delegated('f', 'r')\n"
            "def raises():\n    raise DelegatedRepositoryUnsupportedError('f', 'r')\n"
            "def branches(self):\n    if self._content_backend is not None:\n"
            "        pass\n"
        )
        found = {
            f.name: sorted(i for i in _identifiers(f) if _REFUSAL_EVIDENCE.search(i))
            for f in tree.body
            if isinstance(f, ast.FunctionDef)
        }
        assert found == {
            "refuses": ["refuse_when_delegated"],
            "raises": ["DelegatedRepositoryUnsupportedError"],
            "branches": [],
        }

    def test_a_gate_in_another_module_is_followed(self) -> None:
        assert seam_evidence(
            "x.py", "services/smart_indexer.py:SmartIndexer._smart_index"
        ) == ["git_backend"]


class TestTheExcludedAreas:
    @pytest.mark.parametrize(
        "row,area",
        [
            # Only an explicit area hands a row over; a name never does.
            ({"module": "scip/generator.py", "function": "f"}, None),
            (
                {
                    "module": "server/services/dependency_map_service.py",
                    "function": "f",
                },
                None,
            ),
            (
                {
                    "module": "server/repositories/activated_repo_manager.py",
                    "function": "f",
                },
                None,
            ),
            ({"module": "server/x.py", "function": "Svc.deactivate_repo"}, None),
            (
                {"module": "server/x.py", "function": "f", "area": "activation"},
                "activation",
            ),
            ({"module": "scip/generator.py", "function": "f", "area": "scip"}, "scip"),
            (
                {"module": "server/services/scip_query_service.py", "function": "f"},
                None,
            ),
            ({"module": "server/wiki/routes.py", "function": "f"}, None),
        ],
    )
    def test_area_of(self, row: dict, area: Optional[str]) -> None:
        assert area_of(row) == area

    def test_an_area_survives_regeneration(self) -> None:
        key = ("server/x.py", "f", ".exists")
        old = {
            key: {
                "class": "repo-read",
                "baseline": True,
                "note": "n",
                "area": "activation",
            }
        }
        assert (
            merged_rows(Counter({key: 1}), old, baseline=False)[0]["area"]
            == "activation"
        )


if __name__ == "__main__":
    if "--write" in sys.argv:
        write(baseline="--baseline" in sys.argv)
        print(f"wrote {_INVENTORY} and {_SUMMARY}")
    else:
        print(__doc__)
