"""The content-seam inventory guard's engine: list every filesystem access.

The guard (entry point and committed inventory in the core:
``tests/unit/server/test_content_seam_inventory.py`` and
``content_seam_inventory.json``) keeps a TRUE list of every filesystem
operation, local git run and ``.git`` probe the server can reach, one row per
(module, function, operation), so "can the repository mount come off" is
answered by reading a file. This module is the scanner: what counts as a
filesystem operation (module functions, ``shutil``, pathlib-style methods,
aliased imports), the git operations (read with the git-seam guard's own argv
reader, :mod:`cidx_testkit.guards.git_seam`), tree tools spawned as processes,
and how a fresh scan is merged into the committed rows.

Which modules are scanned is a :class:`~cidx_testkit.guards.source_tree.SourceTree`
closure, so a seam implementation moved into its own distribution
(``cidx_backend_workspace``) is still inventoried -- under the key
``<package>/<path>``. Classification policy (classes, excluded areas, the
indexing and query paths) stays with the distribution's entry point.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from cidx_testkit.guards.git_seam import (
    _call_kind,
    _direct_import_aliases,
    _git_argv,
    _verb,
)
from cidx_testkit.guards.source_tree import SourceTree

Key = Tuple[str, str, str]  # (module, function, op)


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
        "os.getpgrp",
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


def is_local_git(op: str) -> bool:
    """A row that runs git here or probes a .git directory."""
    return op.startswith(_LOCAL_GIT_OPS) or op.endswith(_GIT_DIR_SUFFIX)


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


_TREE_TOOLS = frozenset(
    {"rg", "grep", "find", "cp", "mv", "rm", "rsync", "tar", "du", "ls", "cat"}
)


def tree_tool_operation(call: ast.Call, spawn_aliases: Dict[str, str]) -> Optional[str]:
    """``spawn <program>`` for a spawn whose literal program is a tree tool."""
    kind = _call_kind(call, spawn_aliases)
    if kind not in ("list", "varargs", "string") or not call.args:
        return None
    first = call.args[0]
    program: Optional[str] = None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
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


def fenced_files(tree: SourceTree, seeds: Sequence[str]) -> Tuple[Path, ...]:
    """Every first-party module the server can run, seeds plus imports, across
    the tree's distributions (relative imports, imported submodules and
    ancestor packages included)."""
    return tree.closure(seeds)


def scan_tree(tree: SourceTree, seeds: Sequence[str]) -> Counter:
    """Every (module, function, op) the fence reaches, with its call count."""
    found: Counter = Counter()
    for path in fenced_files(tree, seeds):
        found.update(scan_source(tree.key(path), path.read_text(encoding="utf-8")))
    return found
