"""Tests for the shared reference-doc generator runner (Story #2082)."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

import pytest

from tools.docs import _runner


def _pages() -> Dict[str, str]:
    return {
        "README.md": "<!-- header -->\n# Index\n\nsee a.md\n",
        "a.md": "<!-- header -->\n# A\n\nline one\nline two\n",
    }


def _write(target: Path, pages: Dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        (target / name).write_text(text, encoding="utf-8")


def _run(argv, target: Path, pages=None) -> int:
    rendered = _pages() if pages is None else pages
    return _runner.run(
        argv, target_dir=target, render=lambda: rendered, command="regen-cmd"
    )


def test_write_mode_writes_every_page(tmp_path: Path) -> None:
    target = tmp_path / "ref"
    assert _run([], target) == 0
    assert sorted(p.name for p in target.iterdir()) == ["README.md", "a.md"]
    assert (target / "a.md").read_text(encoding="utf-8") == _pages()["a.md"]


def test_write_mode_removes_orphan_pages(tmp_path: Path) -> None:
    target = tmp_path / "ref"
    _write(target, {**_pages(), "old.md": "gone\n"})
    assert _run([], target) == 0
    assert not (target / "old.md").exists()


def test_write_mode_refuses_orphan_subdirectory(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    (target / "nested").mkdir(parents=True)
    assert _run([], target) == 2
    assert "nested" in capsys.readouterr().err
    assert not (target / "a.md").exists()


def test_check_passes_when_every_page_current(tmp_path: Path) -> None:
    target = tmp_path / "ref"
    _write(target, _pages())
    assert _run(["--check"], target) == 0


def test_check_fails_on_stale_page_with_diff_summary(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    stale = _pages()["a.md"].replace("line two", "line 2 edited by hand")
    _write(target, {**_pages(), "a.md": stale})
    assert _run(["--check"], target) == 1
    err = capsys.readouterr().err
    assert "stale: a.md" in err
    assert "README.md" not in err.replace("regen-cmd", "")
    assert "regen-cmd" in err
    assert "-line 2 edited by hand" in err
    assert "+line two" in err
    assert (target / "a.md").read_text(encoding="utf-8") == stale  # never rewrites


def test_check_fails_on_missing_page(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    _write(target, {"README.md": _pages()["README.md"]})
    assert _run(["--check"], target) == 1
    assert "missing: a.md" in capsys.readouterr().err
    assert not (target / "a.md").exists()


def test_check_fails_on_orphan_page(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    _write(target, {**_pages(), "orphan.md": "x\n"})
    assert _run(["--check"], target) == 1
    assert "orphan: orphan.md" in capsys.readouterr().err
    assert (target / "orphan.md").exists()


def test_check_fails_on_missing_directory(tmp_path: Path, capsys) -> None:
    target = tmp_path / "absent"
    assert _run(["--check"], target) == 1
    assert "missing" in capsys.readouterr().err
    assert not target.exists()


def test_check_diff_summary_is_bounded(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    junk = "".join(f"old {i}\n" for i in range(500))
    _write(target, {"README.md": junk, "a.md": junk})
    assert _run(["--check"], target) == 1
    assert len(capsys.readouterr().err.splitlines()) <= _runner.MAX_DIFF_LINES + 10


def test_page_over_line_limit_rejected_before_writing(tmp_path: Path, capsys) -> None:
    target = tmp_path / "ref"
    big = "x\n" * (_runner.MAX_PAGE_LINES + 1)
    assert _run([], target, pages={"README.md": "ok\n", "big.md": big}) == 2
    assert "big.md" in capsys.readouterr().err
    assert not target.exists()
    assert _runner.MAX_PAGE_LINES == 1000


def test_page_at_line_limit_accepted(tmp_path: Path) -> None:
    target = tmp_path / "ref"
    exact = "x\n" * _runner.MAX_PAGE_LINES
    assert _run([], target, pages={"README.md": exact}) == 0


def test_page_name_with_directory_rejected(tmp_path: Path, capsys) -> None:
    assert _run([], tmp_path / "ref", pages={"sub/a.md": "x\n"}) == 2
    assert "sub/a.md" in capsys.readouterr().err


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_page_names_rejected(tmp_path: Path, capsys, name: str) -> None:
    assert _run([], tmp_path / "ref", pages={name: "x\n"}) == 2
    assert repr(name) in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_render_value_error_is_rc_2_not_stale(tmp_path: Path, capsys, argv) -> None:
    def broken() -> Dict[str, str]:
        raise ValueError("registry is inconsistent")

    target = tmp_path / "ref"
    rc = _runner.run(argv, target_dir=target, render=broken, command="cmd")
    assert rc == 2
    assert "registry is inconsistent" in capsys.readouterr().err
    assert not target.exists()


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_symlinked_target_dir_refused(tmp_path: Path, capsys, argv) -> None:
    real = tmp_path / "elsewhere"
    _write(real, {**_pages(), "keep.md": "keep\n"})
    link = tmp_path / "ref"
    link.symlink_to(real, target_is_directory=True)
    assert _run(argv, link) == 2
    assert "symlink" in capsys.readouterr().err
    assert (real / "keep.md").read_text(encoding="utf-8") == "keep\n"


def test_importing_generators_loads_no_server_or_cli_modules() -> None:
    probe = (
        "import sys\n"
        "import tools.docs.cli_reference, tools.docs.mcp_tools, tools.docs.error_codes\n"
        "loaded = sorted(m for m in sys.modules\n"
        "    if m.startswith('code_indexer.server') or m == 'code_indexer.cli')\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_runner.REPO_ROOT,
        env=dict(os.environ, PYTHONPATH=str(_runner.REPO_ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_generated_header_names_the_command() -> None:
    header = _runner.generated_header("python3 -m tools.docs.example")
    assert header.startswith("<!-- ")
    assert header.endswith(" -->")
    assert "\n" not in header
    assert "`python3 -m tools.docs.example`" in header
    assert "generated" in header.lower()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a | b", "a \\| b"),
        ("first\nsecond\n\tthird", "first second third"),
        ("use <alias> here", "use &lt;alias&gt; here"),
        ("code `<alias>` stays", "code `<alias>` stays"),
        ("glob */tests/*", "glob \\*/tests/\\*"),
        ("regex \\s+", "regex \\\\s+"),
        ("code `a\\s*` stays", "code `a\\s*` stays"),
        ("  padded  ", "padded"),
        ("", ""),
    ],
)
def test_md_cell_escapes_table_breaking_text(raw: str, expected: str) -> None:
    assert _runner.md_cell(raw) == expected


def test_package_location_guard_accepts_this_tree(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "code_indexer" / "__init__.py"
    pkg.parent.mkdir(parents=True)
    pkg.write_text("", encoding="utf-8")
    _runner.check_package_location(pkg, tmp_path)


def test_package_location_guard_rejects_other_clone(tmp_path: Path) -> None:
    other = tmp_path / "other-clone" / "src" / "code_indexer" / "__init__.py"
    repo = tmp_path / "this-repo"
    repo.mkdir()
    with pytest.raises(_runner.WrongTreeError) as excinfo:
        _runner.check_package_location(other, repo)
    assert "PYTHONPATH" in str(excinfo.value)


def test_run_returns_2_when_imported_package_is_from_another_tree(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Point the guard at a repo root that does not contain the imported package.
    monkeypatch.setattr(_runner, "REPO_ROOT", tmp_path)
    target = tmp_path / "ref"
    assert _run([], target) == 2
    assert "PYTHONPATH" in capsys.readouterr().err
    assert not target.exists()
