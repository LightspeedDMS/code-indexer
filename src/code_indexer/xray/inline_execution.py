"""X-Ray over a pod-local repository, executed in the delegated sandbox.

A pod-local repository (``local://`` other than cidx-meta: Langfuse trace
folders) exists only on this pod, so the delegated X-Ray backend's sandbox
cannot mount it. The evaluator is caller-supplied code and must still never
run here. So under ``xray_backend=delegated`` :class:`PodLocalInlineXrayBackend`
reads the candidate files -- this pod's own data -- and hands their bytes to
the delegated plugin (``execute_scan_inline`` / ``execute_graph_analysis_inline``,
cidx-content-backend >= 0.10.0), which runs the evaluator in sandbox-mcp's
mountless ``xray-inline`` profile.

Limits are the plugin's (``inline_limits``: files per request, bytes per file,
bytes per request). A scan is batched under them -- several sandbox runs,
merged back into candidate order -- and the merge is exactly what one run would
have returned: every file is scanned independently (a path listed twice stays in
one run, as xray-cli scans it twice), results come back one tuple per spec in
spec order, and the first whole-batch failure is the answer (as it would be for
one run). Debug lines are the same lines, batch after batch; xray-cli gathers
them in rayon completion order, so not even one run fixes their order. A
graph is built over all of its files at once, so it is never split: over a
limit it is refused.

FAIL LOUD. Every refusal is a whole-batch error tuple (``file_path == ""``) or a
``_graph_error_result``, before the sandbox is asked: a file over the per-file
limit, a candidate whose real path leaves the repository, a candidate that
cannot be read, or a plugin without inline support. Nothing is truncated and
nothing falls back to running here.
"""

from __future__ import annotations

import logging
import math
import os
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from code_indexer.xray.execution_backend import (
    BatchResult,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)

logger = logging.getLogger(__name__)

#: Error types of this module's refusals (whole-batch / graph errors).
INLINE_LIMIT_EXCEEDED = "SandboxInlineLimitExceeded"
INLINE_READ_REFUSED = "InlineReadRefused"
INLINE_UNSUPPORTED = "InlineExecutionUnsupported"
#: The plugin release that first carries inline execution.
INLINE_PLUGIN_REQUIREMENT = "cidx-content-backend >= 0.10.0"
#: What a delegated backend must offer for inline execution.
_INLINE_METHODS = (
    "execute_scan_inline",
    "execute_graph_analysis_inline",
    "inline_limits",
)


class InlineExecutionError(Exception):
    """A pod-local request that cannot be sent: reported, never retried here."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class InlineLimits:
    """What one inline request may carry (decoded bytes)."""

    max_files: int
    max_file_bytes: int
    max_total_bytes: int


def read_candidates(
    repo_path: str,
    rel_paths: Sequence[str],
    limits: Optional[InlineLimits] = None,
) -> Dict[str, bytes]:
    """The bytes of every candidate that is a regular file in *repo_path*.

    A candidate that does not exist, or is not a regular file, is left out:
    the sandbox then does not have it either, which is exactly how xray-cli
    sees a missing file in a mounted repository. A candidate whose real path
    leaves the repository (a symlink out of it) is refused: this pod's other
    files never leave it. Any other read failure is refused too.

    When *limits* is given, a file whose ``st_size`` already exceeds the
    per-file ceiling is refused before its bytes are read into memory.

    Raises:
        InlineExecutionError: an escape, an unreadable candidate, or a
            candidate that exceeds the inline size limit.
    """
    root = Path(repo_path).resolve()
    ceiling = (
        min(limits.max_file_bytes, limits.max_total_bytes) if limits else None
    )
    files: Dict[str, bytes] = {}
    for index, rel in enumerate(dict.fromkeys(rel_paths)):
        path = Path(os.path.realpath(root / rel))
        if path != root and root not in path.parents:
            raise InlineExecutionError(
                INLINE_READ_REFUSED,
                f"candidate {index} resolves outside the repository; "
                "it is not sent to the X-Ray sandbox",
            )
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InlineExecutionError(
                INLINE_READ_REFUSED, f"candidate {index} cannot be read: {exc.strerror}"
            ) from exc
        if not stat.S_ISREG(st.st_mode):
            continue
        if ceiling is not None and st.st_size > ceiling:
            raise InlineExecutionError(
                INLINE_LIMIT_EXCEEDED,
                f"candidate {index} is {st.st_size} bytes, which exceeds the "
                f"X-Ray sandbox's per-file limit of {ceiling} bytes",
            )
        try:
            files[rel] = path.read_bytes()
        except OSError as exc:
            raise InlineExecutionError(
                INLINE_READ_REFUSED, f"candidate {index} cannot be read: {exc.strerror}"
            ) from exc
    return files


def check_file_sizes(files: Mapping[str, bytes], limits: InlineLimits) -> None:
    """Refuse any one file the sandbox could never take, whatever the batching:
    over the per-file limit, or alone over the per-request total."""
    ceiling = min(limits.max_file_bytes, limits.max_total_bytes)
    for data in files.values():
        if len(data) > ceiling:
            raise InlineExecutionError(
                INLINE_LIMIT_EXCEEDED,
                f"a candidate file of {len(data)} bytes exceeds the X-Ray sandbox's "
                f"per-file limit of {ceiling} bytes",
            )


def check_request_size(files: Mapping[str, bytes], limits: InlineLimits) -> None:
    """Refuse a request that does not fit ONE sandbox run (a graph)."""
    check_file_sizes(files, limits)
    if len(files) > limits.max_files:
        raise InlineExecutionError(
            INLINE_LIMIT_EXCEEDED,
            f"{len(files)} files exceed the X-Ray sandbox's limit of "
            f"{limits.max_files} per run; a graph is never split",
        )
    total = sum(len(data) for data in files.values())
    if total > limits.max_total_bytes:
        raise InlineExecutionError(
            INLINE_LIMIT_EXCEEDED,
            f"{total} bytes of files exceed the X-Ray sandbox's limit of "
            f"{limits.max_total_bytes} per run; a graph is never split",
        )


def plan_batches(
    rel_paths: Sequence[str], files: Mapping[str, bytes], limits: InlineLimits
) -> List[List[int]]:
    """Spec indices per sandbox run (each ascending), every run within limits.

    Greedy and deterministic over the DISTINCT paths in first-occurrence order:
    a run takes paths until the next would push it over ``max_files`` paths or
    ``max_total_bytes``. Every occurrence of a path travels in the same run --
    xray-cli scans a path listed twice twice, so splitting the occurrences
    would change their findings. A path absent from *files* weighs nothing but
    still counts (the runner lists it). Every file fits a run on its own
    (:func:`check_file_sizes` ran first).
    """
    occurrences: Dict[str, List[int]] = {}
    for index, rel in enumerate(rel_paths):
        occurrences.setdefault(rel, []).append(index)
    batches: List[List[int]] = []
    current: List[int] = []
    paths = 0
    total = 0
    for rel, indices in occurrences.items():
        weight = len(files[rel]) if rel in files else 0
        if current and (
            paths >= limits.max_files or total + weight > limits.max_total_bytes
        ):
            batches.append(sorted(current))
            current, paths, total = [], 0, 0
        current.extend(indices)
        paths += 1
        total += weight
    if current:
        batches.append(sorted(current))
    return batches


def is_whole_batch_error(results: BatchResult) -> bool:
    """One tuple whose error says nothing was scanned (``file_path == ""``)."""
    if len(results) != 1:
        return False
    _matches, errors, _meta = results[0]
    return bool(errors) and errors[0].get("file_path") == ""


class PodLocalInlineXrayBackend:
    """``XrayExecutionBackend`` for ONE pod-local repository: reads the
    candidates here, executes them in the delegated backend's sandbox."""

    def __init__(
        self,
        delegated: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delegated = delegated
        self._clock = clock

    def _limits(self) -> InlineLimits:
        missing = [n for n in _INLINE_METHODS if not hasattr(self._delegated, n)]
        if missing:
            raise InlineExecutionError(
                INLINE_UNSUPPORTED,
                "X-Ray over a pod-local repository runs in the delegated sandbox "
                f"with the files sent inline, which needs {INLINE_PLUGIN_REQUIREMENT}; "
                f"the installed delegated X-Ray backend has no {', '.join(missing)}",
            )
        raw = self._delegated.inline_limits
        return InlineLimits(
            max_files=int(raw.max_files),
            max_file_bytes=int(raw.max_file_bytes),
            max_total_bytes=int(raw.max_total_bytes),
        )

    # -- scan -----------------------------------------------------------------

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        from code_indexer.xray import delegation_api as api

        specs = request.file_specs
        if not specs:
            return XrayScanOutcome([])
        rel_paths = [str(spec.get("file_path", "")) for spec in specs]
        try:
            limits = self._limits()
            files = read_candidates(
                request.repo_path or os.getcwd(), rel_paths, limits=limits
            )
            check_file_sizes(files, limits)
        except InlineExecutionError as exc:
            return XrayScanOutcome([api.error_tuple("", exc.error_type, exc.message)])
        batches = plan_batches(rel_paths, files, limits)
        logger.info(
            "X-Ray pod-local repository %s: %d candidate(s), %d file(s) read, "
            "%d inline sandbox run(s)",
            request.repo_path,
            len(specs),
            len(files),
            len(batches),
        )
        deadline = self._clock() + request.timeout_seconds
        merged: List[Optional[Tuple[Any, Any, Any]]] = [None] * len(specs)
        debug: Optional[List[str]] = None
        for number, batch in enumerate(batches):
            remaining = deadline - self._clock()
            if number > 0 and remaining <= 0:
                message = api.xray_cli_timeout_message(request.timeout_seconds)
                return XrayScanOutcome(
                    [
                        api.error_tuple(
                            "", "XRayCliError", api.sanitize_error_message(message)
                        )
                    ]
                )
            batch_rels = {rel_paths[i] for i in batch}
            sub_request = replace(
                request,
                file_specs=[specs[i] for i in batch],
                timeout_seconds=max(1, math.ceil(remaining)),
            )
            outcome: XrayScanOutcome = self._delegated.execute_scan_inline(
                sub_request, {rel: files[rel] for rel in batch_rels if rel in files}
            )
            if is_whole_batch_error(outcome.results):
                return outcome
            if len(outcome.results) != len(batch):
                return XrayScanOutcome(
                    [
                        api.error_tuple(
                            "",
                            "SandboxContractError",
                            f"inline X-Ray run {number + 1} of {len(batches)} answered "
                            f"{len(outcome.results)} results for {len(batch)} files",
                        )
                    ]
                )
            for index, result in zip(batch, outcome.results):
                merged[index] = result
            if outcome.debug_messages is not None:
                debug = (debug or []) + list(outcome.debug_messages)
        results: BatchResult = [r for r in merged if r is not None]
        return XrayScanOutcome(results, debug_messages=debug)

    # -- graph ----------------------------------------------------------------

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        from code_indexer.xray import delegation_api as api

        try:
            limits = self._limits()
            files = read_candidates(
                request.repo_root, list(request.file_paths), limits=limits
            )
            check_request_size(files, limits)
        except InlineExecutionError as exc:
            return api.graph_error_result(exc.error_type, exc.message)
        logger.info(
            "X-Ray graph over pod-local repository %s: %d file(s) read, one inline "
            "sandbox run",
            request.repo_root,
            len(files),
        )
        result: Dict[str, Any] = self._delegated.execute_graph_analysis_inline(
            request, files
        )
        return result
