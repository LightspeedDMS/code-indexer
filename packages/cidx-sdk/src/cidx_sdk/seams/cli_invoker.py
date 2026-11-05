"""The CLI invoker seam: where description/analysis LLM calls execute.

Entry-point group ``code_indexer.cli_invoker``. ``local`` (no plugin) runs the
built-in ``claude`` CLI invoker in this pod; ``delegated`` hands every call to
the plugin's invoker (an agent runner). Unlike the other seams the factory
takes two arguments, the analysis model and the soft timeout, mirroring what
the built-in invoker is constructed with.

The result types (:class:`InvocationResult`, :class:`FailureClass`) live here
so a delegate can build them without importing the core; the core's
``server/services/intelligence_cli_invoker.py`` re-exports them.

A delegate that runs the analysis OFF this pod needs to know where the
repository behind the ``cwd`` it is handed comes from. The core answers that
through :data:`REPOSITORY_SOURCE` (a :class:`GoldenRepositorySource`), which it
installs when it loads the seam; a delegate reads the slot instead of
importing the core's wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, Union, runtime_checkable

from cidx_sdk.slots import CapabilitySlot

#: Module attribute a plugin sets to True when its invoker provisions the
#: golden repository ITSELF and never reads the ``cwd`` it is handed -- so it
#: can analyze a repository this pod does not hold.
RESOLVES_GOLDEN_REPOSITORIES = "RESOLVES_GOLDEN_REPOSITORIES"


class FailureClass(Enum):
    """Classification of CLI invocation failures.

    RETRYABLE_ON_SAME  - network errors, timeout transients: retry once on the same CLI.
    RETRYABLE_ON_OTHER - all other non-retryable failures: failover to the alternate CLI.
    """

    RETRYABLE_ON_SAME = "retryable_on_same"
    RETRYABLE_ON_OTHER = "retryable_on_other"


@dataclass
class InvocationResult:
    """Result of a single CLI invocation attempt.

    Fields:
        success:       True when the CLI produced usable output.
        output:        The usable text output from the CLI (empty on failure).
        error:         Human-readable error description (empty on success).
        cli_used:      Which CLI ran: "claude", "codex", or a delegate's name.
        was_failover:  True if this result came from the failover CLI, not the primary.
        failure_class: Set only when success=False; classifies retry strategy.
    """

    success: bool
    output: str
    error: str
    cli_used: str
    was_failover: bool
    failure_class: Optional[FailureClass] = None


@runtime_checkable
class CliInvoker(Protocol):
    """Runs one LLM flow and returns an :class:`InvocationResult`.

    Never raises for a failed run: a failure is a result with
    ``success=False`` and a ``failure_class``, because the core's dispatcher
    decides retry and failover from it.
    """

    def invoke(
        self,
        flow: str,
        cwd: str,
        prompt: str,
        timeout: int,
        max_turns: int = 0,
    ) -> Any: ...


#: What a ``code_indexer.cli_invoker`` entry point resolves to:
#: (analysis_model, soft_timeout_seconds) -> CliInvoker.
CliInvokerFactory = Callable[[str, Optional[int]], Any]


@runtime_checkable
class GoldenRepositorySource(Protocol):
    """Where the repository behind an invocation's ``cwd`` comes from.

    Installed by the core in :data:`REPOSITORY_SOURCE`. A delegate that hands
    the analysis to an agent off this pod asks it before submitting.
    """

    def holds_repositories(self) -> bool:
        """True when repositories are on this pod's disk (the content seam is
        local): the ``cwd`` is a real clone the delegate may read."""
        ...

    def golden_repository(self, cwd: Union[str, Path]) -> Optional[Dict[str, str]]:
        """The golden repository at *cwd* on a pod that does not hold it:
        ``{"alias", "repoUrl", "branch", "commit"}``, or None when *cwd* is
        not a golden repository. Raises when it IS one and the answer cannot
        be determined -- never a silent "no repository"."""
        ...


#: The core's :class:`GoldenRepositorySource`. Empty when no core runs in this
#: process (a delegate's own tests); a delegate then treats ``cwd`` as a
#: local clone.
REPOSITORY_SOURCE: CapabilitySlot[GoldenRepositorySource] = CapabilitySlot(
    "cli_invoker.repository_source"
)
