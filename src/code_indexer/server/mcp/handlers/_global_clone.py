"""Resolve a published global-repo alias to its golden clone, with access enforced.

``list_repositories`` publishes a globally-activated repo under its USER-facing
alias ``<alias>-global``, while the golden repo is registered under the bare
``<alias>``. Handlers handed the published alias could not look it up.

Two things matter about how that gap is closed:

* **Gate on the same source that publishes the alias.** ``list_repositories``
  derives ``-global`` from the global-repos backend registry, while
  ``GoldenRepoManager.is_globally_active`` consults the file-backed
  ``GlobalRegistry``. Those two disagree in deployments where the file registry is
  not populated. Gating on the registry that publishes the alias means exactly the
  aliases a caller can SEE are the aliases that resolve -- no more, and not fewer.
* **Enforce access.** Resolution alone would let any caller with ``repository:read``
  reach a repo the listing would have filtered out, which is why
  ``_global_fallback.py`` forbids its own promotion helper in these handlers.
  ``require_repo_access`` is the canonical guard (admins bypass; it normalises the
  ``-global`` suffix itself; it fails CLOSED when the service is unwired).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ._utils import _get_global_repo

_GLOBAL_SUFFIX = "-global"


def bare_alias(alias: str) -> str:
    """``"x-global"`` -> ``"x"``; anything else unchanged."""
    if alias.endswith(_GLOBAL_SUFFIX):
        return alias[: -len(_GLOBAL_SUFFIX)]
    return alias


def published_global_clone_path(alias: str, golden_repo_manager: Any) -> Optional[Path]:
    """Golden clone path for an alias the global registry actually publishes.

    Returns ``None`` when the alias is not a published global repo, or when the
    golden repo behind it has no clone path. Callers MUST enforce access
    separately -- this answers "where does it live", never "may you see it".
    """
    if not alias or golden_repo_manager is None:
        return None
    bare = bare_alias(alias)
    if not bare:
        return None
    # The same registry list_repositories publishes from. Best-effort: if it is not
    # reachable (no backend wired, storage error), the answer is "not a published
    # global repo", so the caller keeps whatever it was going to report anyway. It
    # must never convert a plain "not found" into a registry error.
    try:
        if _get_global_repo(f"{bare}{_GLOBAL_SUFFIX}") is None:
            return None
    except Exception:  # noqa: BLE001 - any registry failure means "cannot confirm"
        return None
    repo = golden_repo_manager.get_golden_repo(bare)
    if repo is None:
        return None
    clone_path = getattr(repo, "clone_path", None)
    return Path(clone_path) if clone_path else None
