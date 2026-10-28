import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, TypeVar, Union

import httpx

REDACTED = "<redacted>"
CHUNK_BYTES = 1024 * 1024
REMOVED_STATUS = 204

# ^ invariant no frame local or object reachable from a raised failure holds a raw secret


class Masked:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED


class StepFailed(Exception):
    pass


class ScanError(Exception):
    pass


class StepResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __repr__(self) -> str:
        return (
            f"StepResult(returncode={self.returncode}, "
            f"stdout_chars={len(self.stdout)}, stderr_chars={len(self.stderr)})"
        )


Arg = Union[str, Masked]
ErrorT = TypeVar("ErrorT", bound=BaseException)


def _unchained(error: ErrorT) -> ErrorT:
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error


def _contains(path: Path, secret: Masked, chunk_bytes: int) -> bool:
    needle = secret.value.encode()
    keep = len(needle) - 1
    tail = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                return False
            window = tail + chunk
            if needle in window:
                return True
            # ^ invariant overlap = len(needle)-1 bytes => a key split across chunks is still found
            tail = window[-keep:] if keep else b""


VANISHED = (FileNotFoundError, NotADirectoryError)


def scan_file(path: Path, secret: Masked, chunk_bytes: int) -> Optional[bool]:
    try:
        return _contains(path, secret, chunk_bytes)
    except VANISHED:
        if not os.path.lexists(path):
            return None
        raise


def _walk_problem(root: Path, errors: List[OSError]) -> Optional[str]:
    for err in errors:
        # ^ why skip vanished, not fail; server staging folders come and go mid-walk
        if isinstance(err, VANISHED) and not os.path.lexists(str(err.filename)):
            continue
        where = os.path.relpath(str(err.filename), root)
        return f"{where}: unreadable ({type(err).__name__})"
    return None


def files_containing(
    secret: Masked, roots: Iterable[Path], chunk_bytes: int = CHUNK_BYTES
) -> List[str]:
    names: List[str] = []
    problem: Optional[str] = None
    for root in roots:
        errors: List[OSError] = []
        for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if problem is not None or not path.is_file() or path.is_symlink():
                    continue
                try:
                    found = scan_file(path, secret, chunk_bytes)
                except Exception as exc:
                    # ^ why fail, not skip; an unreadable file cannot be certified clean
                    problem = (
                        f"{path.relative_to(root)}: unreadable ({type(exc).__name__})"
                    )
                    continue
                if found:
                    names.append(name)
        problem = problem or _walk_problem(root, errors)
    if problem is not None:
        raise _unchained(ScanError(problem))
    return names


def _spawn(
    argv: Sequence[Arg], timeout: float, env: Optional[Mapping[str, str]]
) -> StepResult:
    resolved = [a.value if isinstance(a, Masked) else a for a in argv]
    try:
        done = subprocess.run(
            resolved,
            capture_output=True,
            timeout=timeout,
            env=None if env is None else dict(env),
        )
    finally:
        del resolved
    return StepResult(
        done.returncode,
        done.stdout.decode(errors="replace"),
        done.stderr.decode(errors="replace"),
    )


def run_step(
    step: str,
    argv: Sequence[Arg],
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
) -> StepResult:
    failure = ""
    try:
        return _spawn(argv, timeout, env)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        failure = type(exc).__name__
    raise _unchained(StepFailed(f"{step}: failed ({failure})"))


def _report_teardown(teardown: Callable[[], List[str]]) -> None:
    try:
        problems = teardown()
    except Exception as exc:
        problems = [f"teardown raised {type(exc).__name__}"]
    if problems:
        print("teardown problems: " + "; ".join(problems), file=sys.stderr)


def run_with_teardown(
    body: Callable[[], object], teardown: Callable[[], List[str]]
) -> None:
    try:
        body()
    except BaseException:
        _report_teardown(teardown)
        raise
    problems = teardown()
    if problems:
        raise AssertionError("teardown could not remove: " + "; ".join(problems))


def remove_golden_repos(
    http: httpx.Client, token: str, aliases: Iterable[str]
) -> List[str]:
    failures = []
    for alias in aliases:
        try:
            response = http.request(
                "DELETE",
                f"/api/admin/golden-repos/{alias}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            failures.append(f"{alias}: {type(exc).__name__}")
            continue
        if response.status_code != REMOVED_STATUS:
            failures.append(f"{alias}: HTTP {response.status_code}")
    return failures
