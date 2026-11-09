"""The git invoker seam: where a single git command line executes.

Entry-point group ``code_indexer.git_invoker``. Placed at the one chokepoint
every git call in the core bottoms out at (``utils.git_runner``), so internal
git and the user-facing git tools delegate through one point. ``local`` (no
invoker) runs ``subprocess`` here; ``delegated`` hands MUTATING commands to the
invoker (the git service), while reads stay in place. Which commands mutate is the
core's policy (``utils.git_invoker.is_mutating_git``), not the invoker's.

Moved from ``code_indexer.utils.git_invoker``, which re-exports it.
"""

from __future__ import annotations

import subprocess
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class GitInvoker(Protocol):
    """Executes a git command somewhere other than the cidx process.

    ``run`` mirrors the slice of ``subprocess.run`` that cidx callers actually
    read back: it returns a ``CompletedProcess`` whose ``returncode``/``stdout``/
    ``stderr`` reflect the delegated execution. It must NOT raise on a non-zero
    git exit -- the runner applies ``check`` semantics uniformly for both the
    native and delegated paths, so the invoker just reports the result.
    """

    def run(
        self,
        cmd: Sequence[str],
        cwd: Any,
        *,
        timeout: Optional[float] = None,
    ) -> "subprocess.CompletedProcess[str]": ...


#: What a ``code_indexer.git_invoker`` entry point resolves to: () -> GitInvoker.
GitInvokerFactory = Callable[[], GitInvoker]
