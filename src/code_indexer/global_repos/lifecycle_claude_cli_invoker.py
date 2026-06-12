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
import logging
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

_logger = logging.getLogger(__name__)

# Absolute path to the packaged unified prompt.  Resolved once at import
# time.
_PROMPT_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "server"
    / "prompts"
    / "lifecycle_unified.md"
)

# Absolute path to the packaged refresh-mode addendum (#1094).  Resolved once at
# import time alongside the unified prompt so a missing file fails fast at
# startup, not on the first refresh from a worker thread.
_REFRESH_ADDENDUM_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "server"
    / "prompts"
    / "lifecycle_refresh_addendum.md"
)

# Placeholder token in lifecycle_unified.md.  In CREATE mode the entire
# placeholder line + its trailing blank line is stripped so the rendered prompt
# is byte-identical to the pre-#1094 file.  In REFRESH mode the bare token is
# replaced with the rendered addendum.
_REFRESH_SECTION_PLACEHOLDER: str = "{{REFRESH_SECTION}}"
_CREATE_PLACEHOLDER_BLOCK: str = _REFRESH_SECTION_PLACEHOLDER + "\n\n"

# Defensive upper bound on the embedded existing description (#1094).  Normal
# READMEs are far smaller; this only guards against a pathological multi-MB body
# blowing up the prompt.  Truncation is explicit (marker + WARNING), never silent.
_MAX_DESCRIPTION_BYTES: int = 64 * 1024
_DESCRIPTION_TRUNCATION_MARKER: str = (
    "\n\n[... existing description truncated at 64 KB for prompt-size safety ...]"
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


def _load_refresh_addendum_eager() -> str:
    """
    Read lifecycle_refresh_addendum.md at import time (#1094).

    Mirrors _load_prompt_eager: a missing addendum is a deployment error that
    must surface at startup, not on the first refresh from a worker thread.
    """
    if not _REFRESH_ADDENDUM_PATH.exists():
        raise FileNotFoundError(
            f"lifecycle_refresh_addendum.md prompt not found at {_REFRESH_ADDENDUM_PATH}"
        )
    return _REFRESH_ADDENDUM_PATH.read_text(encoding="utf-8")


# Frozen prompt text: computed once at import, never mutated afterwards.
# Thread-safe by construction — no lock needed on the hot path.
_PROMPT_TEXT: str = _load_prompt_eager()

# Frozen refresh addendum (#1094): computed once at import, never mutated.
_REFRESH_ADDENDUM_TEXT: str = _load_refresh_addendum_eager()


def _cap_description(alias: str, existing_description: str) -> str:
    """Defensively cap *existing_description* at _MAX_DESCRIPTION_BYTES (#1094).

    Normal descriptions are far below the cap.  When the cap is exceeded the
    body is truncated at a UTF-8 char boundary, a clear truncation marker is
    appended, and a structured WARNING naming the alias and the original length
    is logged (Messi Rule #13 — never truncate silently).
    """
    encoded = existing_description.encode("utf-8")
    if len(encoded) <= _MAX_DESCRIPTION_BYTES:
        return existing_description

    # Truncate on the byte budget, then decode back ignoring a possibly-split
    # final multibyte char.
    truncated = encoded[:_MAX_DESCRIPTION_BYTES].decode("utf-8", errors="ignore")
    _logger.warning(
        "lifecycle-refresh: existing description for alias %r exceeds the 64KB "
        "prompt cap (original_length=%d bytes); truncating to %d bytes",
        alias,
        len(encoded),
        _MAX_DESCRIPTION_BYTES,
    )
    return truncated + _DESCRIPTION_TRUNCATION_MARKER


def _render_prompt(
    alias: str,
    existing_description: str | None,
    last_analyzed: str | None,
) -> str:
    """Render the dispatch prompt for *alias* (#1094).

    CREATE mode (existing_description empty/whitespace/None): strip the
    {{REFRESH_SECTION}} placeholder block so the result is byte-identical to the
    pre-#1094 lifecycle_unified.md.

    REFRESH mode (non-empty existing_description): substitute the placeholder
    with the rendered refinement addendum — the existing body embedded between
    the DATA markers and the last_analyzed stamp filled in.
    """
    if existing_description is None or not existing_description.strip():
        # CREATE mode — byte-identical to the original unified prompt.
        return _PROMPT_TEXT.replace(_CREATE_PLACEHOLDER_BLOCK, "", 1)

    capped = _cap_description(alias, existing_description)
    addendum = _REFRESH_ADDENDUM_TEXT.replace(
        "{{LAST_ANALYZED}}", last_analyzed or "unknown"
    ).replace("{{EXISTING_DESCRIPTION}}", capped)
    # The placeholder line is followed by a blank line in the source file; replace
    # only the bare token so the addendum slots in with surrounding separation.
    return _PROMPT_TEXT.replace(_REFRESH_SECTION_PLACEHOLDER, addendum, 1)


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

    def __call__(
        self,
        alias: str,
        repo_path: Path,
        *,
        existing_description: str | None = None,
        last_analyzed: str | None = None,
    ) -> UnifiedResult:
        """Run lifecycle + description prompt and return parsed UnifiedResult.

        Routes through CliDispatcher (Claude or Codex) via flow='repo_lifecycle'.
        Reads timeouts from ConfigService at call time so Web UI changes take
        effect on the next invocation without a server restart (AC-V4-7, #885).

        #1094 refresh-awareness: when *existing_description* is a non-empty
        string the prompt is rendered in REFRESH mode (the existing body is
        embedded for refinement and *last_analyzed* is stamped into the
        change-scoping instruction).  When it is None/empty/whitespace the prompt
        is byte-identical to the create-mode unified prompt.  Both new params are
        keyword-only with defaults so existing positional (alias, repo_path) call
        sites — including MagicMock-based tests — keep working unchanged.

        Raises:
            ValueError: invalid alias or repo_path.
            RuntimeError: dispatcher reports failure.
            UnifiedResponseParseError: output fails schema validation after all
                optional-section fallbacks are exhausted.
        """
        path_obj = self._validate_repo_inputs(alias, repo_path)
        _lifecycle_cfg = get_config_service().get_config().lifecycle_analysis_config
        prompt = _render_prompt(alias, existing_description, last_analyzed)
        result = self._build_dispatcher().dispatch(
            flow="repo_lifecycle",
            cwd=str(path_obj),
            prompt=prompt,
            timeout=_lifecycle_cfg.outer_timeout_seconds,
        )
        if not result.success:
            raise RuntimeError(
                f"lifecycle dispatcher failed for alias {alias!r}: "
                f"{result.error or result.output}"
            )
        return _parse_with_optional_section_fallback(result.output)
