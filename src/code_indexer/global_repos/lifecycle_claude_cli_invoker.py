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

Failure contract:
  - On subprocess failure, raises RuntimeError with the alias and the
    upstream error message.
  - LifecycleBatchRunner._run_sub_batch catches Exception at per-repo
    level, logs at ERROR, and proceeds with the other repos in the
    sub-batch (per the runner's documented behaviour).
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.repo_analyzer import invoke_claude_cli
from code_indexer.global_repos.unified_response_parser import (
    UnifiedResponseParser,
    UnifiedResult,
)

# Phase 2 timeouts (seconds). The unified prompt asks Claude to spend
# "approximately 2 minutes exploring" — we cap the shell-level timeout
# at 180s and give the outer Python subprocess 240s so timeout signals
# propagate cleanly before Python itself kills the process.
_SHELL_TIMEOUT_SECONDS: int = 180
_OUTER_TIMEOUT_SECONDS: int = 240

# Absolute path to the packaged unified prompt.  Resolved once at import
# time.
_PROMPT_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "server"
    / "prompts"
    / "lifecycle_unified.md"
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
    Callable adapter that runs one Claude CLI invocation per repo and
    returns a parsed UnifiedResult.

    Stateless: no instance attributes are mutated after construction,
    so a single adapter instance can be safely shared across all threads
    in LifecycleBatchRunner's pool.
    """

    def __call__(self, alias: str, repo_path: Path) -> UnifiedResult:
        """
        Run the unified lifecycle + description prompt against *repo_path*
        and return the parsed result.

        Defensive input validation (Messi Rule #15 — Defensive-Invariants):
          alias must be a non-empty string; repo_path must be a Path (or
          string path) to an existing directory.  A violation here would
          otherwise surface deep inside the subprocess layer with a
          generic OSError — unhelpful for diagnosing a fleet-wide batch
          failure.

        Args:
            alias: Repository alias (for error-message diagnostics).
            repo_path: Absolute path to the golden-repo base clone.
                Used as the subprocess cwd so Claude's Read/Bash/Glob
                tools resolve against the repo's files.

        Returns:
            UnifiedResult with validated description and lifecycle.

        Raises:
            ValueError: if alias is None / empty, or if repo_path is
                None, does not exist, or is not a directory.
            RuntimeError: if the subprocess wrapper reports failure
                (non-zero exit, timeout, or unexpected exception).
                The message includes the alias and the upstream error
                text for operator diagnostics.
            UnifiedResponseParseError: if the CLI succeeds but returns
                output that fails schema validation.  Propagates from
                UnifiedResponseParser.parse — the batch runner logs it
                and proceeds with other repos.
        """
        # -- Entry-point validation ----------------------------------------
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(
                f"alias must be a non-empty string, got {alias!r}"
            )
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

        # -- Subprocess invocation -----------------------------------------
        success, raw_output = invoke_claude_cli(
            str(path_obj),
            _PROMPT_TEXT,
            _SHELL_TIMEOUT_SECONDS,
            _OUTER_TIMEOUT_SECONDS,
        )

        if not success:
            raise RuntimeError(
                f"lifecycle Claude CLI failed for alias {alias!r}: {raw_output}"
            )

        # Parser raises UnifiedResponseParseError on schema violations;
        # let that propagate so the batch runner logs the parse error.
        return UnifiedResponseParser.parse(raw_output)
