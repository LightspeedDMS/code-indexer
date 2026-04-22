"""
NFS atomic-create self-check — Story #877 Phase 1.

Validates that the filesystem hosting cidx_meta_dir honours O_CREAT|O_EXCL
atomicity under concurrent contention before the shared memory store is enabled.
"""
import numbers
import os
import queue
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Union, cast


class NFSAtomicCreateSelfCheckError(RuntimeError):
    """Raised when the filesystem does not honour O_CREAT|O_EXCL atomicity."""


DEFAULT_RACER_TIMEOUT_SECONDS = 5
_PathInput = Union[str, Path, "os.PathLike[str]"]
_Outcome = Union[int, Exception]


def run_nfs_atomic_create_self_check(
    cidx_meta_dir: _PathInput,
    *,
    iterations: int = 10,
    racer_timeout_seconds: float = DEFAULT_RACER_TIMEOUT_SECONDS,
) -> None:
    """Race two threads to O_CREAT|O_EXCL the same path `iterations` times.

    Exactly one thread must win each race; the other must get FileExistsError.
    Raises NFSAtomicCreateSelfCheckError on any atomicity violation or error.
    Always attempts to remove the temporary subdirectory. If the check itself
    failed and cleanup also fails, the original check error is raised with the
    cleanup failure attached as a note (Python >= 3.11) or __context__.
    """
    meta_dir = _validate_args(cidx_meta_dir, iterations, racer_timeout_seconds)
    tmp_dir = tempfile.mkdtemp(prefix=".self-check-", dir=meta_dir)
    original_exc: BaseException | None = None
    try:
        for i in range(iterations):
            target = Path(tmp_dir) / f"probe-{i}"
            rq: "queue.Queue[_Outcome]" = queue.Queue()
            racer = _make_racer(target, threading.Barrier(2), rq)
            threading.Thread(target=racer, daemon=True).start()
            threading.Thread(target=racer, daemon=True).start()
            outcomes: list[_Outcome] = []
            for _ in range(2):
                try:
                    outcomes.append(rq.get(timeout=racer_timeout_seconds))
                except queue.Empty:
                    raise NFSAtomicCreateSelfCheckError(
                        f"Racer timed out after {racer_timeout_seconds}s on {target}."
                    )
            for o in outcomes:
                if isinstance(o, NFSAtomicCreateSelfCheckError):
                    raise o
                if isinstance(o, Exception) and not isinstance(o, FileExistsError):
                    raise NFSAtomicCreateSelfCheckError(
                        f"Unexpected {type(o).__name__} on {target}: {o}"
                    )
            winners = sum(1 for o in outcomes if isinstance(o, int))
            if winners != 1:
                raise NFSAtomicCreateSelfCheckError(
                    f"Atomicity violation on {target}: {winners} winner(s) "
                    f"(expected 1). Filesystem may not honour O_CREAT|O_EXCL "
                    f"(e.g. NFSv3 without lock=native)."
                )
    except BaseException as exc:
        original_exc = exc
        raise
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except OSError as cleanup_exc:
            cleanup_msg = f"Also failed to remove temp dir {tmp_dir}: {cleanup_exc}"
            if original_exc is not None:
                # Original check already failed — attach cleanup note without
                # replacing the root-cause exception so callers see the check failure.
                if sys.version_info >= (3, 11):
                    original_exc.add_note(cleanup_msg)
                else:
                    original_exc.__context__ = NFSAtomicCreateSelfCheckError(cleanup_msg)
            else:
                raise NFSAtomicCreateSelfCheckError(
                    f"Failed to remove self-check temp dir {tmp_dir}: {cleanup_exc}"
                ) from cleanup_exc


def _validate_args(
    cidx_meta_dir: object,
    iterations: object,
    racer_timeout_seconds: object,
) -> Path:
    """Validate all entry-point arguments; return normalised Path on success.

    cidx_meta_dir is typed as object so the isinstance guard performs real
    narrowing; cast() then communicates that narrowing to the type checker
    before os.fspath() is called.  bytes results are rejected explicitly.
    """
    if cidx_meta_dir is None or not isinstance(
        cidx_meta_dir, (str, Path, os.PathLike)
    ):
        raise NFSAtomicCreateSelfCheckError(
            f"cidx_meta_dir must be str/Path/PathLike[str], "
            f"got {type(cidx_meta_dir).__name__!r}"
        )
    raw = os.fspath(cast(_PathInput, cidx_meta_dir))
    if isinstance(raw, bytes):
        raise NFSAtomicCreateSelfCheckError(
            "cidx_meta_dir resolved to bytes; provide str or PathLike[str]."
        )
    meta_dir = Path(raw)
    if not meta_dir.exists():
        raise NFSAtomicCreateSelfCheckError(f"cidx_meta_dir does not exist: {meta_dir}")
    if not meta_dir.is_dir():
        raise NFSAtomicCreateSelfCheckError(f"cidx_meta_dir is not a directory: {meta_dir}")
    if not os.access(meta_dir, os.W_OK):
        raise NFSAtomicCreateSelfCheckError(f"cidx_meta_dir is not writable: {meta_dir}")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise NFSAtomicCreateSelfCheckError(
            f"iterations must be a non-bool int >= 1, got {iterations!r}"
        )
    if (
        not isinstance(racer_timeout_seconds, numbers.Real)
        or isinstance(racer_timeout_seconds, bool)
        or racer_timeout_seconds <= 0
    ):
        raise NFSAtomicCreateSelfCheckError(
            f"racer_timeout_seconds must be a real number > 0, got {racer_timeout_seconds!r}"
        )
    return meta_dir


def _make_racer(
    target: Path,
    barrier: threading.Barrier,
    result_queue: "queue.Queue[_Outcome]",
) -> Callable[[], None]:
    """Return a racer closure that guarantees exactly one result_queue.put() per call.

    Both threads wait on barrier before racing to os.open() target with
    O_CREAT|O_EXCL|O_WRONLY.  All outcomes including barrier and close errors
    are enqueued so the main thread always receives exactly two results.
    The nested racer() closure is an implementation detail of this factory.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY

    def racer() -> None:
        fd: int | None = None
        outcome: _Outcome
        try:
            barrier.wait()
            fd = os.open(str(target), flags, 0o600)
            outcome = fd
        except FileExistsError as exc:
            outcome = exc
        except Exception as exc:
            outcome = NFSAtomicCreateSelfCheckError(
                f"Unexpected {type(exc).__name__} on {target}: {exc}"
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as close_exc:
                    outcome = NFSAtomicCreateSelfCheckError(
                        f"Failed to close fd on {target}: {close_exc}"
                    )
            result_queue.put(outcome)

    return racer
