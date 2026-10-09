"""Self-tests for the synthetic non-git trace repository generator."""

import subprocess
import time

import pytest

from synthetic_repo import add_files, generate_repo, list_repo_files


def _contents(root):
    return {p: (root / p).read_bytes() for p in list_repo_files(root)}


def test_generates_exact_count_spread_over_directories(tmp_path):
    root = tmp_path / "repo"
    paths = generate_repo(root, n_files=200, seed=7, files_per_dir=8)
    assert len(paths) == 200
    assert sorted(paths) == list_repo_files(root)
    dirs = {p.rsplit("/", 1)[0] for p in paths}
    assert 20 <= len(dirs) <= 30
    exts = {p.rsplit(".", 1)[1] for p in paths}
    assert exts == {"md", "json"}


def test_generation_is_deterministic_for_a_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_repo(a, n_files=50, seed=3)
    generate_repo(b, n_files=50, seed=3)
    assert _contents(a) == _contents(b)


def test_every_file_has_unique_content(tmp_path):
    root = tmp_path / "repo"
    generate_repo(root, n_files=120, seed=1)
    contents = list(_contents(root).values())
    assert len(set(contents)) == len(contents)


def test_add_files_adds_new_files_and_leaves_existing_untouched(tmp_path):
    root = tmp_path / "repo"
    generate_repo(root, n_files=40, seed=1)
    before = _contents(root)
    added = add_files(root, count=5, batch_tag="sync-001", seed=1)
    after = _contents(root)
    assert len(added) == 5
    assert set(added).isdisjoint(before)
    assert {p: after[p] for p in before} == before
    assert set(after) == set(before) | set(added)


def test_generated_files_are_backdated_and_sync_files_are_new(tmp_path):
    root = tmp_path / "repo"
    paths = generate_repo(root, n_files=30, seed=1, mtime_age_seconds=86400)
    now = time.time()
    assert all((root / p).stat().st_mtime <= now - 86400 for p in paths)
    added = add_files(root, count=3, batch_tag="sync-001", seed=1)
    assert all((root / p).stat().st_mtime >= now - 5 for p in added)


def test_duplicate_groups_hold_2_to_50_identical_copies(tmp_path):
    root = tmp_path / "repo"
    paths = generate_repo(root, n_files=60, seed=2, dup_groups=6)
    contents = _contents(root)
    assert sorted(paths) == sorted(contents)
    assert len(set(contents.values())) == 60
    groups: dict[bytes, list[str]] = {}
    for path, body in contents.items():
        groups.setdefault(body, []).append(path)
    sizes = sorted(len(g) for g in groups.values() if len(g) > 1)
    assert len(sizes) == 6
    assert all(2 <= size <= 50 for size in sizes)
    copies = [p for p in paths if p.startswith("dups/")]
    assert len(copies) == sum(size - 1 for size in sizes)


def test_duplicate_groups_are_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_repo(a, n_files=30, seed=4, dup_groups=3)
    generate_repo(b, n_files=30, seed=4, dup_groups=3)
    assert _contents(a) == _contents(b)


def test_sync_can_add_copies_of_existing_content(tmp_path):
    root = tmp_path / "repo"
    generate_repo(root, n_files=40, seed=1)
    existing = set(_contents(root).values())
    added = add_files(root, count=10, batch_tag="sync-001", seed=1, dup_fraction=0.5)
    copies = [p for p in added if (root / p).read_bytes() in existing]
    assert len(added) == 10 and len(copies) == 5


def test_listing_ignores_git_metadata_and_dotfiles(tmp_path):
    root = tmp_path / "repo"
    generate_repo(root, n_files=5, seed=1)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".gitignore").write_text(".code-indexer/\n")
    assert len(list_repo_files(root)) == 5


def test_refuses_to_generate_inside_a_git_work_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(RuntimeError, match="git"):
        generate_repo(tmp_path / "repo", n_files=5, seed=1)
