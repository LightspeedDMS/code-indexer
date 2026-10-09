"""Command line of the reproduction runner: options, scenario presets, trees."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

from scenarios import DEFAULT_CYCLES, SCENARIOS

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[2]
SCRATCH_ROOT = Path.home() / ".tmp" / "reembed-repro"
TREES_DIR = SCRATCH_ROOT / "trees"
DEFAULT_OLD_REF = "v12.82.0"
# init, initial and cycle-1 on node 0; the first crash recovery and everything
# after it on node 1, whose resume-seal key differs (as on a cluster).
DEFAULT_NODE_SEQUENCE = [0, 0, 0, 1]
CLUSTER_MODE_ENV = "CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE"
#: R-d: how long the fake holds each response of a SIGTERM@inflight run
#: (well under the client's request timeout).
DEFAULT_HOLD_SECONDS = 5.0
_MARKER = ".extracted"


def _extract(sha: str, dest: Path) -> None:
    # A path-filtered archive keeps the "src/" prefix, so files land in dest/src.
    archive = subprocess.Popen(
        ["git", "-C", str(REPO_ROOT), "archive", sha, "src"], stdout=subprocess.PIPE
    )
    untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout)
    if archive.stdout is not None:
        archive.stdout.close()
    if archive.wait() != 0 or untar.returncode != 0:
        raise RuntimeError(f"extracting {sha} failed")
    (dest / _MARKER).touch()


def archive_tree(ref: str, dest_root: Path) -> Path:
    """Extract ``src/`` of git ``ref`` once (read-only ``git archive``); return it.

    Safe for concurrent harness runs: each extracts into its own staging
    directory, which is renamed into place atomically; a complete tree that
    another run published first wins, and a stale partial one is replaced.
    """
    resolved = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise RuntimeError(f"unknown git ref {ref!r}")
    sha = resolved.stdout.strip()
    target = dest_root / sha[:12]
    if (target / _MARKER).exists():
        return target / "src"
    dest_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}-", dir=dest_root))
    try:
        _extract(sha, staging)
        _publish(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target / "src"


_PUBLISH_ATTEMPTS = 3


def _publish(staging: Path, target: Path) -> None:
    """Atomically rename ``staging`` onto an absent ``target``.

    A complete target another run published first wins. A stale target (no
    marker) is renamed aside under a unique quarantine name -- never deleted
    before publication -- and quarantines are removed only after publishing.
    """
    quarantined: List[Path] = []
    try:
        for _ in range(_PUBLISH_ATTEMPTS):
            try:
                os.rename(staging, target)
                return
            except OSError:
                if (target / _MARKER).exists():
                    return
                aside = target.with_name(f".{target.name}-stale-{secrets.token_hex(4)}")
                try:
                    os.rename(target, aside)
                except FileNotFoundError:
                    continue  # another run moved it aside first; retry the publish
                quarantined.append(aside)
        raise RuntimeError(
            f"could not publish {target} after {_PUBLISH_ATTEMPTS} attempts"
        )
    finally:
        for aside in quarantined:
            try:
                shutil.rmtree(aside)
            except OSError as exc:
                print(f"WARNING: stale tree left at {aside}: {exc}", file=sys.stderr)


def parse_node_sequence(text: str) -> List[int]:
    """'0,0,1' -> [0, 0, 1]; each entry a non-negative node index."""
    nodes = []
    for entry in text.split(","):
        if not entry.strip().isdigit():
            raise ValueError(f"bad node index {entry!r} in {text!r}")
        nodes.append(int(entry))
    return nodes


def node_for_run(run_index: int, sequence: List[int], rotate: bool) -> int:
    """Cluster node (and so resume-seal key) the run_index-th run executes on.

    Runs are numbered in execution order: init, initial, cycles, finals.
    """
    if run_index < 0:
        raise ValueError(f"run_index must be non-negative, got {run_index}")
    if rotate:
        return run_index
    if not sequence:
        return 0
    return sequence[min(run_index, len(sequence) - 1)]


_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _safe_name(text: str) -> str:
    """--name is one path component under the scratch root, nothing else."""
    if not text or text in (".", "..") or not set(text) <= _NAME_CHARS:
        raise argparse.ArgumentTypeError(
            f"--name must be one path component of [A-Za-z0-9._-], got {text!r}"
        )
    return text


MAX_RECOVERY_DELAY_SECONDS = 600.0


def _bounded_delay(text: str) -> float:
    """--recovery-delay-seconds: a finite number within [0, 600]."""
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    # Chained comparison is False for nan, and inf exceeds the bound.
    if not 0.0 <= value <= MAX_RECOVERY_DELAY_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must be within [0, {MAX_RECOVERY_DELAY_SECONDS:g}] seconds, got {text!r}"
        )
    return value


def work_dir_for(name: str, root: Path = SCRATCH_ROOT) -> Path:
    """The scratch work directory; refuses anything not directly under ``root``
    (checked on the resolved path, so a symlink cannot steer an rmtree)."""
    work = root / _safe_name(name)
    if work.resolve().parent != root.resolve():
        raise RuntimeError(
            f"work directory {work} resolves outside the scratch root {root}"
        )
    return work


def _build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawTextHelpFormatter
    )
    add = p.add_argument
    add(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="default",
        help="preset of the options below (see scenarios.py); explicit options win",
    )
    add(
        "--files",
        type=int,
        default=20000,
        help="synthetic repo size (199000 = large-repo soak)",
    )
    add("--files-per-dir", type=int, default=8)
    add(
        "--dup-groups",
        type=int,
        default=0,
        help="duplicate-content groups of 2-50 identical copies",
    )
    add(
        "--sync-files",
        type=int,
        default=5,
        help="files the sync job adds before each cycle",
    )
    add(
        "--sync-dup-fraction",
        type=float,
        default=0.0,
        help="fraction of each sync's files that copy existing content",
    )
    add(
        "--git",
        action="store_true",
        help="git variant: commits, edits, renames, branch switches",
    )
    add(
        "--cycles",
        default=DEFAULT_CYCLES,
        help="comma list, one refresh per entry: SIGTERM|SIGKILL|CANCEL|RESTART@early:<s>|"
        "planned:<s>|chunks:<n>|inflight:<n>|final:<s>, or 'none'",
    )
    add(
        "--random-cycles",
        type=int,
        default=0,
        help="N seeded random cycles (plus the R-d yield probe) instead of --cycles",
    )
    add("--cycle-seed", type=int, default=1)
    add(
        "--random-kinds",
        default="SIGTERM,SIGKILL",
        help="interrupt kinds for random cycles",
    )
    add("--max-chunks-trigger", type=int, default=400)
    add(
        "--hold-seconds",
        type=float,
        default=DEFAULT_HOLD_SECONDS,
        help="R-d: seconds the fake holds each response of an inflight-triggered run",
    )
    add("--final-runs", type=int, default=2, help="uninterrupted refreshes at the end")
    add(
        "--recovery-delay-seconds",
        type=_bounded_delay,
        default=0.0,
        help="pause before the final recovery runs, as when the recovery runs on a later "
        "scheduler cycle (random presets: 65 s, beyond the 60 s incremental safety buffer)",
    )
    add("--seed", type=int, default=1)
    add(
        "--name",
        type=_safe_name,
        default=None,
        help="scratch sub-directory (default: <scenario>-n<files>)",
    )
    add("--src", default=str(REPO_ROOT / "src"), help="code_indexer tree under test")
    add(
        "--src-ref",
        default=None,
        help="pin the tree under test to this git ref (read-only git archive), e.g. HEAD",
    )
    add(
        "--old-ref",
        default=DEFAULT_OLD_REF,
        help="an older release run before the upgrade, extracted with git archive and used "
        "for init, the initial index and the first --old-src-runs refreshes; 'none' disables",
    )
    add("--initial-src", default=None, help="explicit old tree (overrides --old-ref)")
    add(
        "--old-src-runs",
        type=int,
        default=2,
        help="the first N cycle refreshes also run on the old tree (an upgrade between refreshes)",
    )
    add(
        "--no-cluster-mode",
        action="store_true",
        help=f"do not set {CLUSTER_MODE_ENV}=1 (cluster-like mode)",
    )
    add(
        "--rotate-node-key",
        action="store_true",
        help="fresh server data dir (resume-seal key) per run, as on another cluster node",
    )
    add(
        "--node-sequence",
        type=parse_node_sequence,
        default=DEFAULT_NODE_SEQUENCE,
        help="node per run in order (init, initial, cycles, finals; last repeats), "
        "e.g. 0,0,0,1 -- each node has its own resume-seal key",
    )
    add("--keep", action="store_true", help="keep the scratch repo and logs")
    add(
        "--disk",
        action="store_true",
        help="keep the repo on disk; default overlays it with a private RAM tmpfs "
        "(per-file fsync makes on-disk indexing ~12 files/s)",
    )
    return p


def parse_args(argv: List[str], description: str = "") -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--scenario", choices=sorted(SCENARIOS), default="default")
    scenario = pre.parse_known_args(argv)[0].scenario
    parser = _build_parser(description)
    parser.set_defaults(**SCENARIOS[scenario])
    args = parser.parse_args(argv)
    if args.src_ref:
        args.src = str(archive_tree(args.src_ref, TREES_DIR))
    if args.initial_src is None and args.old_ref != "none":
        args.initial_src = str(archive_tree(args.old_ref, TREES_DIR))
    if not args.initial_src:
        args.old_src_runs = 0  # no old tree: every run uses --src
    args.name = args.name or f"{args.scenario}-n{args.files}"
    return args
