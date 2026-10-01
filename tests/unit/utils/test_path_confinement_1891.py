"""Bug #1891 round 3 (D2): dedicated tests for the dependency-free
``code_indexer.utils.path_confinement`` module.

This module was extracted from ``FileListingService.resolve_confined_path``
(a staticmethod, now removed with no re-export shim) so that low-layer /
CLI-path callers such as ``global_repos/directory_explorer.py`` no longer
pull the entire server layer (config_service, auto_update, etc. --
~106 ``code_indexer.server.*`` modules) into their import chain merely to
confine a path.

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- no
mocks.
"""

import os
import subprocess
import sys

import pytest

from code_indexer.utils.path_confinement import resolve_confined_path

OVERLONG_COMPONENT_LENGTH = 5000
NORMAL_COMPONENT_LENGTH = 100


def test_module_pulls_in_zero_server_modules():
    """Guards the whole point of D2: importing this module must never
    transitively import anything under code_indexer.server.*.

    Run in a FRESH subprocess (not against the current process's
    sys.modules) -- inside the pytest process itself, conftest.py, other
    test modules, or plugin collection may already have imported server
    modules for unrelated reasons, which would make an in-process check
    of sys.modules order-dependent and produce false failures/passes
    depending on test execution order.
    """
    src_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "import code_indexer.utils.path_confinement; "
            "server_mods = [m for m in sys.modules "
            "if m.startswith('code_indexer.server')]; "
            "print(len(server_mods))",
        ],
        env={**os.environ, "PYTHONPATH": src_root},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    server_module_count = int(result.stdout.strip())
    assert server_module_count == 0, (
        "path_confinement must be stdlib-only and never pull in the "
        f"server layer, found {server_module_count} server modules in a "
        f"fresh subprocess. stderr={result.stderr}"
    )


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('hello')")
    return root


class TestLegitimatePathsResolve:
    def test_nested_path_resolves_inside_repo(self, repo_root):
        result = resolve_confined_path(repo_root, "src/a.py")
        assert result == (repo_root / "src" / "a.py").resolve()
        assert result.read_text() == "print('hello')"

    def test_dot_resolves_to_repo_root(self, repo_root):
        result = resolve_confined_path(repo_root, ".")
        assert result == repo_root.resolve()


class TestEscapeAttemptsBlocked:
    def test_parent_traversal_rejected(self, repo_root):
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "../outside.txt")

    def test_absolute_path_rejected(self, repo_root, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, str(outside))

    def test_symlink_escape_rejected(self, repo_root, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        os.symlink(outside, repo_root / "link_outside")
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "link_outside")


class TestNulByteAndSymlinkLoopRaisePermissionError:
    def test_nul_byte_raises_permission_error(self, repo_root):
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "src/a.py\x00evil")

    def test_symlink_loop_raises_permission_error(self, repo_root):
        loop_a = repo_root / "loop_a"
        loop_b = repo_root / "loop_b"
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "loop_a")


class TestOverlongComponentHandling:
    def test_overlong_component_raises_permission_error(self, repo_root):
        overlong_name = "a" * OVERLONG_COMPONENT_LENGTH
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, overlong_name)

    def test_normal_length_component_still_resolves(self, repo_root):
        normal_name = "b" * NORMAL_COMPONENT_LENGTH
        (repo_root / normal_name).write_text("ok")
        result = resolve_confined_path(repo_root, normal_name)
        assert result.exists()


class TestLoneSurrogateAndNonStringInputRaisePermissionError:
    def test_lone_surrogate_path_raises_permission_error_not_unicode_error(
        self, repo_root
    ):
        """Round 3 P3: a lone UTF-16 surrogate code point (never valid in
        a real filesystem path) used to escape the try/except entirely --
        str.encode("utf-8", "surrogateescape") raises UnicodeEncodeError
        for an arbitrary lone surrogate (that handler only round-trips
        the specific U+DC80-U+DCFF range produced by decoding with it),
        and that loop ran OUTSIDE any try/except in the pre-fix code."""
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "\ud800")

    def test_non_string_relative_path_raises_permission_error_not_type_error(
        self, repo_root
    ):
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, None)  # type: ignore[arg-type]
