"""Blob hashes are computed in-process and match git exactly.

The hash seeds the deterministic point UUID for every indexed chunk, so a drift
here silently re-identifies chunks rather than merely invalidating them. These
tests pin the equivalence against real git, and pin the two call sites to the
helper so the subprocess cannot creep back in.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from code_indexer.utils.blob_hash import git_blob_hash, git_blob_hashes

CONTENTS = {
    "empty": b"",
    "ascii": b"hello world\n",
    "no_trailing_newline": b"no newline at eof",
    "lf": b"a\nb\nc\n",
    "crlf": b"a\r\nb\r\n",
    "binary": bytes(range(256)) * 40,
    "unicode": "café — naïve \U0001f600\n".encode("utf-8"),
    "nulls": b"\x00\x00\x00binary-ish\x00",
    "large": b"x" * (3 * 1024 * 1024 + 7),  # crosses the 1 MiB read chunk
}

git_required = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


@git_required
@pytest.mark.parametrize("name", sorted(CONTENTS))
def test_matches_git_hash_object(tmp_path: Path, name: str) -> None:
    """Our hash equals `git hash-object --no-filters` byte for byte."""
    target = tmp_path / name
    target.write_bytes(CONTENTS[name])
    expected = subprocess.run(
        ["git", "hash-object", "--no-filters", str(target)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert git_blob_hash(target) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        # Fixed vectors, so equivalence is still pinned on a host without git.
        (b"", "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"),
        (b"hello world\n", "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"),
        (b"a\r\nb\r\n", "c30dea8a3641ea99b125d04d599d843712292759"),
    ],
)
def test_known_vectors(tmp_path: Path, payload: bytes, expected: str) -> None:
    f = tmp_path / "f"
    f.write_bytes(payload)
    assert git_blob_hash(f) == expected


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert git_blob_hash(tmp_path / "nope") is None


def test_directory_returns_none(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    assert git_blob_hash(tmp_path / "d") is None


def test_batch_keys_by_given_path_and_honours_root(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"A")
    (tmp_path / "sub" / "b.txt").write_bytes(b"B")
    out = git_blob_hashes(["sub/a.txt", "sub/b.txt"], root=tmp_path)
    assert set(out) == {"sub/a.txt", "sub/b.txt"}
    assert out["sub/a.txt"] == git_blob_hash(tmp_path / "sub" / "a.txt")
    assert out["sub/a.txt"] != out["sub/b.txt"]


def test_batch_omits_unreadable_without_discarding_the_batch(tmp_path: Path) -> None:
    """The old subprocess dropped EVERY hash when one entry misbehaved."""
    (tmp_path / "good.txt").write_bytes(b"good")
    out = git_blob_hashes(["good.txt", "missing.txt"], root=tmp_path)
    assert set(out) == {"good.txt"}


def test_batch_empty_input() -> None:
    assert git_blob_hashes([]) == {}


# --- wiring: the call sites must USE the helper, not re-grow a subprocess ---


def _calls_in(path: str, func_name: str) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
    raise AssertionError(f"{func_name} not found in {path}")


def test_file_identifier_metadata_uses_the_helper() -> None:
    calls = _calls_in(
        "src/code_indexer/services/file_identifier.py", "_get_git_metadata"
    )
    assert "git_blob_hash" in calls


def test_topology_batch_uses_the_helper() -> None:
    calls = _calls_in(
        "src/code_indexer/services/git_topology_service.py", "_batch_git_hash_object"
    )
    assert "git_blob_hashes" in calls


@pytest.mark.parametrize(
    "module",
    [
        "src/code_indexer/services/file_identifier.py",
        "src/code_indexer/services/git_topology_service.py",
    ],
)
def test_no_raw_hash_object_subprocess_remains(module: str) -> None:
    """Guard the regression: no `git hash-object` argv list in these modules."""
    tree = ast.parse(Path(module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == "git":
                rest = [
                    e.value
                    for e in node.elts[1:]
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                assert "hash-object" not in rest, f"{module} re-grew a hash-object exec"
