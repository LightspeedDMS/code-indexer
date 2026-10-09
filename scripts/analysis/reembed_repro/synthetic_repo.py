"""Synthetic non-git repository shaped like a trace-sync ``local://`` repo.

Many small markdown and JSON "trace" files spread across session
directories (about ``files_per_dir`` files each), every line carrying a
unique span id so no two files -- and no two chunks -- share content. A
sync job is modelled by ``add_files``, which writes a few NEW files and
never touches existing ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path
from typing import List

_TOOLS = ["read_file", "search", "edit", "bash", "list_dir", "fetch", "plan", "review"]
_STATUSES = ["ok", "ok", "ok", "retry", "error"]
_WORDS = (
    "agent span tool call result context window token budget planner step "
    "retrieval cache refresh index chunk vector query latency timeout model "
    "prompt summary trace session turn handoff verify patch apply rollback"
).split()


def _assert_not_in_git_work_tree(root: Path) -> None:
    probe = root
    while not probe.exists():
        probe = probe.parent
    result = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip() == "true":
        raise RuntimeError(
            f"{root} is inside a git work tree; the repro needs a non-git repo"
        )


def _session_dir(seed: int, dir_index: int) -> str:
    digest = hashlib.sha1(f"{seed}:{dir_index}".encode()).hexdigest()[:12]
    day = 1 + dir_index % 28
    return f"sessions/2026-09-{day:02d}/{digest}"


def _line(rng: random.Random, uid: str) -> str:
    words = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(4, 10)))
    return (
        f"span {uid} tool={rng.choice(_TOOLS)} status={rng.choice(_STATUSES)} "
        f"duration_ms={rng.randint(1, 90000)} note: {words}"
    )


def _file_body(rng: random.Random, ext: str, uid_prefix: str) -> str:
    n_lines = rng.randint(6, 30) if rng.random() < 0.9 else rng.randint(60, 120)
    lines = [_line(rng, f"{uid_prefix}-{i}") for i in range(n_lines)]
    if ext == "md":
        return (
            f"# Trace {uid_prefix}\n\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n"
        )
    spans = [{"id": f"{uid_prefix}-{i}", "event": line} for i, line in enumerate(lines)]
    return json.dumps({"trace": uid_prefix, "spans": spans}, indent=2) + "\n"


def _write_file(
    root: Path, rel_dir: str, name_index: int, rng: random.Random, uid_prefix: str
) -> str:
    ext = "md" if rng.random() < 0.7 else "json"
    rel_path = f"{rel_dir}/turn-{name_index:03d}.{ext}"
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_file_body(rng, ext, uid_prefix), encoding="utf-8")
    return rel_path


DEFAULT_MTIME_AGE_SECONDS = 2 * 86400


#: A duplicate-content group holds between these many byte-identical files.
DUP_GROUP_MIN, DUP_GROUP_MAX = 2, 50


def _copy(root: Path, src_rel: str, dest_rel: str) -> str:
    src, dest = root / src_rel, root / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return dest_rel


def generate_repo(
    root: Path,
    n_files: int,
    seed: int = 1,
    files_per_dir: int = 8,
    mtime_age_seconds: int = DEFAULT_MTIME_AGE_SECONDS,
    dup_groups: int = 0,
) -> List[str]:
    """Create ``n_files`` trace files under ``root``; return their relative paths.

    Every file's mtime is backdated by at least ``mtime_age_seconds`` (spread
    over another ``mtime_age_seconds``), like trace files synced over days
    before the repository was first indexed. ``dup_groups`` distinct files
    each get a group of 2-50 byte-identical copies under ``dups/`` (the
    returned paths include the copies).
    """
    if n_files <= 0 or files_per_dir <= 0 or mtime_age_seconds <= 0:
        raise ValueError(
            "n_files, files_per_dir and mtime_age_seconds must be positive"
        )
    if not 0 <= dup_groups <= n_files:
        raise ValueError("dup_groups must be between 0 and n_files")
    _assert_not_in_git_work_tree(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    newest = time.time() - mtime_age_seconds
    paths = []
    for i in range(n_files):
        dir_index, name_index = divmod(i, files_per_dir)
        rel_path = _write_file(
            root, _session_dir(seed, dir_index), name_index, rng, f"s{seed}-f{i}"
        )
        stamp = newest - (i * 7919) % mtime_age_seconds
        os.utime(root / rel_path, (stamp, stamp))
        paths.append(rel_path)
    dup_rng = random.Random(f"{seed}:dups")
    for group, src in enumerate(dup_rng.sample(paths[:n_files], dup_groups)):
        stat = (root / src).stat()
        for j in range(1, dup_rng.randint(DUP_GROUP_MIN, DUP_GROUP_MAX)):
            copy = _copy(
                root, src, f"dups/group-{group:03d}/copy-{j:02d}{Path(src).suffix}"
            )
            os.utime(root / copy, (stat.st_mtime, stat.st_mtime))
            paths.append(copy)
    return paths


def add_files(
    root: Path, count: int, batch_tag: str, seed: int = 1, dup_fraction: float = 0.0
) -> List[str]:
    """Write ``count`` NEW files in a fresh sync directory; return their paths.

    The first ``round(count * dup_fraction)`` are byte copies of seeded
    existing files (a sync that brings content the repository already holds).
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if not 0.0 <= dup_fraction <= 1.0:
        raise ValueError("dup_fraction must be within [0, 1]")
    rng = random.Random(f"{seed}:{batch_tag}")
    rel_dir = f"sessions/sync/{batch_tag}"
    if (root / rel_dir).exists():
        raise RuntimeError(f"sync batch directory already exists: {rel_dir}")
    n_dup = round(count * dup_fraction)
    existing = list_repo_files(root) if n_dup else []
    if n_dup and not existing:
        raise RuntimeError("no existing files to copy")
    added = []
    for i, src in enumerate(rng.sample(existing, min(n_dup, len(existing)))):
        added.append(_copy(root, src, f"{rel_dir}/copy-{i:03d}{Path(src).suffix}"))
    for i in range(len(added), count):
        added.append(_write_file(root, rel_dir, i, rng, f"{batch_tag}-f{i}"))
    return added


def list_repo_files(root: Path) -> List[str]:
    """Sorted relative paths of every repository file, skipping dot paths
    (``.code-indexer``, ``.git``, ``.gitignore``)."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )
