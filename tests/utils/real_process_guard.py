from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

CIDX = "cidx"
XRAY_CLI = "xray-cli"
REPORT_ENV = "CIDX_REAL_PROCESS_GUARD_REPORT"
# ^ coupled ALLOWING_MARKERS with fast-automation.sh and server-fast-automation.sh `-m` exclusions
ALLOWING_MARKERS = (
    "slow",
    "e2e",
    "real_api",
    "integration",
    "requires_server",
    "requires_containers",
    "performance",
)

_WATCHED = frozenset({"subprocess.Popen", "os.exec", "os.posix_spawn", "os.system"})
_CIDX_MODULES = frozenset(
    {"code_indexer.cli", "code_indexer.cli_fast_entry", "code_indexer"}
)
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
_WRAPPERS = frozenset({"command", "exec", "env", "nohup"})
_PYTHON_OPTIONS_WITH_VALUE = frozenset({"-X", "-W"})
_PYTHON_NAME = re.compile(r"python[0-9.]*")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_HEAD_BYTES = 4096
_ABSOLUTE_PATH = re.compile(rb"/[^\s'\"\\,;()\[\]]+")
_MAX_FOLLOWED_PATHS = 8
_SHOWN_ARGS = 6

# ^ why plain globals, not contextvars; worker threads (RefreshScheduler, indexing) never see a contextvar
_active: Optional[Tuple[str, bool]] = None
_violations: Dict[str, List[str]] = {}
_lock = threading.Lock()
_installed = False
_report_write_failures = 0


class RealProcessInUnitTest(Exception):
    pass


def _text(value: Any) -> str:
    if isinstance(value, (bytes, os.PathLike)):
        return os.fsdecode(value)
    return str(value)


def classify_spawn(
    argv: Sequence[Any], env: Optional[Mapping[Any, Any]]
) -> Optional[str]:
    return _classify_args([_text(a) for a in argv], env)


def _classify_args(args: List[str], env: Optional[Mapping[Any, Any]]) -> Optional[str]:
    args = _strip_wrappers(args)
    if not args:
        return None
    name = os.path.basename(args[0])
    if (
        name in _SHELLS
        and len(args) >= 3
        and args[1].startswith("-")
        and "c" in args[1]
    ):
        return _classify_shell(args[2], env)
    if _runs_cidx_module(args):
        return CIDX
    if name == CIDX:
        return CIDX if _cidx_executable_runs_code_indexer(args[0], env) else None
    if name == XRAY_CLI:
        return XRAY_CLI
    return None


def _classify_shell(command: str, env: Optional[Mapping[Any, Any]]) -> Optional[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return CIDX if "code_indexer" in command else None
    found: Optional[str] = None
    segment: List[str] = []
    for token in [*tokens, ";"]:
        if token and all(ch in lexer.punctuation_chars for ch in token):
            kind = _classify_args(segment, env)
            if kind == CIDX:
                return CIDX
            found = found or kind
            segment = []
        else:
            segment.append(token)
    return found


def _strip_wrappers(args: List[str]) -> List[str]:
    index = 0
    wrapped = False
    after_command = False
    while index < len(args):
        token = args[index]
        name = os.path.basename(token)
        if name in _WRAPPERS:
            wrapped = True
            after_command = name == "command"
        elif after_command and token.startswith("-") and ("v" in token or "V" in token):
            return []
        elif not (
            _ENV_ASSIGNMENT.fullmatch(token) or (wrapped and token.startswith("-"))
        ):
            break
        index += 1
    return args[index:]


def _is_python(arg: str) -> bool:
    return (
        arg == sys.executable
        or _PYTHON_NAME.fullmatch(os.path.basename(arg)) is not None
    )


def _runs_cidx_module(args: List[str]) -> bool:
    for index, arg in enumerate(args):
        if _is_python(arg):
            return _python_module(args[index + 1 :]) in _CIDX_MODULES
    return False


def _python_module(options: List[str]) -> Optional[str]:
    index = 0
    while index < len(options):
        option = options[index]
        if option == "-m":
            return options[index + 1] if index + 1 < len(options) else None
        if option.startswith("-m"):
            return option[2:]
        if option == "-c" or not option.startswith("-"):
            return None
        index += 2 if option in _PYTHON_OPTIONS_WITH_VALUE else 1
    return None


def _env_path(env: Optional[Mapping[Any, Any]]) -> str:
    if env is None:
        return os.environ.get("PATH", os.defpath)
    for key in ("PATH", b"PATH"):
        if key in env:
            return _text(env[key])
    return os.defpath


def _cidx_executable_runs_code_indexer(
    program: str, env: Optional[Mapping[Any, Any]]
) -> bool:
    # ^ why unverifiable `cidx` => True, not False; guard blocks what it cannot prove fake; test:test_unresolvable_cidx_fails_closed
    resolved = shutil.which(program, path=_env_path(env))
    if resolved is None:
        return True
    try:
        head = _head(resolved)
    except OSError:
        return True
    if b"code_indexer" in head:
        return True
    # ^ why follow named scripts one hop, not stop at the shim; test:test_cidx_shim_delegating_to_a_script_that_runs_the_cli_is_cidx
    for match in _ABSOLUTE_PATH.findall(head)[:_MAX_FOLLOWED_PATHS]:
        target = os.fsdecode(match)
        if target != resolved and os.path.isfile(target):
            try:
                if b"code_indexer" in _head(target):
                    return True
            except OSError:
                return True
    return False


def _head(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(_HEAD_BYTES)


def take_violations(nodeid: str) -> List[str]:
    with _lock:
        return _violations.pop(nodeid, [])


def _spawn_argv_env(
    event: str, args: Tuple[Any, ...]
) -> Tuple[List[Any], Optional[Mapping[Any, Any]]]:
    if event == "os.system":
        return ["/bin/sh", "-c", _text(args[0])], None
    if event == "subprocess.Popen":
        executable, popen_args, _cwd, env = args
        if isinstance(popen_args, (str, bytes, os.PathLike)):
            popen_args = [popen_args]
        argv = list(popen_args)
        # ^ external CPython Popen runs `executable`, keeping args[0] as the child's argv[0]
        if executable is not None and argv:
            argv[0] = executable
        return argv, env
    path, argv, env = args
    return [path, *list(argv)[1:]], env


def _audit(event: str, args: Tuple[Any, ...]) -> None:
    if event not in _WATCHED:
        return
    _on_spawn(event, args)


def _on_spawn(event: str, args: Tuple[Any, ...]) -> None:
    argv, env = _spawn_argv_env(event, args)
    kind = classify_spawn(argv, env)
    if kind is None:
        return
    nodeid, marker_allowed = _active if _active is not None else (None, True)
    allowed = marker_allowed or kind != CIDX
    report_path = os.environ.get(REPORT_ENV)
    if report_path:
        row = {
            "nodeid": nodeid,
            "kind": kind,
            "argv0": _text(argv[0]),
            "allowed": allowed,
        }
        _append_report_row(report_path, row)
        return
    if allowed or nodeid is None:
        return
    shown = shlex.join(_text(a) for a in argv[:_SHOWN_ARGS])
    message = (
        f"{nodeid} started a real {kind} process ({shown}) without @pytest.mark.slow:"
        " mark it slow (slow-automation.sh runs it)"
    )
    with _lock:
        _violations.setdefault(nodeid, []).append(message)
    raise RealProcessInUnitTest(message)


def _append_report_row(report_path: str, row: Dict[str, Any]) -> None:
    global _report_write_failures
    with _lock:
        try:
            with open(report_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError as error:
            _report_write_failures += 1
            if _report_write_failures == 1:
                # ^ invariant report mode never fails a spawn; a broken stderr loses only this warning
                try:
                    sys.stderr.write(
                        f"real-process guard: cannot write {report_path}"
                        f" ({type(error).__name__}); later failures are counted only\n"
                    )
                except (OSError, ValueError):
                    pass


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: Any, nextitem: Any) -> Any:
    global _active
    marker_allowed = any(
        item.get_closest_marker(m) is not None for m in ALLOWING_MARKERS
    )
    _active = (item.nodeid, marker_allowed)
    try:
        yield
    finally:
        _active = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    outcome = yield
    messages = take_violations(item.nodeid)
    if messages:
        report = outcome.get_result()
        report.outcome = "failed"
        report.longrepr = "\n".join(messages)
