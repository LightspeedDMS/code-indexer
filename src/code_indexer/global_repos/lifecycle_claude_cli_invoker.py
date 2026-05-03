"""
LifecycleClaudeCliInvoker — Story #876 Phase B-1 Deliverable 1.

Adapter exposing the callable contract required by LifecycleBatchRunner:

    claude_cli_invoker(alias: str, repo_path: Path) -> UnifiedResult

Why a dedicated adapter instead of routing through ClaudeCliManager:
  - LifecycleBatchRunner already owns its own thread-pool concurrency
    (via its `concurrency` parameter). Re-submitting each repo into
    ClaudeCliManager's internal work queue would double-queue and could
    deadlock when the runner's pool exceeds ClaudeCliManager.max_workers.
  - The runner expects a SYNCHRONOUS callable that returns a UnifiedResult.
    ClaudeCliManager.submit_work is fire-and-forget and hands its result
    back via callback — not ergonomic for this call site.
  - repo_analyzer.invoke_claude_cli is the exact same blocking subprocess
    wrapper used by the dependency-map analyzer for identical reasons.

Thread-safety:
  The adapter is called concurrently from LifecycleBatchRunner's thread
  pool.  The unified prompt is loaded ONCE at import time into a module-
  level frozen string (_PROMPT_TEXT) so there is no shared mutable state
  and no lock is required on the hot path.

Timeout budget (Story #885 Phase 5a — config-driven):
  Shell timeout and outer Python timeout are read from ConfigService at
  each call via lifecycle_analysis_config.  Defaults are 360s/420s
  (bumped from 240s/300s in v3 per workshop decision #7, Story #885).
  Operators may hot-reload these values via the Web UI without restarting
  the server — each invocation reads the current config at call time.

Failure contract:
  - On subprocess failure, raises RuntimeError with the alias and the
    upstream error message.
  - LifecycleBatchRunner._run_sub_batch catches Exception at per-repo
    level, logs at ERROR, and proceeds with the other repos in the
    sub-batch (per the runner's documented behaviour).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from code_indexer.global_repos.repo_analyzer import invoke_claude_cli  # noqa: F401 — kept for backward-compat test patching
from code_indexer.global_repos.unified_response_parser import (
    UnifiedResponseParser,
    UnifiedResponseParseError,
    UnifiedResult,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.dep_map_dispatcher_factory import (
    build_dep_map_dispatcher,
)

# Absolute path to the packaged unified prompt.  Resolved once at import
# time.
_PROMPT_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "server"
    / "prompts"
    / "lifecycle_unified.md"
)

# Optional sections that the parser silently skips when absent.
# Used by _parse_with_optional_section_fallback to identify which
# section caused a parse failure so it can be dropped and retried.
_OPTIONAL_SECTIONS = ("branching", "ci", "release")

# Pattern to extract the optional section name from a parse-error message.
# UnifiedResponseParser error messages follow the form:
#   "missing required field: 'lifecycle.<section>.<field>'"
#   "lifecycle.<section> must be an object, got ..."
#   "lifecycle.<section>.<field> value '...' not in ..."
_SECTION_IN_ERROR_RE = re.compile(
    r"lifecycle\.(" + "|".join(_OPTIONAL_SECTIONS) + r")\b"
)


def _parse_with_optional_section_fallback(raw: str) -> UnifiedResult:
    """Attempt UnifiedResponseParser.parse(); drop invalid optional sections and retry.

    The parser treats all fields within a present optional section as required.
    An LLM may emit a section that is structurally partial or contains invalid
    enum values.  Since optional sections are silently skipped when absent, the
    safest recovery is to drop the offending section and retry parsing.

    Retry policy:
      - Maximum 3 retries (one per optional section: branching, ci, release).
      - Only ``UnifiedResponseParseError`` whose message names one of the three
        optional sections triggers a retry; all other errors are re-raised
        immediately.
      - Each retry removes at most one section; if no section is identified in
        the error message the error is re-raised rather than looping indefinitely.

    Args:
        raw: Raw JSON string from the CLI dispatcher.

    Returns:
        A valid ``UnifiedResult`` from the first successful parse attempt.

    Raises:
        UnifiedResponseParseError: When all retries are exhausted or the error
            is not attributable to an optional section.
    """
    payload = raw
    dropped: list = []

    for _attempt in range(len(_OPTIONAL_SECTIONS) + 1):
        try:
            return UnifiedResponseParser.parse(payload)
        except UnifiedResponseParseError as exc:
            match = _SECTION_IN_ERROR_RE.search(str(exc))
            if not match:
                # Error is not from a known optional section — re-raise.
                raise
            section = match.group(1)
            if section in dropped:
                # Already dropped this section once; error persists — re-raise.
                raise
            # Drop the offending optional section and retry.
            try:
                obj = json.loads(payload)
                lifecycle = obj.get("lifecycle")
                if isinstance(lifecycle, dict) and section in lifecycle:
                    del lifecycle[section]
                    payload = json.dumps(obj)
            except json.JSONDecodeError:
                # Cannot modify payload — let the original error propagate.
                raise exc from None
            dropped.append(section)

    # Unreachable: the loop re-raises before this point; guard for type checkers.
    raise UnifiedResponseParseError(  # type: ignore[misc]
        "parse failed after all optional section fallbacks",
        raw=raw,
        validation_errors=["exhausted optional section fallbacks"],
    )


def _load_prompt_eager() -> str:
    """
    Read lifecycle_unified.md at import time.

    Raises FileNotFoundError immediately if the prompt is missing from
    the distribution — a deployment error must surface at startup, not
    on the first call from a worker thread.  Failing fast here keeps
    the adapter entirely stateless on the hot path.
    """
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"lifecycle_unified.md prompt not found at {_PROMPT_PATH}"
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


# Frozen prompt text: computed once at import, never mutated afterwards.
# Thread-safe by construction — no lock needed on the hot path.
_PROMPT_TEXT: str = _load_prompt_eager()


class LifecycleClaudeCliInvoker:
    """
    Callable adapter that runs one CLI invocation per repo (via CliDispatcher)
    and returns a parsed UnifiedResult.

    Stateless: no instance attributes are mutated after construction,
    so a single adapter instance can be safely shared across all threads
    in LifecycleBatchRunner's pool.
    """

    def _build_dispatcher(self):
        """Build a CliDispatcher from the current ServerConfig (Bug #936).

        Delegates to build_dep_map_dispatcher so Codex wiring, weight, and
        Claude fallback are applied consistently with other LLM call sites.
        Returns a fully initialised CliDispatcher — Claude-only when Codex
        is unavailable.
        """
        config = get_config_service().get_config()
        return build_dep_map_dispatcher(config)

    def _validate_repo_inputs(self, alias: str, repo_path: Path) -> Path:
        """Validate alias and repo_path; return a validated Path on success.

        Raises:
            ValueError: if alias is None/empty or repo_path is None,
                missing, or not a directory.
        """
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"alias must be a non-empty string, got {alias!r}")
        if repo_path is None:
            raise ValueError("repo_path must not be None")
        path_obj = Path(repo_path)
        if not path_obj.exists():
            raise ValueError(
                f"repo_path does not exist for alias {alias!r}: {path_obj}"
            )
        if not path_obj.is_dir():
            raise ValueError(
                f"repo_path is not a directory for alias {alias!r}: {path_obj}"
            )
        return path_obj

    def __call__(self, alias: str, repo_path: Path) -> UnifiedResult:
        """Run lifecycle + description prompt and return parsed UnifiedResult.

        Routes through CliDispatcher (Claude or Codex) via flow='repo_lifecycle'.
        Reads timeouts from ConfigService at call time so Web UI changes take
        effect on the next invocation without a server restart (AC-V4-7, #885).

        Raises:
            ValueError: invalid alias or repo_path.
            RuntimeError: dispatcher reports failure.
            UnifiedResponseParseError: output fails schema validation after all
                optional-section fallbacks are exhausted.
        """
        path_obj = self._validate_repo_inputs(alias, repo_path)
        _lifecycle_cfg = get_config_service().get_config().lifecycle_analysis_config
        result = self._build_dispatcher().dispatch(
            flow="repo_lifecycle",
            cwd=str(path_obj),
            prompt=_PROMPT_TEXT,
            timeout=_lifecycle_cfg.outer_timeout_seconds,
        )
        if not result.success:
            raise RuntimeError(
                f"lifecycle dispatcher failed for alias {alias!r}: "
                f"{result.error or result.output}"
            )
        return _parse_with_optional_section_fallback(result.output)
