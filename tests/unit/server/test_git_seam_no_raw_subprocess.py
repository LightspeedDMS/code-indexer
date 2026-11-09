"""issue-64678: the git seam owns every server-side git invocation.

``utils/git_runner.run_git_command`` is the ONE chokepoint git calls bottom out
at: it asks ``is_mutating_git()`` whether a command mutates and, when an invoker
is installed, delegates the mutating ones to git-mcp. Reads stay local -- that is
a deliberate, credential-driven policy (a read needs the working tree, already in
the pod on shared NFS, and no PAT; a mutation needs the PAT, which issue-64616
confines to git-mcp).

That policy only holds if the chokepoint is the only door. A raw
``subprocess.run(["git", ...])`` under ``server/`` or ``global_repos/`` skips both
the delegation decision AND ``get_git_environment()``'s safe.directory handling.

Upstream has no reason to know the seam exists, so each upstream snapshot can
reintroduce raw calls in code that merges without conflict -- the v12.78.0
snapshot brought four of them into ``golden_repo_manager.py`` alone. This test
turns that from archaeology into a build failure.

To add a site here you must justify it in ``ALLOWED_READS``. A MUTATING command
is never allowed: route it through ``run_git_command``.

The scanning engine (argv reading, spawn shapes, the reachable-closure walk
across distributions) is ``cidx_testkit.guards.git_seam``; this module holds
the core's POLICY -- what is governed, exempt and allowlisted -- and the tests.
The closure crosses into the in-repo distributions the server imports
(``cidx_sdk``, ``cidx_backend_*``), so a seam implementation moved out of
``code_indexer`` is still scanned.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import List, Tuple

from cidx_testkit.guards import git_seam as _engine
from cidx_testkit.guards.git_seam import (
    GitSeamPolicy,
    _call_kind,
    _direct_import_aliases,
    _enclosing_scope,
    _git_argv,
    _scope_index,
    _verb,
)
from cidx_testkit.guards.source_tree import in_tree_source_tree

from code_indexer.utils.git_invoker import is_mutating_git

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TREE = in_tree_source_tree(_REPO_ROOT, primary="code_indexer")
_SRC = _TREE.primary_root
_engine.configure_classifier(is_mutating_git)

__all__ = [
    "_call_kind",
    "_direct_import_aliases",
    "_enclosing_scope",
    "_git_argv",
    "_scope_index",
    "_verb",
]

# Directories the seam governs. The CLI runs on a developer's own machine, so
# client-side git is out of scope; auto_update/ and scripts/ are deploy tooling
# that runs outside the request path.
# SEEDS, not the whole fence. What the seam governs is what the server can
# REACH: the fence is the transitive first-party import closure of these, so a
# raw mutating git call one directory away is not invisible just because it
# lives outside `server/`. `services/git_topology_service.py` is imported by
# `server/mcp/handlers/repos.py` and was never scanned under the old rule.
# The in-tree feature add-ons (packages/features/*/src/<package>) are seeds too:
# the server loads an enabled one through its entry point, which no import
# closure of the core shows (since modular phase 4c the core imports no
# cidx_xray module). Named relative to the core's package root.
_GOVERNED = ("server", "global_repos") + tuple(
    os.path.relpath(package, _TREE.primary_root)
    for src in sorted((_REPO_ROOT / "packages" / "features").glob("*/src"))
    for package in sorted(src.iterdir())
    if (package / "__init__.py").is_file()
)

# Deploy tooling that runs outside the request path. Matched as a PATH PREFIX:
# as "any component" it would have exempted a future
# `server/repositories/scripts/` for free.
_EXEMPT_DIRS = ("scripts", "server/scripts")

# auto_update is NOT a blanket exemption any more. Its git calls act on the
# server's OWN source tree and on hnswlib -- never on a tenant golden repo --
# so git-mcp, which exists to hold golden-repo credentials, is the wrong
# destination for them. That justification is real, but it belongs against each
# CALL: as a directory rule it silently covered any future mutation dropped in
# the same folder, and three request-path modules import DeploymentExecutor.
# Keyed (path, verb) so a new verb in the same file fails until justified.
ALLOWED_SELF_UPDATE_MUTATIONS = {
    # `git fetch` / `git pull origin <branch>` on the cidx checkout itself
    ("server/auto_update/change_detector.py", "fetch"),
    ("server/auto_update/deployment_executor.py", "pull"),
    # `git clone <hnswlib>` to a fallback path, and submodule sync for the build
    ("server/auto_update/deployment_executor.py", "clone"),
    ("server/auto_update/deployment_executor.py", "submodule"),
}

# Read-only git that deliberately runs locally. is_mutating_git() returns False
# for every entry; each is keyed by (path relative to src/code_indexer, git verb)
# so it survives line-number drift. A verb appearing here does NOT whitelist its
# mutating forms -- the assertion below re-checks each call against the
# classifier, so `git remote show` being listed cannot smuggle in `git remote add`.
ALLOWED_READS = {
    ("server/services/git_operations_service.py", "config"),
    # These argv lists are handed to the git tools' DELEGATED runner
    # (DelegatedGitTools.run -> git-mcp git_exec), never to a local process:
    # a mount-less pod asks git-mcp whether a copy is a repository / mid-merge.
    ("server/services/git_tools_seam.py", "rev-parse"),
    ("server/mcp/handlers/_legacy.py", "rev-parse"),
    ("server/services/golden_repo_reconciler.py", "rev-parse"),
    ("server/startup/lifespan.py", "remote"),
    ("server/repositories/activated_repo_manager.py", "branch"),
    ("server/repositories/activated_repo_manager.py", "diff"),
    ("server/repositories/activated_repo_manager.py", "rev-parse"),
    ("server/repositories/activated_repo_manager.py", "log"),
    ("server/repositories/activated_repo_manager.py", "status"),
    ("server/repositories/golden_repo_manager.py", "config"),
    ("server/repositories/golden_repo_manager.py", "rev-parse"),
    ("server/repositories/repository_listing_manager.py", "ls-remote"),
    ("server/routers/activated_repos.py", "branch"),
    ("server/routers/git.py", "rev-parse"),
    ("server/routers/git.py", "show"),
    ("server/routers/git.py", "blame"),
    # annotated local: `cmd: list[str] = ["git", "log", "--follow", ...]`
    ("server/routers/git.py", "log"),
    ("server/routers/inline_repos.py", "log"),
    # Self-update reads on the server's OWN checkout (see
    # ALLOWED_SELF_UPDATE_MUTATIONS for why this directory is not delegated).
    ("server/auto_update/change_detector.py", "rev-parse"),
    ("server/auto_update/deployment_executor.py", "ls-files"),
    # Reads are listed only for the CORE (see _CORE): a read outside it is local
    # by definition and listing 50+ of them would bury the entries that matter.
    # variable-argv reads (cmd = ["git", ...]; subprocess.run(cmd, ...)) -- only
    # visible since the guard resolves a Name to its assignment.
    ("server/services/golden_repo_branch_service.py", "for-each-ref"),
    ("server/services/golden_repo_branch_service.py", "symbolic-ref"),
    ("server/services/golden_repo_branch_service.py", "branch"),
    ("server/services/remote_branch_service.py", "ls-remote"),
}

# argv built by a helper we cannot read statically. Each entry asserts: this
# builder does not produce a git command. Keyed on (file, builder) so it survives
# edits and a NEW builder in the same file still fails until justified.
ALLOWED_UNREADABLE_ARGV = {
    # builds `cidx index ...` -- server layout flags appended to a cidx invocation
    ("server/repositories/golden_repo_manager.py", "append_server_layout_args"),
    # builds the `claude` CLI invocation
    ("server/services/claude_invoker.py", "_build_claude_command"),
    # `base_cmd + [...]` where base_cmd is the `claude` CLI invocation
    ("server/services/research_assistant_service.py", "<concatenation>"),
    # Sites the fail-closed rewrite made visible. Each was triaged by reading
    # the call: none is git, and none can become git without editing the line.
    # `append_server_layout_args(["cidx", "index"])` -- the cidx CLI again
    ("server/services/claude_cli_manager.py", "append_server_layout_args"),
    # `[indexer_name, "--version"]` -- probes whether an indexer is installed
    ("server/installer.py", "<dynamic-program>"),
    # Popen of the cidx server itself during lifecycle start
    ("server/lifecycle/server_lifecycle_manager.py", "<dynamic-program>"),
    # `create_subprocess_exec(program, *args)` where program is `command.split()[0]`
    # of a caller-supplied tool-version string ("rg --version")
    ("server/services/diagnostics_service.py", "<starred-argv>"),
    # `create_subprocess_exec(*cmd)` -- cmd is a parameter holding the claude CLI
    # (moved with the SCIP feature into its own distribution)
    ("cidx_scip/server/self_healing.py", "<starred-argv>"),
    # `subprocess.run(list(outcome.command))` -- the command a feature's index
    # builder answers ``run_locally`` with (SCIP: `cidx scip generate`), run in
    # the repository by the add-index job and the activated reindex (modular
    # phase 4d, Feature.index_builders()); never git.
    ("server/repositories/golden_repo_manager.py", "list"),
    ("server/services/activated_repo_index_manager.py", "list"),
    # `os.execv(sys.executable, [sys.executable] + sys.argv)` -- dev-mode re-exec
    ("server/web/pages/restart.py", "<dynamic-program>"),
    # Self-update tooling: builds `cidx`/`python`/`pip`/shell invocations to
    # rebuild and restart the server itself. Not git, and not reachable with a
    # tenant's credentials -- see ALLOWED_SELF_UPDATE_MUTATIONS.
    ("server/auto_update/deployment_executor.py", "<concatenation>"),
    ("server/auto_update/deployment_executor.py", "<dynamic-program>"),
    ("server/auto_update/deployment_executor.py", "<expression>"),
    ("server/auto_update/deployment_executor.py", "<shell-wrapper>"),
    # `[str(xray_cli_path), "--print-graph-extractor-extensions"]` -- the xray
    # binary, whose path is resolved at runtime (CIDX_XRAY_CLI_PATH).
    # (moved with the X-Ray feature into its own distribution)
    ("cidx_xray/rust_backend.py", "<dynamic-program>"),
}

# Where the READ allowlist is enforced. Mutations and unreadable argv are
# enforced across the whole reachable closure -- those are the safety
# properties -- but ALLOWED_READS exists to force a human to look at each read
# in the request path, and extending it to every reachable module would add
# ~50 entries of noise and devalue the ones that matter. A read outside the
# core runs locally either way; the seam's rule is about mutations.
_CORE = ("server/", "global_repos/")


def _policy() -> GitSeamPolicy:
    """The allowlists above, read at call time (tests patch them)."""
    module = sys.modules[__name__]
    return GitSeamPolicy(
        allowed_reads=frozenset(module.ALLOWED_READS),
        allowed_unreadable_argv=frozenset(module.ALLOWED_UNREADABLE_ARGV),
        allowed_self_update_mutations=frozenset(module.ALLOWED_SELF_UPDATE_MUTATIONS),
        core_prefixes=tuple(_CORE),
    )


def _is_exempt(rel: str) -> bool:
    """True if *rel* is deploy tooling outside the request path."""
    return _engine.is_exempt(rel, _EXEMPT_DIRS)


def _governed_files() -> List[Path]:
    """Every first-party module the server can reach by import.

    Seeded from _GOVERNED and closed over imports -- across the in-repo
    distributions -- so moving a file out of `server/` (or out of the
    code_indexer package) no longer moves it out of the seam's scope.
    """
    return _engine.governed_files(_TREE, _GOVERNED, _EXEMPT_DIRS)


def _scan() -> Tuple[List[str], List[str]]:
    """Return (violations, unknown_argv) across every governed file."""
    return _engine.scan_tree(_TREE, _GOVERNED, _policy(), _EXEMPT_DIRS)


def _scan_source(
    rel: str, source: str, apply_self_update_allowlist: bool = True
) -> Tuple[List[str], List[str]]:
    """One module's source against this module's policy."""
    return _engine.scan_source(rel, source, _policy(), apply_self_update_allowlist)


def test_no_raw_mutating_git_outside_the_seam() -> None:
    """No server-side git mutation may bypass run_git_command()."""
    violations, _ = _scan()
    assert not violations, (
        "git calls under server/ or global_repos/ must go through "
        "code_indexer.utils.git_runner.run_git_command() so the seam can decide "
        "whether to delegate (issue-64678).\n\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nA MUTATING call is never allowed here: route it through "
        "run_git_command(). A genuinely read-only call may be added to "
        "ALLOWED_READS in this file, with a reason."
    )


def test_no_raw_git_with_unknowable_argv() -> None:
    """A raw call whose argv is built at runtime cannot be classified.

    Such a site can silently become a mutation later, so it must go through the
    chokepoint, where is_mutating_git() sees the real argv.
    """
    _, unknown = _scan()
    assert not unknown, (
        "these raw git calls build argv at runtime, so no static check can tell "
        "whether they mutate -- route them through run_git_command() and let "
        "is_mutating_git() classify each call (issue-64678).\n\n"
        + "\n".join(f"  - {u}" for u in unknown)
    )


def test_unreadable_allowlist_has_no_stale_entries() -> None:
    """Keep ALLOWED_UNREADABLE_ARGV honest too.

    Mutation testing found that adding a fabricated entry here left all tests
    green: the staleness check below only ever subtracted from ALLOWED_READS.
    An allowlist nobody audits is how a justified exemption outlives the code
    that justified it.

    Scans ONCE with the allowlist emptied rather than re-scanning per entry --
    this runs in CI, and a per-entry rescan made the file take a minute.
    """
    import unittest.mock as _mock

    with _mock.patch.object(sys.modules[__name__], "ALLOWED_UNREADABLE_ARGV", set()):
        _, raw_unknown = _scan()

    stale = {
        (rel, builder)
        for rel, builder in ALLOWED_UNREADABLE_ARGV
        if not any(u.startswith(f"{rel}:") and repr(builder) in u for u in raw_unknown)
    }
    assert not stale, (
        "these ALLOWED_UNREADABLE_ARGV entries no longer match any call site -- "
        "remove them so the allowlist keeps meaning something:\n"
        + "\n".join(f"  - {e}" for e in sorted(map(str, stale)))
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Keep ALLOWED_READS honest as call sites are removed or seamed."""
    seen = set()
    for path in _governed_files():
        rel = _TREE.key(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _direct_import_aliases(tree)
        spans = _scope_index(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kind = _call_kind(node, aliases)
                if kind is None:
                    continue
                argv, _b = _git_argv(
                    node, kind, _enclosing_scope(tree, spans, node), tree
                )
                if argv is None:
                    continue
                verb = _verb(argv)
                if verb is not None:
                    seen.add((rel, verb))
    stale = sorted(ALLOWED_READS - seen)
    assert not stale, (
        "ALLOWED_READS entries no longer match any raw git call -- delete them:\n"
        + "\n".join(f"  - {s[0]}  git {s[1]}" for s in stale)
    )


# ---------------------------------------------------------------------------
# The guard's own behaviour.
#
# Everything above asserts something about the tree as it stands today, which
# means a guard that silently sees nothing passes just as loudly as one that
# works. These feed the scanner synthetic sources instead, so each shape it
# claims to cover is actually proven to be reported -- and a regression in the
# scanner fails here rather than going unnoticed until the next bypass ships.
# ---------------------------------------------------------------------------

_REL = "server/services/synthetic.py"


def _violations(source: str) -> List[str]:
    return _scan_source(_REL, source)[0]


def _unknowns(source: str) -> List[str]:
    return _scan_source(_REL, source)[1]


class TestGuardSeesEveryCallShape:
    def test_literal_argv(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["git", "push", "origin", "main"])\n'
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_in_a_plain_variable(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_in_an_annotated_variable(self) -> None:
        """The shape that slipped through the first version of this guard."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd: list[str] = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_argv_from_a_module_level_constant(self) -> None:
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def f():\n"
            "    subprocess.run(_PUSH)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_directly_imported_spawn_function(self) -> None:
        src = (
            "from subprocess import check_call as cc\n"
            "def f():\n"
            '    cc(["git", "reset", "--hard", "HEAD"])\n'
        )
        assert any("MUTATING: git reset" in v for v in _violations(src))

    def test_asyncio_create_subprocess_exec_varargs(self) -> None:
        src = (
            "import asyncio\n"
            "async def f():\n"
            '    await asyncio.create_subprocess_exec("git", "push", "origin", "main")\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_check_call_is_in_the_call_set(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.check_call(["git", "commit", "-m", "x"])\n'
        assert any("MUTATING: git commit" in v for v in _violations(src))


class TestGuardClassifiesCorrectly:
    def test_unlisted_read_is_reported(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["git", "status"])\n'
        assert any("unlisted read: git status" in v for v in _violations(src))

    def test_append_and_extend_keep_the_verb_readable(self) -> None:
        """They add to the end, so the program and verb survive."""
        src = (
            "import subprocess\n"
            "def f(path):\n"
            '    cmd = ["git", "status"]\n'
            "    cmd.append(path)\n"
            '    cmd.extend(["--", path])\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("unlisted read: git status" in v for v in _violations(src))
        assert not _unknowns(src)

    def test_insert_rewrites_the_front_so_argv_becomes_unreadable(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "status"]\n'
            '    cmd.insert(1, "push")\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("front rewritten" in u for u in _unknowns(src))

    def test_argv_built_by_a_helper_is_reported_not_skipped(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            "    cmd = build_it()\n"
            "    subprocess.run(cmd)\n"
        )
        assert any("build_it" in u for u in _unknowns(src))

    def test_generic_runner_taking_argv_from_its_caller_is_skipped(self) -> None:
        """Its caller is scanned on its own; flagging here would make every
        subprocess helper a permanent violation while catching nothing."""
        src = "import subprocess\ndef run_it(command):\n    subprocess.run(command)\n"
        assert not _violations(src)
        assert not _unknowns(src)

    def test_non_git_commands_are_ignored(self) -> None:
        src = 'import subprocess\ndef f():\n    subprocess.run(["cidx", "index", "--fts"])\n'
        assert not _violations(src)
        assert not _unknowns(src)

    def test_global_flags_do_not_hide_the_verb(self) -> None:
        src = (
            "import subprocess\n"
            "def f(repo):\n"
            '    subprocess.run(["git", "-C", repo, "push", "origin", "main"])\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_sibling_function_assignment_does_not_leak_into_a_runner(self) -> None:
        """A helper's parameter must not adopt another function's variable.

        `cmd`, `command` and `args` are common names. Resolving the module with
        ast.walk() reached into every function body, so a generic runner was
        reported as a bypass at a line that has none -- and the only escape was an
        allowlist entry for code that was never a bypass.
        """
        src = (
            "import subprocess\n"
            "def run_it(cmd):\n"
            "    subprocess.run(cmd)\n"
            "def other():\n"
            '    cmd = ["git", "push", "origin", "main"]\n'
            "    run_it(cmd)\n"
        )
        assert not _violations(src)
        assert not _unknowns(src)

    def test_mutation_hidden_in_one_branch_is_still_caught(self) -> None:
        """Taking the last assignment by line number read the else-branch and
        missed the push. Any reachable literal that mutates wins instead."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    else:\n"
            '        cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_unreadable_branch_is_not_masked_by_a_literal_read(self) -> None:
        """An unreadable branch outranks a readable read, whichever comes first.

        Severity, not line order, picks the candidate. Taking the highest line
        let a literal `git status` in the else-branch stand in for a builder the
        guard cannot read, so whether a possible `git push` got reported came
        down to which branch the author happened to write first.
        """
        unreadable_first = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_push_cmd()\n"
            "    else:\n"
            '        cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        unreadable_last = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "status"]\n'
            "    else:\n"
            "        cmd = build_push_cmd()\n"
            "    subprocess.run(cmd)\n"
        )
        for src in (unreadable_first, unreadable_last):
            unknowns = _unknowns(src)
            assert unknowns, f"unreadable branch masked in:\n{src}"
            # Named by ITS OWN builder: ALLOWED_UNREADABLE_ARGV is keyed on the
            # builder, so borrowing a different candidate's label would make a
            # justified allowlist entry silently fail to match.
            assert any("build_push_cmd" in u for u in unknowns), unknowns

    def test_mutating_literal_outranks_an_unreadable_branch(self) -> None:
        """Severity order is mutating > unreadable > last read: a command we can
        prove mutates stays the violation it is, rather than being softened into
        an unknown by an unreadable sibling branch."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_something()\n"
            "    else:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_aliased_varargs_import_is_not_read_as_list_style(self) -> None:
        """`from asyncio import create_subprocess_exec as cse` is still varargs.

        Deciding the convention from the LOCAL name made `cse` unknown to
        _ASYNC_EXEC, so the call was read as list-style: argv[0] became the whole
        command and a varargs `git push` scanned as clean.
        """
        src = (
            "from asyncio import create_subprocess_exec as cse\n"
            "async def f():\n"
            '    await cse("git", "push", "origin", "main")\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_aliased_list_style_import_still_resolves(self) -> None:
        """The same mapping must not mis-file `from subprocess import run as r`
        as varargs -- that would take argv[0] as the verb and miss everything."""
        src = (
            "from subprocess import run as r\n"
            "def f():\n"
            '    r(["git", "push", "origin", "main"])\n'
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_assignment_inside_a_match_case_reaches_the_call(self) -> None:
        """match/case arms are branches of the scope like if/else.

        They hang off `cases` rather than `body`/`orelse`, so the generic field
        walk dropped them and an argv built in a case arm was invisible.
        """
        src = (
            "import subprocess\n"
            "def f(mode):\n"
            "    match mode:\n"
            '        case "sync":\n'
            '            cmd = ["git", "push", "origin", "main"]\n'
            "        case _:\n"
            '            cmd = ["git", "status"]\n'
            "    subprocess.run(cmd)\n"
        )
        assert any("MUTATING: git push" in v for v in _violations(src))

    def test_allowlisted_builder_does_not_clear_an_unlisted_sibling(self) -> None:
        """One justified builder must not speak for an unjustified one.

        `claude_invoker.py` really does have `_build_claude_command` allowlisted.
        Collapsing the reaching builders to a single label meant that whichever
        one happened to be visited first decided the call: if the allowlisted one
        won, the site was skipped and the unlisted builder -- which could be
        building `git push` -- was never reported. Both orders are asserted
        because _statements_of pops its stack, so "first" tracks source layout.
        """
        rel = "server/services/claude_invoker.py"
        assert (rel, "_build_claude_command") in ALLOWED_UNREADABLE_ARGV
        allowed_first = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = _build_claude_command()\n"
            "    else:\n"
            "        cmd = build_push_cmd()\n"
            "    subprocess.run(cmd)\n"
        )
        allowed_last = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_push_cmd()\n"
            "    else:\n"
            "        cmd = _build_claude_command()\n"
            "    subprocess.run(cmd)\n"
        )
        for src in (allowed_first, allowed_last):
            _, unknown = _scan_source(rel, src)
            assert any("build_push_cmd" in u for u in unknown), (
                f"unlisted builder masked by the allowlisted sibling:\n{src}"
            )
            assert not any("_build_claude_command" in u for u in unknown), (
                f"the allowlisted builder must stay justified: {unknown}"
            )

    def test_every_unlisted_builder_is_reported(self) -> None:
        """Two unlisted builders reaching one call are two findings, not one --
        silencing the pair takes two justifications, not whichever is reported."""
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            "        cmd = build_a()\n"
            "    else:\n"
            "        cmd = build_b()\n"
            "    subprocess.run(cmd)\n"
        )
        unknown = _unknowns(src)
        assert any("build_a" in u for u in unknown), unknown
        assert any("build_b" in u for u in unknown), unknown


class TestGuardFailsClosed:
    """The contract: anything the guard cannot statically read is REPORTED.

    Six review rounds each closed the one shape the reviewer named, and an
    adversarial pass then found 41 more. These tests pin the CLASSES instead,
    because the recurring defect was never a missing shape -- it was
    `_git_argv` returning ``(None, None)``, which `_scan_source` cannot tell
    apart from "this call is not git at all". Silence is the bug.
    """

    def test_bindings_other_than_assignment_are_not_mistaken_for_parameters(
        self,
    ) -> None:
        """A name bound by any construct is bound; only a PARAMETER is a
        caller's business. Reading only Assign/AnnAssign made every other
        binding form look unbound, and unbound was treated as "generic runner,
        skip" -- so argv bound by a for-loop, a with-item or a walrus reached
        subprocess.run with no report at all."""
        shapes = {
            "for": (
                "import subprocess\n"
                "def f():\n"
                '    for cmd in [["git", "push", "origin", "main"]]:\n'
                "        subprocess.run(cmd)\n"
            ),
            "with": (
                "import subprocess\n"
                "def f(ctx):\n"
                "    with ctx as cmd:\n"
                "        subprocess.run(cmd)\n"
            ),
            "walrus": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run(cmd := ["git", "push", "origin", "main"])\n'
            ),
            "tuple-unpack": (
                "import subprocess\n"
                "def f():\n"
                '    cmd, env = (["git", "push", "origin", "main"], {})\n'
                "    subprocess.run(cmd)\n"
            ),
            "except-as": (
                "import subprocess\n"
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except OSError as cmd:\n"
                "        subprocess.run(cmd)\n"
            ),
            "comprehension": (
                "import subprocess\n"
                "def f(xs):\n"
                "    [subprocess.run(cmd) for cmd in xs]\n"
            ),
        }
        for name, src in shapes.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: argv reached a spawn with no report"

    def test_a_parameter_shadowing_a_module_constant_is_not_the_constant(
        self,
    ) -> None:
        """Pins `_parameters_of`, which mutation testing found unpinned.

        The module-level fallback exists so `_PUSH = ["git","push"]` used inside
        a function is still read. But a PARAMETER of the same name is the
        caller's value, not the module's -- without consulting the signature the
        fallback adopts the constant and reports a mutation at a generic runner
        that never runs one.
        """
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def run_it(_PUSH):\n"
            "    subprocess.run(_PUSH)\n"
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"a parameter was read as the module constant: {v + u}"

    def test_a_module_constant_is_still_read(self) -> None:
        """Negative control for the test above: the fallback must still work."""
        src = (
            "import subprocess\n"
            '_PUSH = ["git", "push", "origin", "main"]\n'
            "def f():\n"
            "    subprocess.run(_PUSH)\n"
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert any("MUTATING: git push" in x for x in v), v

    def test_a_real_parameter_is_still_skipped(self) -> None:
        """Negative control for the test above. A generic runner whose argv IS
        a parameter must stay silent -- its callers are scanned on their own,
        and flagging it would make every subprocess helper a permanent
        violation while catching nothing. This is the one legitimate silence,
        and it must be decided from the signature, not inferred from the
        absence of an assignment."""
        src = "import subprocess\ndef run_it(cmd):\n    subprocess.run(cmd)\n"
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"generic runner wrongly reported: {v + u}"

    def test_append_cannot_smuggle_a_mutating_subverb(self) -> None:
        """THE critical one, and it refutes this file's own docstring.

        The append/extend exemption reasoned that those only add to the END, so
        the program and verb survive and the literal stays safe to classify.
        True -- and incomplete. For a dual-form verb the SUB-verb decides:
        `is_mutating_git(["git","remote"])` is False but
        `is_mutating_git(["git","remote","add",...])` is True. So the exempted
        append is exactly where the mutation arrives, and `git remote` is an
        allowlisted read in lifespan.py. This is the house idiom --
        storage/shared/git_backend.py:481 is literally `cmd = ["git","remote"]`
        followed by a conditional append.
        """
        for rel, head, tail in (
            ("server/startup/lifespan.py", '["git", "remote"]', '["add", n, u]'),
            (
                "server/services/git_operations_service.py",
                '["git", "config"]',
                '["--unset", k]',
            ),
            (
                "server/repositories/activated_repo_manager.py",
                '["git", "branch"]',
                '["-D", n]',
            ),
        ):
            src = (
                "import subprocess\n"
                "def f(n, u, k):\n"
                f"    cmd = {head}\n"
                f"    cmd.extend({tail})\n"
                "    subprocess.run(cmd)\n"
            )
            v, u_ = _scan_source(rel, src)
            assert v or u_, f"{rel}: mutating sub-verb smuggled past {head}"

    def test_append_still_trusts_an_unambiguous_read(self) -> None:
        """The exemption exists for a reason and must survive: a verb with no
        mutating form cannot be turned into one by appending."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    cmd = ["git", "status"]\n'
            '    cmd.append("--short")\n'
            "    subprocess.run(cmd)\n"
        )
        _, u = _scan_source("server/repositories/activated_repo_manager.py", src)
        assert not u, f"an unambiguous read was made unreadable: {u}"

    def test_keyword_argv_is_scanned(self) -> None:
        """`if not node.args: return None, None` skipped the entire call.
        `args=` is ordinary Python and appears in governed code."""
        src = (
            "import subprocess\n"
            "def f():\n"
            '    subprocess.run(args=["git", "push", "origin", "main"], check=True)\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert v or u

    def test_spawn_is_recognised_by_function_not_receiver(self) -> None:
        """`_call_kind` required a receiver literally named `subprocess`, and
        the alias map was built from ImportFrom only -- so `import subprocess
        as sp` was invisible, as was a stashed `self._sp`."""
        for name, src in {
            "import-as": (
                "import subprocess as sp\n"
                "def f():\n"
                '    sp.run(["git", "push", "origin", "main"])\n'
            ),
            # An unidentified receiver is scanned, but only a LITERAL git argv
            # is reported there -- see _call_kind. Reporting unreadable argv on
            # any receiver named `run` flagged every `asyncio.run(coro)` in the
            # tree, which is the cry-wolf failure that gets a guard deleted.
            "attribute-receiver": (
                "class C:\n"
                "    def f(self):\n"
                '        self._sp.run(["git", "push", "origin", "main"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: spawn not recognised"

    def test_dynamic_program_is_unreadable_not_invisible(self) -> None:
        """The guard only failed closed AFTER proving argv[0] was literally
        "git". A dynamic verb was caught; a dynamic PROGRAM was silently
        skipped, because `argv[0] != "git"` is indistinguishable from "not a
        git call". diagnostics_service.py:631 has this shape today."""
        for name, src in {
            "list": (
                "import subprocess\n"
                "def f(p):\n"
                '    subprocess.run([p, "push", "origin", "main"])\n'
            ),
            "varargs": (
                "import asyncio\n"
                "async def f(p):\n"
                '    await asyncio.create_subprocess_exec(p, "push", "origin")\n'
            ),
            "hoisted-const": (
                "import subprocess\n"
                'GIT = "git"\n'
                "def f():\n"
                '    subprocess.run([GIT, "push", "origin", "main"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: dynamic program silently skipped"

    def test_starred_and_inline_expression_argv_are_unreadable(self) -> None:
        """Binding the expression to a name was reported; passing the SAME
        expression inline was not. Deleting one line made a bypass invisible."""
        for name, src in {
            "starred": (
                "import subprocess\n"
                "def f():\n"
                '    a = (["git", "push", "origin", "main"],)\n'
                "    subprocess.run(*a)\n"
            ),
            "inline-concat": (
                "import subprocess\n"
                'BASE = ["git"]\n'
                "def f():\n"
                '    subprocess.run(BASE + ["push", "origin", "main"])\n'
            ),
            "inline-subscript": (
                'import subprocess\ndef f(cmds):\n    subprocess.run(cmds["push"])\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: inline argv silently skipped"

    def test_shell_and_other_spawn_families_are_seen(self) -> None:
        """A string argv and the os.* spawn family were entirely unknown."""
        for name, src in {
            "shell-string": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run("git push origin main", shell=True)\n'
            ),
            "sh-c": (
                "import subprocess\n"
                "def f():\n"
                '    subprocess.run(["sh", "-c", "git push origin main"])\n'
            ),
            "os-system": (
                'import os\ndef f():\n    os.system("git push origin main")\n'
            ),
        }.items():
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"{name}: spawn family not seen"


class TestGuardDoesNotCryWolf:
    """A guard that reports a command a line cannot run gets disabled.

    These are the cost of the round-5 severity ordering: it picks the worst
    candidate reaching a call with no notion of which assignments actually
    DOMINATE that call.
    """

    def test_a_mutation_in_one_branch_does_not_taint_the_other(self) -> None:
        src = (
            "import subprocess\n"
            "def f(force):\n"
            "    if force:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "        subprocess.run(cmd)\n"
            "    else:\n"
            '        cmd = ["tar", "xf", "a.tar"]\n'
            "        subprocess.run(cmd)\n"
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert len(v) == 1, f"the tar call was blamed for the push: {v}"

    def test_a_nested_def_does_not_poison_the_outer_call(self) -> None:
        """`_prefix_rewritten_after` still uses ast.walk, so it descends into
        nested defs -- the exact leak `_statements_of` was written to stop."""
        src = (
            "import subprocess\n"
            "def f(repo):\n"
            '    cmd = ["git", "rev-parse", "HEAD"]\n'
            "    def helper():\n"
            '        inner = ["a", "b"]\n'
            '        inner.insert(0, "c")\n'
            "    subprocess.run(cmd)\n"
        )
        _, u = _scan_source("server/routers/git.py", src)
        assert not u, f"an unrelated nested local poisoned the outer call: {u}"


def test_scan_is_live() -> None:
    """Liveness control for `_scan()` itself.

    Mutation testing found that making `_scan()` return ``([], [])`` -- the
    guard going completely blind -- left all 26 self-tests passing, because the
    two production assertions are vacuous while the tree is clean. Nothing
    proved the live scan path still executed. This asserts it does real work.
    """
    files = _governed_files()
    assert len(files) > 50, f"the governed tree should be substantial, got {len(files)}"
    scanned = 0
    for path in files:
        rel = _TREE.key(path)
        _scan_source(rel, path.read_text(encoding="utf-8"))
        scanned += 1
    assert scanned == len(files)
    # and the production path must still classify a known bypass
    v, _ = _scan_source(
        "server/services/synthetic.py",
        'import subprocess\ndef f():\n    subprocess.run(["git", "push", "o", "m"])\n',
    )
    assert any("MUTATING: git push" in x for x in v), "the scan path is not classifying"


class TestRemainingBypassClasses:
    """The tail an adversarial pass found after the fail-closed rewrite."""

    def test_partial_bound_argv_is_seen(self) -> None:
        """`functools.partial(subprocess.run, argv)` defers the spawn but binds
        argv now. The call whose func is `partial` is not itself a spawn, so the
        site was invisible -- and `partial` appears 35x in governed roots."""
        for src in (
            "import functools, subprocess\n"
            "def f():\n"
            '    r = functools.partial(subprocess.run, ["git", "push", "o", "m"])\n'
            "    r()\n",
            "from functools import partial\n"
            "import subprocess\n"
            "def f():\n"
            '    r = partial(subprocess.run, ["git", "push", "o", "m"])\n'
            "    r()\n",
        ):
            v, u = _scan_source("server/services/synthetic.py", src)
            assert v or u, f"partial-bound argv not seen:\n{src}"

    def test_star_import_binds_the_spawn(self) -> None:
        """`from subprocess import *` binds `run` with no alias to record."""
        src = (
            "from subprocess import *\n"
            "def f():\n"
            '    run(["git", "push", "origin", "main"])\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert v or u

    def test_an_assignment_later_in_the_loop_reaches_the_call(self) -> None:
        """A loop back-edge makes a textually-later assignment reachable.

        Resolution stops at `line >= call.lineno`, which is sound for straight
        line code and wrong inside a loop: on iteration 2 the call runs the argv
        assigned at the BOTTOM of iteration 1. Reported as the harmless
        `rev-parse` while the push was never mentioned.
        """
        src = (
            "import subprocess\n"
            "def f(items):\n"
            '    cmd = ["git", "rev-parse", "HEAD"]\n'
            "    for i in items:\n"
            "        subprocess.run(cmd)\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert any("push" in x for x in v + u), f"loop back-edge missed: {v + u}"

    def test_a_statically_dead_branch_is_not_reported(self) -> None:
        """`if False:` cannot run, so blaming its contents names a command the
        program will never execute."""
        src = (
            "import subprocess\n"
            "def f():\n"
            "    if False:\n"
            '        cmd = ["git", "push", "origin", "main"]\n'
            "    else:\n"
            '        cmd = ["cidx", "index"]\n'
            "    subprocess.run(cmd)\n"
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not v and not u, f"dead branch reported: {v + u}"


class TestTheFenceIsReachabilityNotDirectory:
    """What the seam governs is what the SERVER can reach, not two directories.

    `_GOVERNED` named `server/` and `global_repos/`, so a raw mutating git call
    one directory outside was invisible with no allowlist entry needed --
    `services/git_topology_service.py` is imported by
    `server/mcp/handlers/repos.py` and was never scanned. A directory boundary
    pretending to be a reachability boundary is defeated by moving a file.
    """

    def test_a_module_reachable_only_by_import_is_governed(self) -> None:
        governed = {_TREE.key(p) for p in _governed_files()}
        assert "services/git_topology_service.py" in governed, (
            "a module the server imports must be scanned, wherever it lives"
        )

    def test_client_only_modules_stay_out_of_scope(self) -> None:
        """The CLI runs on a developer's machine with their own credentials, so
        client-side git is deliberately not the seam's business. Widening the
        fence to the whole package would bury real findings."""
        governed = {_TREE.key(p) for p in _governed_files()}
        assert "sync/conflict_resolution.py" not in governed

    def test_exempt_dirs_match_a_path_prefix_not_any_component(self) -> None:
        """`_EXEMPT_DIRS` matched ANY path component, so a future
        `server/repositories/scripts/` would be exempt for free."""
        assert not _is_exempt("server/repositories/scripts/thing.py")
        assert _is_exempt("scripts/thing.py")


class TestSelfUpdateMutationsAreJustifiedPerCall:
    """auto_update's git calls are exempt per CALL, not per directory.

    They operate on the server's OWN source tree and on hnswlib -- not on a
    tenant golden repo -- so git-mcp, which exists to hold golden-repo
    credentials, is the wrong destination for them. That is a real
    justification, and it belongs against each call so a NEW mutating call in
    the same directory still fails until someone justifies it too.
    """

    def test_every_self_update_entry_is_live(self) -> None:
        live = set()
        for path in _governed_files():
            rel = _TREE.key(path)
            violations, _ = _scan_source(
                rel, path.read_text(encoding="utf-8"), apply_self_update_allowlist=False
            )
            for v in violations:
                for arel, averb in ALLOWED_SELF_UPDATE_MUTATIONS:
                    if v.startswith(f"{arel}:") and v.endswith(f"git {averb}"):
                        live.add((arel, averb))
        stale = ALLOWED_SELF_UPDATE_MUTATIONS - live
        assert not stale, f"stale self-update exemptions: {sorted(map(str, stale))}"

    def test_an_unjustified_mutation_in_the_same_dir_still_fails(self) -> None:
        src = (
            "import subprocess\n"
            "def f():\n"
            '    subprocess.run(["git", "push", "origin", "main"])\n'
        )
        v, _ = _scan_source("server/auto_update/deployment_executor.py", src)
        assert any("MUTATING: git push" in x for x in v), v


class TestLiteralHeadConcatenationIsReadable:
    """`["git", "log", ...] + files` carries its verb in the literal head.

    Reporting it as opaque pushed real, classifiable reads into
    ALLOWED_UNREADABLE_ARGV, where they stop being checked against the
    classifier at all -- the allowlist is keyed on the BUILDER, so an entry for
    `<concatenation>` in a file would also cover a future
    `["git","push"] + args` in it. Classifying is strictly safer than exempting.
    """

    def test_a_concatenated_read_is_classified_not_exempted(self) -> None:
        src = (
            "import subprocess\n"
            "def f(files):\n"
            '    subprocess.run(["git", "log", "--format=%H", "--"] + files)\n'
        )
        v, u = _scan_source("server/services/synthetic.py", src)
        assert not u, f"should be readable, not opaque: {u}"
        assert any("unlisted read: git log" in x for x in v), v

    def test_a_concatenated_mutation_is_caught(self) -> None:
        src = (
            "import subprocess\n"
            "def f(args):\n"
            '    subprocess.run(["git", "push"] + args)\n'
        )
        v, _ = _scan_source("server/services/synthetic.py", src)
        assert any("MUTATING: git push" in x for x in v), v

    def test_a_dual_form_head_stays_opaque(self) -> None:
        """The appended tail is exactly where `git remote`'s sub-verb arrives,
        so the same rule that governs append/extend must govern `+`."""
        src = (
            "import subprocess\n"
            "def f(args):\n"
            '    subprocess.run(["git", "remote"] + args)\n'
        )
        v, u = _scan_source("server/startup/lifespan.py", src)
        assert v or u, "a dual-form verb completed by the tail must not read as clean"
        assert not any("unlisted read" in x for x in v), v
