"""SCIP indexer whose compile step runs in a plugin's execution backend.

``scip_backend='delegated-plugin'``: the successor of :class:`DelegatedIndexer`
for a cidx pod with no repository mount. ``DelegatedIndexer`` asks sandbox-mcp's
deprecated ``sandbox_scip`` tool to write ``index.scip`` into the shared golden
volume and then waits for the file to appear there -- which a mount-less pod
can never see. Here the plugin (``code_indexer.scip_backend``; evo-sandbox's
runs sandbox-mcp's ``scip`` run profile) RETURNS the index bytes, and this
indexer writes them to ``output_dir`` -- the project's directory under the
repository's index root (``scip.paths.scip_index_dir``), the IndexStore path
every SCIP reader and the index-artifact publish already use. Nothing is read
from, or written to, the repository.

Semantics are ``DelegatedIndexer``'s, outcome for outcome:

* ``ok`` -> SUCCESS with ``output_file`` (``degenerate`` carried through), but
  only after cidx re-hashes the bytes and they match the producer's sha256;
* ``unsupported`` -> FAILED with exit 69, the benign "no indexer for this
  language" skip that does not block freshness;
* ``failed`` -> FAILED (``backpressure`` adds the marker the build queue
  retries on);
* a raised exception, a non-outcome return, missing/mismatched bytes, an
  unknown status -> FAILED, logged.

FAIL LOUD, no fallback: every failure is a FAILED project, never a local build
(the pod has neither the toolchain nor the repository). A stale ``index.scip``
is removed before dispatch so a failed build can never leave the previous
build's index looking like this run's.
"""

from __future__ import annotations

import logging
import os
import posixpath
import time
from pathlib import Path
from typing import Optional

from ..delegation_api import (
    DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    ScipBuildOutcome,
    ScipBuildRequest,
    ScipExecutionBackend,
    sha256_hex,
)
from .base import (
    DELEGATED_BACKPRESSURE_MARKER,
    UNSUPPORTED_LANGUAGE_EXIT_CODE,
    IndexerResult,
    IndexerStatus,
    SCIPIndexer,
)

logger = logging.getLogger(__name__)

#: The file name every SCIP reader looks for in a project's output dir.
INDEX_FILENAME = "index.scip"
#: Exit code recorded for a FAILED project (any non-zero, non-69 value works:
#: it only has to read as "not unsupported").
_FAILED_EXIT_CODE = 1
#: How much of the backend's diagnostic log a result keeps.
_LOG_TAIL_CHARS = 4000


class PluginScipIndexer(SCIPIndexer):
    """SCIP indexer delegating every language to one ``ScipExecutionBackend``."""

    def __init__(
        self,
        backend: ScipExecutionBackend,
        repo_root: Path,
        *,
        timeout_seconds: int = DEFAULT_SCIP_BUILD_TIMEOUT_SECONDS,
    ) -> None:
        self._backend = backend
        self._repo_root = Path(repo_root)
        self._timeout = timeout_seconds

    # --------------------------------------------------------------------- ABC
    def generate(
        self,
        project_dir: Path,
        output_dir: Path,
        build_system: str,
        *,
        workspace_id: Optional[str] = None,
        golden_alias: Optional[str] = None,
        language: Optional[str] = None,
        allow_degenerate: bool = False,
    ) -> IndexerResult:
        start = time.time()
        if workspace_id:
            return self._failure(
                start,
                "the delegated-plugin SCIP backend addresses golden repositories "
                "by path/alias only; a workspace_id build is not supported",
            )
        if not language:
            return self._failure(
                start, "the discovered project language is required; none was given"
            )
        project_path = self._project_path(project_dir)
        if project_path is None:
            return self._failure(
                start,
                f"project dir {project_dir} is not under repo root {self._repo_root}",
            )
        stale_error = _remove_stale_index(Path(output_dir))
        if stale_error is not None:
            return self._failure(start, stale_error)

        request = ScipBuildRequest(
            repo_path=os.path.abspath(str(self._repo_root)),
            golden_alias=golden_alias or None,
            project_path=project_path,
            build_system=build_system,
            language=language,
            allow_degenerate=allow_degenerate,
            timeout_seconds=self._timeout,
        )
        logger.info(
            "PluginScipIndexer: building project '%s' (language=%s, build_system=%s, "
            "alias=%s) via %s",
            project_path,
            language,
            build_system,
            golden_alias or "-",
            type(self._backend).__name__,
        )
        try:
            outcome = self._backend.build_index(request)
        except Exception as exc:  # fail loud as a FAILED project, never raise
            return self._failure(
                start,
                f"SCIP backend {type(self._backend).__name__} raised "
                f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(outcome, ScipBuildOutcome):
            return self._failure(
                start,
                f"SCIP backend returned {type(outcome).__name__}, not a ScipBuildOutcome",
            )
        return self._map_outcome(outcome, Path(output_dir), start)

    def is_available(self) -> bool:
        """The backend's own reachability probe (False on any error)."""
        try:
            return bool(self._backend.is_available())
        except Exception as exc:  # a probe never raises into startup wiring
            logger.warning("PluginScipIndexer: availability probe raised: %s", exc)
            return False

    def get_version(self) -> Optional[str]:
        """None: the indexer runs elsewhere; each outcome names its own."""
        return None

    # ----------------------------------------------------------------- helpers
    def _project_path(self, project_dir: Path) -> Optional[str]:
        """``project_dir`` relative to the repo root, lexically (never touching disk)."""
        root = os.path.normpath(os.path.abspath(str(self._repo_root)))
        target = os.path.normpath(os.path.abspath(str(project_dir)))
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        if rel == ".." or rel.startswith("../") or posixpath.isabs(rel):
            return None
        return rel

    def _map_outcome(
        self, outcome: ScipBuildOutcome, output_dir: Path, start: float
    ) -> IndexerResult:
        duration = (
            outcome.duration_ms / 1000.0
            if isinstance(outcome.duration_ms, (int, float))
            and not isinstance(outcome.duration_ms, bool)
            else time.time() - start
        )
        identity = _identity(outcome)
        log_tail = (outcome.log or "")[-_LOG_TAIL_CHARS:]

        if outcome.status == STATUS_UNSUPPORTED:
            message = (
                f"no SCIP indexer for language in the delegated backend{identity}: "
                f"{outcome.error or 'unsupported'}"
            )
            logger.info("PluginScipIndexer: %s", message)
            return IndexerResult(
                status=IndexerStatus.FAILED,
                duration_seconds=duration,
                output_file=None,
                stdout=log_tail,
                stderr=message,
                exit_code=UNSUPPORTED_LANGUAGE_EXIT_CODE,
            )
        if outcome.status == STATUS_FAILED:
            message = f"delegated SCIP build failed{identity}: {outcome.error or 'no reason given'}"
            if outcome.backpressure:
                message = f"{message} ({DELEGATED_BACKPRESSURE_MARKER})"
            return self._failure(start, message, duration=duration, stdout=log_tail)
        if outcome.status != STATUS_OK:
            return self._failure(
                start,
                f"SCIP backend reported unknown status {outcome.status!r}",
                duration=duration,
                stdout=log_tail,
            )

        data = outcome.index_bytes
        if not isinstance(data, (bytes, bytearray)) or not data:
            return self._failure(
                start, "SCIP backend reported ok without index bytes", duration=duration
            )
        actual = sha256_hex(bytes(data))
        if not isinstance(outcome.sha256, str) or actual != outcome.sha256.lower():
            return self._failure(
                start,
                "SCIP index sha256 mismatch: the backend reported "
                f"{outcome.sha256!r}, the received {len(data)} bytes hash to {actual}",
                duration=duration,
            )
        try:
            scip_file = _write_index(output_dir, bytes(data))
        except OSError as exc:
            return self._failure(
                start,
                f"could not write the delegated index under {output_dir}: {exc}",
                duration=duration,
            )
        if outcome.degenerate:
            logger.info(
                "PluginScipIndexer: DEGENERATE (locals-only) index at %s: %s",
                scip_file,
                outcome.degraded_reason or "no reason given",
            )
        return IndexerResult(
            status=IndexerStatus.SUCCESS,
            duration_seconds=duration,
            output_file=scip_file,
            stdout=f"index.scip {len(data)} bytes sha256={actual}{identity}",
            stderr="",
            exit_code=0,
            degenerate=bool(outcome.degenerate),
        )

    def _failure(
        self,
        start: float,
        message: str,
        *,
        duration: Optional[float] = None,
        stdout: str = "",
    ) -> IndexerResult:
        logger.warning("PluginScipIndexer: %s", message)
        return IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=duration if duration is not None else time.time() - start,
            output_file=None,
            stdout=stdout,
            stderr=message,
            exit_code=_FAILED_EXIT_CODE,
        )


def _identity(outcome: ScipBuildOutcome) -> str:
    if not outcome.indexer_name:
        return ""
    return f" ({outcome.indexer_name}@{outcome.indexer_version or '?'})"


def _remove_stale_index(output_dir: Path) -> Optional[str]:
    """Delete a previous ``index.scip``; an error message if that fails."""
    try:
        (output_dir / INDEX_FILENAME).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"could not remove stale {INDEX_FILENAME} in {output_dir}: {exc}"
    return None


def _write_index(output_dir: Path, data: bytes) -> Path:
    """Write ``index.scip`` atomically (temp file + rename) and return its path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / INDEX_FILENAME
    tmp = output_dir / f".{INDEX_FILENAME}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return final
