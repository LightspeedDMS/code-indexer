"""Bug #1891 round 2 (S4): malformed paths must map to PermissionError, not a
raw exception.

``code_indexer.utils.path_confinement.resolve_confined_path`` (the single,
shared confinement primitive introduced in round 1; moved out of
``FileListingService`` in round 3 D2 -- no re-export shim) resolves the
caller-supplied path via ``Path.resolve()``. Three caller-supplied inputs
cause that call -- or the downstream ``exists()``/``is_file()``/``open()`` a
caller is documented to perform right after it -- to raise something other
than the documented ``PermissionError``:

- An embedded NUL byte (e.g. ``"src/a.py\\x00evil"``) makes ``Path.resolve()``
  raise ``ValueError: embedded null byte``.
- A symlink loop inside the repository makes ``Path.resolve()`` raise
  ``RuntimeError: Symlink loop from ...`` (observed on this interpreter;
  older/newer CPython may instead raise ``OSError`` -- both are covered by
  the same broad catch).
- An overlong path component (longer than the filesystem's NAME_MAX) does
  NOT make ``Path.resolve()`` itself raise (verified empirically: resolve()
  silently defers the error) -- it is the caller's very next
  ``exists()``/``is_file()``/``open()``/``stat()`` call that raises
  ``OSError: [Errno 36] File name too long``. Because every caller of
  ``resolve_confined_path`` performs one of those calls immediately
  afterward (that is the documented contract), the only way to prevent an
  uncaught ``OSError`` from ever reaching a caller is for
  ``resolve_confined_path`` to reject an overlong component BEFORE it ever
  reaches a stat/lstat syscall.

Before the fix, all three cases let the raw exception propagate out of
``resolve_confined_path`` (and, transitively, out of every REST/MCP/CRUD
front door that calls it), producing an uncaught-exception 500 instead of
the same 4xx/PermissionError contract every caller already handles for an
ordinary out-of-repo escape attempt.

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- no mocks.
"""

import os

import pytest

from code_indexer.utils.path_confinement import resolve_confined_path

# Linux NAME_MAX is 255 bytes; comfortably exceed it to trigger ENAMETOOLONG.
OVERLONG_COMPONENT_LENGTH = 5000
# Comfortably under NAME_MAX -- must resolve normally.
NORMAL_COMPONENT_LENGTH = 100


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('hello')")
    return root


class TestNulByteRaisesPermissionErrorNotValueError:
    def test_nul_byte_in_relative_path_raises_permission_error(self, repo_root):
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "src/a.py\x00evil")

    def test_discrimination_proof_raw_resolve_raises_value_error(self, repo_root):
        """Proves the malicious input genuinely triggers ValueError from a
        bare (unconfined) resolve() call -- demonstrating the fix's catch is
        discriminating, not a no-op."""
        from pathlib import Path

        with pytest.raises(ValueError):
            (Path(repo_root) / "src/a.py\x00evil").resolve()


class TestSymlinkLoopRaisesPermissionErrorNotRuntimeError:
    def test_symlink_loop_raises_permission_error(self, repo_root):
        loop_a = repo_root / "loop_a"
        loop_b = repo_root / "loop_b"
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)

        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, "loop_a")

    def test_discrimination_proof_raw_resolve_raises(self, repo_root):
        """Proves the symlink loop genuinely triggers an exception from a
        bare (unconfined) resolve() call."""
        from pathlib import Path

        loop_a = repo_root / "loop_a2"
        loop_b = repo_root / "loop_b2"
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)

        with pytest.raises((RuntimeError, OSError)):
            (Path(repo_root) / "loop_a2").resolve()


class TestOverlongComponentRaisesPermissionErrorNotOSError:
    def test_overlong_path_component_raises_permission_error(self, repo_root):
        overlong_name = "a" * OVERLONG_COMPONENT_LENGTH
        with pytest.raises(PermissionError):
            resolve_confined_path(repo_root, overlong_name)

    def test_discrimination_proof_raw_exists_raises_oserror(self, repo_root):
        """Proves an overlong component genuinely triggers OSError from the
        very next filesystem call every caller performs immediately after
        resolve() -- demonstrating why resolve_confined_path must reject it
        proactively rather than relying on catching resolve() alone
        (resolve() itself does not raise for this input). Uses os.stat()
        directly rather than Path.exists() since exists() is documented to
        swallow some OSError subclasses on certain platforms/versions --
        os.stat() unambiguously propagates ENAMETOOLONG."""
        from pathlib import Path

        overlong_name = "a" * OVERLONG_COMPONENT_LENGTH
        candidate = (Path(repo_root) / overlong_name).resolve()
        with pytest.raises(OSError):
            os.stat(str(candidate))


class TestLegitimatePathsStillResolve:
    def test_legitimate_deeply_nested_path_still_resolves(self, repo_root):
        deep_dir = repo_root / "a" / "b" / "c" / "d" / "e"
        deep_dir.mkdir(parents=True)
        (deep_dir / "leaf.py").write_text("deep")

        result = resolve_confined_path(repo_root, "a/b/c/d/e/leaf.py")
        assert result == (deep_dir / "leaf.py").resolve()
        assert result.read_text() == "deep"

    def test_legitimate_normal_length_component_still_resolves(self, repo_root):
        normal_name = "b" * NORMAL_COMPONENT_LENGTH
        (repo_root / normal_name).write_text("ok")
        result = resolve_confined_path(repo_root, normal_name)
        assert result.exists()
