"""Tests for scripts/check_doc_references.py (Story #2079).

Every test builds a REAL temporary git repository (git init, add, commit) and
runs the checker against it -- no mocks. The checker only reads git-tracked
files, so the repositories must be committed for the files to count.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "check_doc_references.py"
_GIT_TIMEOUT_SECONDS = 30
_SCRIPT_TIMEOUT_SECONDS = 60


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_references", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_doc_references"] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _make_repo(
    tmp_path: Path,
    tracked: Dict[str, str],
    untracked: Optional[Dict[str, str]] = None,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Tester")
    for rel, content in tracked.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    for rel, content in (untracked or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


def _run_cli(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--root", str(repo)],
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TIMEOUT_SECONDS,
    )


def _rendered_hits(repo: Path, allowlist: Optional[Dict[str, str]] = None) -> List[str]:
    checker = _load_checker()
    if allowlist is None:
        hits = checker.find_broken_references(repo)
    else:
        hits = checker.find_broken_references(repo, allowlist=allowlist)
    return [f"{h.source}:{h.line} -> {h.target}" for h in hits]


# --------------------------------------------------------------------------
# Check 1: Markdown links
# --------------------------------------------------------------------------


def test_dangling_markdown_link_fails_with_file_and_line(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {"README.md": "# Title\n\nSee [the guide](docs/missing-guide.md).\n"},
    )
    result = _run_cli(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "README.md:3 -> docs/missing-guide.md" in result.stdout


def test_valid_links_pass(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": "[guide](docs/guide.md) and [folder](docs/) "
            "and ![logo](images/logo.png)\n",
            "docs/guide.md": "Back to [readme](../README.md) and [sib](./other.md)\n",
            "docs/other.md": "[root dir](..)\n",
            "images/logo.png": "png",
        },
    )
    result = _run_cli(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _rendered_hits(repo) == []


def test_link_resolves_relative_to_containing_file_not_repo_root(
    tmp_path: Path,
) -> None:
    # docs/a.md links to "b.md": valid only as docs/b.md, so a root-level
    # b.md must NOT satisfy it.
    repo = _make_repo(
        tmp_path,
        {"docs/a.md": "[b](b.md)\n", "b.md": "root b\n"},
    )
    assert _rendered_hits(repo) == ["docs/a.md:1 -> b.md"]


def test_links_inside_fenced_code_and_inline_code_are_ignored(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "Intro\n"
                "```text\n"
                "![alt](diagram/missing.png)\n"
                "[ref]: fenced/missing.md\n"
                "```\n"
                "~~~python\n"
                "open('/tmp/x','w')  # [a](tilde/missing.md)\n"
                "~~~\n"
                "Inline `[x](inline/missing.md)` code span.\n"
                "After fence [real](real/missing.md)\n"
            )
        },
    )
    assert _rendered_hits(repo) == ["README.md:10 -> real/missing.md"]


def test_fragment_and_query_are_stripped(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "[s](docs/guide.md#section) [q](docs/guide.md?plain=1) "
                "[a](#local-anchor) [w](https://example.com/x.md) "
                "[m](mailto:someone@example.com)\n"
                "[bad](docs/absent.md#section)\n"
            ),
            "docs/guide.md": "guide\n",
        },
    )
    assert _rendered_hits(repo) == ["README.md:2 -> docs/absent.md#section"]


def test_reference_definitions_are_checked(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "Use [the guide][g] and [gone][x].\n"
                "\n"
                "[g]: docs/guide.md\n"
                '[x]: docs/gone.md "Title"\n'
                "[^1]: a footnote, not a link\n"
            ),
            "docs/guide.md": "guide\n",
        },
    )
    assert _rendered_hits(repo) == ["README.md:4 -> docs/gone.md"]


def test_docs_paths_in_markdown_prose_are_checked(tmp_path: Path) -> None:
    # Prose doc paths are scanned over the RAW text: inline code and fences
    # included, because a docs move must update them too.
    repo = _make_repo(
        tmp_path,
        {
            "docs/a.md": (
                "See `docs/gone.md` and `docs/present.md`.\n"
                "Plain text docs/plain-gone.md here.\n"
                "```bash\n"
                "cat docs/fenced-gone.md\n"
                "```\n"
            ),
            "docs/present.md": "present\n",
        },
    )
    assert _rendered_hits(repo) == [
        "docs/a.md:1 -> docs/gone.md",
        "docs/a.md:2 -> docs/plain-gone.md",
        "docs/a.md:4 -> docs/fenced-gone.md",
    ]


def test_broken_link_also_seen_by_prose_scan_is_reported_once(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"README.md": "[x](docs/absent.md#part)\n"})
    assert _rendered_hits(repo) == ["README.md:1 -> docs/absent.md#part"]


def test_prose_scan_skips_changelog_plans_and_reports(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "CHANGELOG.md": "Moved docs/old-name.md.\n[x](missing.md)\n",
            "plans/designs/plan.md": "Source: `docs/old-name.md`\n",
            "reports/bugs/r.md": "Was docs/old-name.md\n",
        },
    )
    # Historical records keep old paths in prose; their LINKS are still checked.
    assert _rendered_hits(repo) == ["CHANGELOG.md:2 -> missing.md"]


# --------------------------------------------------------------------------
# Check 2: code-side doc paths
# --------------------------------------------------------------------------


def test_dangling_docs_path_string_in_src_python_fails(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "src/pkg/mod.py": (
                "OK = 'docs/present.md'\n"
                "HELP = 'See docs/server/missing-guide.md for details'\n"
            ),
            "docs/present.md": "present\n",
        },
    )
    result = _run_cli(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/pkg/mod.py:2 -> docs/server/missing-guide.md" in result.stdout


def test_docs_paths_checked_only_in_scoped_files(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            # Out of scope: arbitrary root files and non-docs prefixes.
            "notes.txt": "docs/not-checked.md\n",
            "src/a.py": (
                "x = 'tests/fixtures/docs/nested.md'\n"
                "y = 'https://example.com/org/repo/blob/master/docs/remote.md'\n"
                "z = 'README.md'\n"
            ),
            # In scope: workflow YAML, root shell script, pyproject, CLAUDE.md.
            ".github/workflows/ci.yml": "run: cat docs/ci-missing.md\n",
            "build.sh": "echo docs/sh-missing.md\n",
            "pyproject.toml": 'readme = "docs/toml-missing.md"\n',
            "CLAUDE.md": "Detail in docs/claude-missing.md\n",
            "tools/t.py": "T = 'docs/xray-templates/missing-template.rs'\n",
        },
    )
    assert sorted(_rendered_hits(repo)) == sorted(
        [
            ".github/workflows/ci.yml:1 -> docs/ci-missing.md",
            "build.sh:1 -> docs/sh-missing.md",
            "pyproject.toml:1 -> docs/toml-missing.md",
            "CLAUDE.md:1 -> docs/claude-missing.md",
            "tools/t.py:1 -> docs/xray-templates/missing-template.rs",
        ]
    )


def test_dangling_include_str_fails(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "rust/crate/src/lib.rs": (
                "const A: &str = include_str!(\n"
                '    "../../../docs/templates/gone.rs"\n'
                ");\n"
            ),
        },
    )
    result = _run_cli(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "rust/crate/src/lib.rs:2 -> ../../../docs/templates/gone.rs" in result.stdout


def test_valid_include_str_passes(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "rust/crate/src/lib.rs": (
                'const A: &str = include_str!("../../../docs/templates/t.rs");\n'
                'const B: &str = include_str!("sibling.rs");\n'
            ),
            "rust/crate/src/sibling.rs": "// sibling\n",
            "docs/templates/t.rs": "// template\n",
        },
    )
    result = _run_cli(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _rendered_hits(repo) == []


# --------------------------------------------------------------------------
# Scope rules: allowlist and tracked-only
# --------------------------------------------------------------------------


def test_allowlisted_fixture_file_is_ignored(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "tests/test_fixture_repo.py": "FAKE = 'docs/edge-cases/fake.md'\n",
            "tests/test_other.py": "REAL = 'docs/really-missing.md'\n",
        },
    )
    allowlist = {"tests/test_fixture_repo.py": "builds a synthetic repo"}
    assert _rendered_hits(repo, allowlist=allowlist) == [
        "tests/test_other.py:1 -> docs/really-missing.md"
    ]
    # Without the allowlist the fixture file is reported too: the exclusion
    # is by file path, not by value.
    assert len(_rendered_hits(repo, allowlist={})) == 2


def test_untracked_files_are_ignored(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {"README.md": "clean\n"},
        untracked={
            "scratch.md": "[x](nowhere.md)\n",
            "src/new.py": "P = 'docs/nowhere.md'\n",
        },
    )
    result = _run_cli(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_targets_must_be_tracked_not_merely_present(tmp_path: Path) -> None:
    # CI checks out only tracked files: an untracked/gitignored target that
    # exists locally is still a broken reference.
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "[t](docs/tracked.md) [d](docs/) [u](local/untracked.md) "
                "[ud](local/)\n"
                "Prose docs/untracked-doc.md\n"
            ),
            "docs/tracked.md": "tracked\n",
        },
        untracked={
            "local/untracked.md": "local only\n",
            "docs/untracked-doc.md": "local only\n",
        },
    )
    assert _rendered_hits(repo) == [
        "README.md:1 -> local/",
        "README.md:1 -> local/untracked.md",
        "README.md:2 -> docs/untracked-doc.md",
    ]


def test_tracked_target_deleted_from_working_tree_is_broken(tmp_path: Path) -> None:
    # A plain `mv` before `git add`: the old path is still in the index but
    # the reference already dangles.
    repo = _make_repo(
        tmp_path,
        {
            "README.md": "[m](docs/moved.md)\nProse docs/moved.md\n",
            "docs/moved.md": "x\n",
        },
    )
    (repo / "docs" / "moved.md").unlink()
    assert _rendered_hits(repo) == [
        "README.md:1 -> docs/moved.md",
        "README.md:2 -> docs/moved.md",
    ]


def test_link_escaping_repository_root_is_broken(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"README.md": "[out](../outside.md)\n"})
    (tmp_path / "outside.md").write_text("exists outside the repo\n")
    assert _rendered_hits(repo) == ["README.md:1 -> ../outside.md"]


def test_leading_slash_link_resolves_from_repo_root(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {"docs/deep/a.md": "[r](/README.md) [x](/docs/nope.md)\n", "README.md": "r\n"},
    )
    assert _rendered_hits(repo) == ["docs/deep/a.md:1 -> /docs/nope.md"]


def test_angle_bracket_and_percent_encoded_targets_resolve(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "[a](<docs/my guide.md>) [b](docs/my%20guide.md) "
                '[c](docs/my%20guide.md "Title") [d](<docs/no such.md>)\n'
            ),
            "docs/my guide.md": "spaced\n",
        },
    )
    assert _rendered_hits(repo) == ["README.md:1 -> docs/no such.md"]


def test_binary_and_deleted_tracked_files_are_skipped(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "src/blob.bin": "\0binary docs/inside-binary.md\n",
            "src/deleted.py": "D = 'docs/deleted-ref.md'\n",
            "src/kept.py": "K = 'docs/kept-ref.md'\n",
        },
    )
    (repo / "src" / "deleted.py").unlink()  # still tracked, gone from disk
    assert _rendered_hits(repo) == ["src/kept.py:1 -> docs/kept-ref.md"]


def _commit_raw_named_file(repo: Path, raw_name: bytes, content: bytes) -> None:
    raw_path = os.path.join(os.fsencode(str(repo)), raw_name)
    with open(raw_path, "wb") as handle:
        handle.write(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "raw name")


def test_non_utf8_tracked_filename_is_reported_not_crashed(tmp_path: Path) -> None:
    # Exit 1 must only ever mean "broken references", never a traceback.
    clean = _make_repo(tmp_path / "clean", {"README.md": "clean\n"})
    _commit_raw_named_file(clean, b"caf\xe9.md", b"no references\n")
    result = _run_cli(clean)
    assert result.returncode == 0, result.stdout + result.stderr

    broken = _make_repo(tmp_path / "broken", {"README.md": "clean\n"})
    _commit_raw_named_file(broken, b"caf\xe9.md", b"See docs/nope.md\n")
    result = _run_cli(broken)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".md:1 -> docs/nope.md" in result.stdout


def test_main_in_process_exit_codes_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    checker = _load_checker()
    clean = _make_repo(tmp_path / "clean", {"README.md": "clean\n"})
    assert checker.main(["--root", str(clean)]) == 0
    assert "no broken references" in capsys.readouterr().out

    broken = _make_repo(tmp_path / "broken", {"README.md": "[x](gone.md)\n"})
    assert checker.main(["--root", str(broken)]) == 1
    out = capsys.readouterr().out
    assert "1 broken reference(s)" in out
    assert "README.md:1 -> gone.md" in out

    not_git = tmp_path / "not-git"
    not_git.mkdir()
    assert checker.main(["--root", str(not_git)]) == 2
    assert "git ls-files failed" in capsys.readouterr().err


def test_cli_rejects_non_git_root(tmp_path: Path) -> None:
    result = _run_cli(tmp_path)
    assert result.returncode == 2
    assert "git ls-files" in result.stderr


@pytest.mark.parametrize("entry", sorted(_load_checker().FIXTURE_ALLOWLIST))
def test_every_allowlist_entry_is_a_tracked_file_in_this_repo(entry: str) -> None:
    # A stale allowlist entry (file moved/deleted) must be removed, not kept.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", entry],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    assert tracked.returncode == 0, f"{entry} is not tracked"


def test_checker_source_and_its_tests_are_allowlisted() -> None:
    # The checker names example doc paths in its docstrings/regex comments and
    # this test file builds synthetic repos full of fake docs/ paths; without
    # these entries the checker fails on a clean clone of its own repository.
    allowlist = _load_checker().FIXTURE_ALLOWLIST
    assert "scripts/check_doc_references.py" in allowlist
    assert "tests/unit/scripts/test_check_doc_references.py" in allowlist
