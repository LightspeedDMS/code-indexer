#!/usr/bin/env python3
import argparse
import fcntl
import fnmatch
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ^ why this checkout's src first, not the installed package; chunks must be stopped by the helper of the tree under test
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from code_indexer.utils.process_group import terminate_process_group  # noqa: E402

_DURATIONS_HEADER = re.compile(r"^=+ slowest (?:\d+ )?durations =+$")
_DURATION_ROW = re.compile(
    r"^(\d+(?:\.\d+)?)s (?:setup|call|teardown)\s+([^\s:]+\.py)::"
)
_RUN_STAMP = re.compile(r"(\d{8}_\d{6})")


def parse_durations(lines: Iterable[str]) -> Dict[str, float]:
    durations: Dict[str, float] = {}
    in_section = False
    for line in lines:
        if not in_section:
            in_section = bool(_DURATIONS_HEADER.match(line.strip()))
            continue
        row = _DURATION_ROW.match(line)
        if row:
            seconds = float(row.group(1))
            durations[row.group(2)] = durations.get(row.group(2), 0.0) + seconds
    return durations


def _history_groups(history_dir: Path, globs: List[str]) -> List[List[Path]]:
    groups: Dict[str, List[Path]] = {}
    if not history_dir.is_dir():
        return []
    for pattern in globs:
        for path in history_dir.glob(pattern):
            stamp = _RUN_STAMP.search(path.name)
            if stamp and path.is_file():
                groups.setdefault(stamp.group(1), []).append(path)
    return [sorted(set(groups[stamp])) for stamp in sorted(groups, reverse=True)]


def load_history_durations(
    history_dir: Path, globs: List[str], lookback: int
) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    used = 0
    for group in _history_groups(history_dir, globs):
        if used >= lookback:
            break
        run: Dict[str, float] = {}
        for log in group:
            with log.open(encoding="utf-8", errors="replace") as handle:
                for name, seconds in parse_durations(handle).items():
                    run[name] = run.get(name, 0.0) + seconds
        if not run:
            continue
        used += 1
        for name, seconds in run.items():
            merged.setdefault(name, seconds)
    return merged


def read_ignore_file(path: Path) -> List[str]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            entries.append(entry)
    return entries


# ^ external pytest default norecursedirs plus dot dirs; doc:pytest#confval-norecursedirs
_NO_RECURSE = (
    "*.egg",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)


def _is_ignored(rel: str, ignores: List[str]) -> bool:
    return any(rel == ign or rel.startswith(ign + "/") for ign in ignores)


def _skip_dir(name: str) -> bool:
    return (
        name.startswith(".")
        or name == "__pycache__"
        or any(fnmatch.fnmatch(name, pattern) for pattern in _NO_RECURSE)
    )


def select_test_files(root: Path, paths: List[str], ignores: List[str]) -> List[str]:
    norm_ignores = [Path(ign).as_posix() for ign in ignores]
    selected = set()
    for raw in paths:
        target = (root / raw).resolve()
        if target.is_file():
            # ^ why keep an ignored explicit file, not drop it; pytest --ignore never removes an explicit arg
            selected.add(target.relative_to(root.resolve()).as_posix())
            continue
        if not target.is_dir():
            raise FileNotFoundError(f"test path does not exist: {raw}")
        target_rel = target.relative_to(root.resolve()).as_posix()
        # ^ external pytest --ignore equal to or above an explicit target has no effect; doc:pytest#ignore-paths
        below = [
            ign
            for ign in norm_ignores
            if target_rel == "." or ign.startswith(target_rel + "/")
        ]
        for dirpath, dirnames, filenames in os.walk(target):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not _skip_dir(d)
                and not _is_ignored(
                    (here / d).relative_to(root.resolve()).as_posix(), below
                )
            )
            for name in filenames:
                rel = (here / name).relative_to(root.resolve()).as_posix()
                if fnmatch.fnmatch(name, "test_*.py") and not _is_ignored(rel, below):
                    selected.add(rel)
    return sorted(selected)


@dataclass
class Chunk:
    files: List[str] = field(default_factory=list)
    seconds: float = 0.0
    unmeasured: int = 0


def plan_chunks(files: List[str], durations: Dict[str, float], n: int) -> List[Chunk]:
    if n < 1:
        raise ValueError(f"chunk count must be >= 1 (got {n})")
    chunks = [Chunk() for _ in range(n)]
    unique = sorted(set(files))
    measured = sorted(
        (f for f in unique if f in durations), key=lambda f: (-durations[f], f)
    )
    for name in measured:
        target = min(range(n), key=lambda i: (chunks[i].seconds, i))
        chunks[target].files.append(name)
        chunks[target].seconds += durations[name]
    for index, name in enumerate(f for f in unique if f not in durations):
        chunks[index % n].files.append(name)
        chunks[index % n].unmeasured += 1
    for chunk in chunks:
        chunk.files.sort()
    # ^ hazard keeping an empty chunk => pytest with no file args collects all testpaths
    return [chunk for chunk in chunks if chunk.files]


# ^ external pytest exit 5 = no tests collected; doc:pytest#exit-codes
_NO_TESTS_COLLECTED = 5
_LANE_FAILED = 1


def lane_exit_code(codes: List[int]) -> int:
    if not codes:
        raise ValueError("no chunk exit codes to combine")
    if all(code == _NO_TESTS_COLLECTED for code in codes):
        return _NO_TESTS_COLLECTED
    if any(code not in (0, _NO_TESTS_COLLECTED) for code in codes):
        return _LANE_FAILED
    return 0


@dataclass
class ChunkSummary:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    line: str = "no summary"


_SUMMARY_LINE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|skipped|deselected)\b.* in \d+(?:\.\d+)?s\b"
)
_SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped)\b")


def summarize_log(path: Path) -> ChunkSummary:
    last = ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if _SUMMARY_LINE.search(line):
                last = line.strip()
    summary = ChunkSummary()
    if not last:
        return summary
    summary.line = last.strip("= ")
    for count, word in _SUMMARY_COUNT.findall(last):
        attr = "errors" if word.startswith("error") else word
        setattr(summary, attr, getattr(summary, attr) + int(count))
    return summary


@dataclass
class ChunkResult:
    index: int
    exit_code: int
    wall_seconds: float
    log: Path
    summary: ChunkSummary


# ^ security real_home_lane_env per chunk => no chunk resolves the real ~/.cidx-server; see scripts/real-server-home-guard.sh
_CHUNK_WRAPPER = 'set -e; source "$1"; real_home_lane_env "$2"; shift 2; exec "$@"'


# ^ external /proc/self/mounts escapes space, tab, newline, backslash as octal; doc:man5-fstab
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def mount_fstype(path: Path, mounts: str) -> str:
    target = path.as_posix()
    best, fstype = "", ""
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[1])
        inside = point == "/" or target == point or target.startswith(point + "/")
        if inside and len(point) > len(best):
            best, fstype = point, fields[2]
    if not best:
        raise ValueError(f"no mount contains {target}")
    return fstype


class TmpRootRefused(RuntimeError):
    pass


def require_tmpfs(path: Path, mounts_file: Path = Path("/proc/self/mounts")) -> None:
    if not path.is_dir():
        raise TmpRootRefused(f"lane TMPDIR root {path} is not a directory")
    if not os.access(path, os.W_OK | os.X_OK):
        raise TmpRootRefused(f"lane TMPDIR root {path} is not writable")
    fstype = mount_fstype(path.resolve(), mounts_file.read_text(encoding="utf-8"))
    if fstype != "tmpfs":
        raise TmpRootRefused(
            f"lane TMPDIR root {path} is not on tmpfs ({fstype}); the lanes keep "
            "pytest temp data in RAM and never fall back to disk"
        )


_TMP_ROOT_PREFIX = "cidx-lane-tmp-"
_TMP_CLAIM_PREFIX = ".cidx-lane-tmp-claim-"
_OWNER_FILE = "owner"
OWNERLESS_GRACE_SECONDS = 60.0
# ^ external starttime = field 22 of /proc/PID/stat = index 19 after the ')' closing comm; doc:man5-proc#stat
_STAT_STARTTIME_INDEX = 19


_PROC_ROOT = Path("/proc")


class OwnerLivenessUnknown(RuntimeError):
    pass


def process_start_ticks(pid: int, proc_root: Path = _PROC_ROOT) -> Optional[int]:
    stat_path = proc_root / str(pid) / "stat"
    try:
        stat_text = stat_path.read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        # ^ why only a missing entry proves death; any other error reads as dead => a live lane's root deleted
        raise OwnerLivenessUnknown(
            f"{stat_path} unreadable ({type(exc).__name__})"
        ) from exc
    try:
        return int(stat_text.rsplit(")", 1)[1].split()[_STAT_STARTTIME_INDEX])
    except (IndexError, ValueError) as exc:
        raise OwnerLivenessUnknown(f"{stat_path} unparseable") from exc


def _owner_alive(root: Path, proc_root: Path) -> Optional[bool]:
    try:
        pid_text, ticks_text = (root / _OWNER_FILE).read_text("utf-8").split()
        owner_pid, owner_ticks = int(pid_text), int(ticks_text)
    except (OSError, ValueError):
        return None
    # ^ why pid + start ticks, not pid alone; a reused pid must not keep a dead lane's root
    return process_start_ticks(owner_pid, proc_root) == owner_ticks


def _is_stale(root: Path, now: float, proc_root: Path) -> bool:
    alive = _owner_alive(root, proc_root)
    if alive is None:
        # ^ order a claim dir gets its owner before its rename; ownerless = mid-claim or pre-owner legacy
        return now - root.lstat().st_mtime > OWNERLESS_GRACE_SECONDS
    return not alive


def sweep_stale_tmp_roots(parent: Path, proc_root: Path = _PROC_ROOT) -> List[Path]:
    # ^ why owner liveness, not "found under the lane lock"; tests run unit_lane_start under private locks inside a live lane
    now = time.time()
    stale = []
    for entry in sorted(parent.iterdir()):
        if not entry.name.startswith((_TMP_ROOT_PREFIX, _TMP_CLAIM_PREFIX)):
            continue
        try:
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                continue
            if _is_stale(entry, now, proc_root):
                stale.append(entry)
        except FileNotFoundError:
            continue
        except OwnerLivenessUnknown as unknown:
            print(f"kept {entry}: owner liveness unknown ({unknown})", file=sys.stderr)
    leftovers = remove_trees(stale)
    if leftovers:
        raise TmpCleanupFailed(leftovers)
    return stale


def create_tmp_root(parent: Path, owner_pid: int) -> Path:
    ticks = process_start_ticks(owner_pid)
    if ticks is None:
        raise TmpRootRefused(f"lane owner pid {owner_pid} is not running")
    claim = Path(tempfile.mkdtemp(prefix=_TMP_CLAIM_PREFIX, dir=parent))
    (claim / _OWNER_FILE).write_text(f"{owner_pid} {ticks}\n", encoding="utf-8")
    root = parent / (_TMP_ROOT_PREFIX + claim.name[len(_TMP_CLAIM_PREFIX) :])
    os.rename(claim, root)
    return root


def _chunk_env(
    scratch: Path, tmpdir: Path, extra_env: Dict[str, str]
) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CIDX_REAL_HOME_GUARD_DIR"}
    data_dir = str(scratch / "server-home")
    env["CIDX_SERVER_DATA_DIR"] = data_dir
    env["CIDX_DATA_DIR"] = data_dir
    env.update(extra_env)
    # ^ perf pytest tmp_path on tmpfs, not the disk; 8 chunks contending for fsyncs timed out tests; #2297
    env["TMPDIR"] = str(tmpdir)
    return env


# ^ external pgrp = field 5 of /proc/PID/stat = index 2 after the ')' closing comm; doc:man5-proc#stat
_STAT_PGRP_INDEX = 2
_STAT_STATE_INDEX = 0
_STOP_LOCK = threading.Lock()
# ^ perf bound on joining one waiter after _stop = its group stop grace + one TMPDIR removal
_WAITER_JOIN_SECONDS = 30.0


def _group_survivors(pgid: int) -> List[int]:
    survivors = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == pgid:
            continue
        try:
            fields = (
                Path(f"/proc/{entry}/stat").read_text("utf-8").rsplit(")", 1)[1].split()
            )
        except (OSError, IndexError):
            continue
        if fields[_STAT_PGRP_INDEX] == str(pgid) and fields[_STAT_STATE_INDEX] != "Z":
            survivors.append(int(entry))
    return sorted(survivors)


def _stop_group(
    proc: "subprocess.Popen[bytes]", report_chunk: Optional[int] = None
) -> None:
    with _STOP_LOCK:
        # ^ hazard signalling after the leader is reaped => its pid may name an unrelated reused group
        if proc.returncode is not None:
            return
        survivors = _group_survivors(proc.pid) if report_chunk is not None else []
        if survivors:
            print(
                f"warning: chunk {report_chunk} leader exited with its process group "
                f"still running (pids {', '.join(map(str, survivors))}); stopping "
                "them before its TMPDIR is removed",
                file=sys.stderr,
            )
        if not terminate_process_group(proc):
            print(
                f"warning: chunk pid {proc.pid}: its process group could not be "
                "signalled; only the chunk itself was killed",
                file=sys.stderr,
            )


# ^ hazard proc.poll() before this => leader reaped, its group id lost, descendants survive; test:test_run_chunks_reaps_descendants_of_a_chunk_that_already_exited
def _stop(procs: List["subprocess.Popen[bytes]"]) -> None:
    for proc in procs:
        _stop_group(proc)
        if proc.returncode is None:
            print(f"error: chunk pid {proc.pid} was not reaped", file=sys.stderr)


def run_chunks(
    chunks: List[Chunk],
    *,
    command: List[str],
    common_args: List[str],
    log_paths: List[Path],
    scratch_root: Path,
    tmp_root: Path,
    guard: Path,
    extra_env: Dict[str, str],
    cwd: Path = Path("."),
    pass_fds: Tuple[int, ...] = (),
    pgid_file: Optional[Path] = None,
) -> List[ChunkResult]:
    if len(log_paths) != len(chunks):
        raise ValueError(
            f"{len(chunks)} chunks need {len(chunks)} logs, got {len(log_paths)}"
        )
    scratches: List[Path] = []
    tmpdirs: List[Path] = []
    procs: List["subprocess.Popen[bytes]"] = []
    done: Dict[int, float] = {}
    waiters: List[threading.Thread] = []
    started = time.monotonic()
    try:
        for index, (chunk, log) in enumerate(zip(chunks, log_paths)):
            scratch = Path(
                tempfile.mkdtemp(
                    prefix=f"cidx-lane-chunk{index + 1}-", dir=scratch_root
                )
            )
            scratches.append(scratch)
            tmpdir = Path(
                tempfile.mkdtemp(
                    prefix=f"cidx-lane-chunk{index + 1}-tmp-", dir=tmp_root
                )
            )
            tmpdirs.append(tmpdir)
            log.parent.mkdir(parents=True, exist_ok=True)
            argv = [
                "bash",
                "-c",
                _CHUNK_WRAPPER,
                "unit-lane-chunk",
                str(guard),
                str(scratch),
            ]
            with log.open("wb") as handle:
                # ^ order own session before any stop; terminate_process_group refuses to signal the runner's own group
                proc = subprocess.Popen(
                    argv + command + common_args + chunk.files,
                    cwd=cwd,
                    env=_chunk_env(scratch, tmpdir, extra_env),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    # ^ invariant every chunk holds the lane flock fd => no second lane while any chunk lives
                    pass_fds=pass_fds,
                )
            procs.append(proc)
            if pgid_file is not None:
                with pgid_file.open("a", encoding="utf-8") as pgids:
                    pgids.write(f"{proc.pid}\n")
            waiter = threading.Thread(
                target=_wait_chunk, args=(proc, index, done, tmpdir), daemon=True
            )
            waiter.start()
            waiters.append(waiter)
        for waiter in waiters:
            waiter.join()
    finally:
        _stop(procs)
        # ^ order waiters finish their own TMPDIR removal before the leftover check below
        for waiter in waiters:
            waiter.join(timeout=_WAITER_JOIN_SECONDS)
        # ^ coupled deletion after _stop with scripts/unit_lane_common.sh:_unit_lane_cleanup; a file left = groups to kill
        if pgid_file is not None:
            pgid_file.unlink(missing_ok=True)
        for scratch in scratches:
            shutil.rmtree(scratch, ignore_errors=True)
        # ^ order removal attempted and reported on interrupt too; the interrupt keeps its own exit code
        leftovers = remove_trees(tmpdirs)
    results = [
        ChunkResult(
            i,
            proc.returncode if proc.returncode is not None else -1,
            done[i] - started,
            log_paths[i],
            summarize_log(log_paths[i]),
        )
        for i, proc in enumerate(procs)
    ]
    if leftovers:
        raise TmpCleanupFailed(leftovers, results)
    return results


class TmpCleanupFailed(RuntimeError):
    def __init__(self, paths: List[Path], results: Optional[List[ChunkResult]] = None):
        super().__init__(
            "tmpfs data not removed (RAM stays used): " + ", ".join(map(str, paths))
        )
        self.paths = paths
        self.results = results or []


def remove_trees(paths: Iterable[Path]) -> List[Path]:
    leftovers = []
    for path in paths:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(
                f"FAILED: could not remove {path} ({type(exc).__name__})",
                file=sys.stderr,
            )
            leftovers.append(path)
            continue
        if os.path.lexists(path):
            print(f"FAILED: {path} still exists after removal", file=sys.stderr)
            leftovers.append(path)
    return leftovers


class LaneBusy(RuntimeError):
    pass


def _holder_text(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    parts = os.read(fd, 256).decode("utf-8", errors="replace").split()
    if len(parts) != 2:
        return "another lane"
    return f"lane '{parts[1]}' (pid {parts[0]})"


# ^ invariant the caller keeps fd open for the lane's lifetime; the kernel drops the flock when its last holder exits
def acquire_lane_lock(fd: int, lane: str, pid: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LaneBusy(
            f"{_holder_text(fd)} is running; two lanes never run at once"
        ) from None
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{pid} {lane}\n".encode("utf-8"))


def _wait_chunk(
    proc: "subprocess.Popen[bytes]", index: int, done: Dict[int, float], tmpdir: Path
) -> None:
    # ^ why WNOWAIT, not proc.wait(); the unreaped leader keeps its group id for _stop
    try:
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    except ChildProcessError:
        pass
    done[index] = time.monotonic()
    # ^ order group stopped before its TMPDIR is removed; a descendant would lose its files mid-run
    _stop_group(proc, report_chunk=index + 1)
    # ^ perf tmpfs is RAM; a finished chunk frees its TMPDIR now, not when the slowest chunk ends
    shutil.rmtree(tmpdir, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Duration-balanced parallel chunks for the unit test lanes."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument("--root", default=".")
    selection.add_argument("--paths", nargs="+", required=True)
    selection.add_argument("--ignore-file", action="append", default=[])
    selection.add_argument("--ignore", action="append", default=[])
    selection.add_argument("--chunks", type=int, default=8)
    selection.add_argument("--history-dir", default=".test-telemetry")
    selection.add_argument("--history-glob", action="append", default=[])
    selection.add_argument("--lookback", type=int, default=3)
    plan = sub.add_parser("plan", parents=[selection])
    plan.add_argument("--list-out")
    plan.add_argument("--show-files", action="store_true")
    run = sub.add_parser("run", parents=[selection])
    run.add_argument("--log-prefix", required=True)
    run.add_argument("--timestamp", required=True)
    run.add_argument("--scratch-root", required=True)
    run.add_argument("--tmp-root", required=True)
    run.add_argument("--guard", required=True)
    run.add_argument("--env", action="append", default=[])
    run.add_argument("--lock-fd", type=int)
    run.add_argument("--pgid-file")
    locker = sub.add_parser("lock")
    locker.add_argument("--fd", type=int, required=True)
    locker.add_argument("--pid", type=int, required=True)
    locker.add_argument("--lane", required=True)
    tmp_root = sub.add_parser("tmp-root")
    tmp_root.add_argument("--parent", default="/dev/shm")
    tmp_root.add_argument("--owner-pid", type=int, required=True)
    return parser


def _build_plan(args: argparse.Namespace) -> Tuple[List[Chunk], List[str]]:
    root = Path(args.root)
    ignores = [
        entry for path in args.ignore_file for entry in read_ignore_file(Path(path))
    ]
    ignores += args.ignore
    files = select_test_files(root, args.paths, ignores)
    history = root / args.history_dir
    durations = load_history_durations(history, args.history_glob, args.lookback)
    return plan_chunks(files, durations, args.chunks), ignores


def _print_plan(chunks: List[Chunk], show_files: bool) -> None:
    total = sum(len(chunk.files) for chunk in chunks)
    longest = max((chunk.seconds for chunk in chunks), default=0.0)
    print(
        f"plan: {total} files in {len(chunks)} chunks; longest estimate {longest:.0f}s"
    )
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"chunk {index}: {len(chunk.files)} files, estimate {chunk.seconds:.0f}s "
            f"({chunk.unmeasured} unmeasured)"
        )
        if show_files:
            for name in chunk.files:
                print(f"    {name}")


_TMP_ROOT_REFUSED_EXIT = 2


def _cmd_run(args: argparse.Namespace, pytest_args: List[str]) -> int:
    tmp_root = Path(args.tmp_root)
    try:
        require_tmpfs(tmp_root)
    except TmpRootRefused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return _TMP_ROOT_REFUSED_EXIT
    chunks, ignores = _build_plan(args)
    _print_plan(chunks, show_files=False)
    if not chunks:
        print("no test files selected")
        return _NO_TESTS_COLLECTED
    extra_env: Dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            raise ValueError(f"--env needs KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        extra_env[key] = value
    root = Path(args.root)
    logs = [
        root / args.history_dir / f"{args.log_prefix}-{args.timestamp}-chunk{k}.log"
        for k in range(1, len(chunks) + 1)
    ]
    try:
        results = run_chunks(
            chunks,
            command=[sys.executable, "-m", "pytest"],
            common_args=pytest_args + [f"--ignore={ign}" for ign in ignores],
            log_paths=logs,
            scratch_root=Path(args.scratch_root),
            tmp_root=tmp_root,
            guard=Path(args.guard),
            extra_env=extra_env,
            cwd=root,
            pass_fds=() if args.lock_fd is None else (args.lock_fd,),
            pgid_file=None if args.pgid_file is None else Path(args.pgid_file),
        )
    except TmpCleanupFailed as failed:
        _print_results(failed.results)
        print(f"FAILED: {failed}", file=sys.stderr)
        return _LANE_FAILED
    _print_results(results)
    return lane_exit_code([r.exit_code for r in results])


_FAILURE_LINES_SHOWN = 20
# ^ coupled exit code with the lock calls in fast-automation.sh, server-fast-automation.sh, slow-automation.sh
_LANE_BUSY_EXIT = 3


def _chunk_status(code: int) -> str:
    if code == 0:
        return "PASS (exit 0)"
    if code == _NO_TESTS_COLLECTED:
        return "EMPTY (exit 5)"
    return f"FAIL (exit {code})"


def _print_results(results: List[ChunkResult]) -> None:
    print("=== chunk results ===")
    for r in results:
        print(
            f"  chunk {r.index + 1}: {_chunk_status(r.exit_code)} - {r.summary.line}"
            f" - wall {r.wall_seconds:.0f}s - {r.log}"
        )
    for r in results:
        if r.exit_code in (0, _NO_TESTS_COLLECTED):
            continue
        print(f"--- chunk {r.index + 1} failures ---")
        shown = 0
        with r.log.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if shown >= _FAILURE_LINES_SHOWN:
                    break
                if line.startswith(("FAILED ", "ERROR ")):
                    print(line.rstrip())
                    shown += 1
    totals = ChunkSummary()
    for r in results:
        totals.passed += r.summary.passed
        totals.failed += r.summary.failed
        totals.errors += r.summary.errors
        totals.skipped += r.summary.skipped
    wall = max(r.wall_seconds for r in results)
    print(
        f"overall: {totals.passed} passed, {totals.failed} failed, {totals.errors} errors, "
        f"{totals.skipped} skipped in {len(results)} chunks; wall {wall:.0f}s"
    )
    if all(r.exit_code == _NO_TESTS_COLLECTED for r in results):
        print("FAILED: no tests ran in any chunk (check --paths and -m)")


_INTERRUPTED_EXIT = 130


def _cmd_tmp_root(args: argparse.Namespace) -> int:
    parent = Path(args.parent)
    try:
        require_tmpfs(parent)
        swept = sweep_stale_tmp_roots(parent)
        root = create_tmp_root(parent, args.owner_pid)
    except TmpRootRefused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return _TMP_ROOT_REFUSED_EXIT
    except TmpCleanupFailed as failed:
        print(f"FAILED: stale lane {failed}", file=sys.stderr)
        return _LANE_FAILED
    except OwnerLivenessUnknown as unknown:
        print(f"FAILED: lane owner {unknown}", file=sys.stderr)
        return _LANE_FAILED
    for stale in swept:
        print(f"removed stale lane TMPDIR root {stale}", file=sys.stderr)
    print(root)
    return 0


def _interrupt(signum: int, frame: object) -> None:
    # ^ hazard a second TERM/INT inside run_chunks' finally => cleanup aborted, chunk groups survive; test:test_run_cleanup_survives_a_second_signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: List[str]) -> int:
    head, pytest_args = argv, []
    if "--" in argv:
        separator = argv.index("--")
        head, pytest_args = argv[:separator], argv[separator + 1 :]
    args = _parser().parse_args(head)
    if args.command == "plan":
        chunks, _ = _build_plan(args)
        _print_plan(chunks, args.show_files)
        if args.list_out:
            out = Path(args.list_out)
            out.mkdir(parents=True, exist_ok=True)
            for k, chunk in enumerate(chunks, start=1):
                (out / f"chunk{k}.txt").write_text(
                    "".join(f"{f}\n" for f in chunk.files), encoding="utf-8"
                )
        return 0
    if args.command == "run":
        # ^ why SIGTERM/SIGINT => KeyboardInterrupt, not default death; chunks sit in their own sessions and run_chunks finally must stop them
        signal.signal(signal.SIGTERM, _interrupt)
        signal.signal(signal.SIGINT, _interrupt)
        try:
            return _cmd_run(args, pytest_args)
        except KeyboardInterrupt:
            print("interrupted: chunks stopped, scratch removed", file=sys.stderr)
            return _INTERRUPTED_EXIT
    if args.command == "tmp-root":
        return _cmd_tmp_root(args)
    try:
        acquire_lane_lock(args.fd, args.lane, args.pid)
    except LaneBusy as busy:
        print(f"REFUSED: {busy}", file=sys.stderr)
        return _LANE_BUSY_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
