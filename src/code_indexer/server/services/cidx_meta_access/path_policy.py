"""What a caller may do with one path of the internal catalog repository.

Deny by default: any path the table below does not name is DENY. A new
catalog artefact type must be added here on purpose.

| Path                                        | Restricted caller                  |
|---------------------------------------------|------------------------------------|
| ``<x>.md`` at the root                      | DESCRIPTION if ``x`` is visible    |
| ``memories/<id>.md``                        | MEMORY under the memory scope rule |
| ``dependency-map/_index.md``                | TRANSFORM                          |
| ``dependency-map/<domain>.md``              | TRANSFORM if a participant visible |
| ``dependency-map/_dep_types.md``,           | ALLOW                              |
| ``dependency-map/_analysis_guidelines.md``  |                                    |
| ``xray-patterns/__any__/**``                | ALLOW                              |
| ``xray-patterns/<alias>/**``                | ALLOW if ``alias`` is visible      |
| ``.git``, dot segments, ``*.tmp``, rest     | DENY                               |

Classification always uses the full relative path, never the basename.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, List, Optional, Tuple

from ....utils.path_confinement import has_git_segment
from ..repo_access_guard import normalize_repo_alias
from .visibility import MetaVisibility

DEP_MAP_DIR = "dependency-map"
MEMORIES_DIR = "memories"
XRAY_PATTERNS_DIR = "xray-patterns"
XRAY_ANY_SCOPE = "__any__"
INDEX_FILE = "_index.md"
_DEP_MAP_ALLOWED = frozenset({"_dep_types.md", "_analysis_guidelines.md"})

ParticipantsFn = Callable[[str], Optional[FrozenSet[str]]]
MemoryFn = Callable[[str], bool]


class DecisionKind(enum.Enum):
    ALLOW = "allow"
    DESCRIPTION = "description"
    TRANSFORM = "transform"
    MEMORY = "memory"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    """``subject`` names the alias (DESCRIPTION), domain (TRANSFORM of a
    domain file) or memory id (MEMORY) the path is about."""

    kind: DecisionKind
    subject: Optional[str] = None


_DENY = Decision(DecisionKind.DENY)
_ALLOW = Decision(DecisionKind.ALLOW)


def normalize_meta_path(relative_path: str) -> Optional[Tuple[str, ...]]:
    """Path segments of a catalog-relative path, or None when it is not one
    (empty, absolute, parent traversal, NUL byte, backslash, not a string).

    A backslash is a literal file-name character to the POSIX reader, so a
    path holding one is refused rather than reinterpreted as a separator.
    """
    if not isinstance(relative_path, str) or not relative_path:
        return None
    if "\x00" in relative_path or "\\" in relative_path:
        return None
    if relative_path.startswith("/"):
        return None
    parts = tuple(seg for seg in relative_path.split("/") if seg not in ("", "."))
    if not parts or ".." in parts:
        return None
    return parts


def _root(name: str, vis: MetaVisibility) -> Decision:
    if not name.endswith(".md"):
        return _DENY
    alias = name[: -len(".md")]
    if alias in vis.visible:
        return Decision(DecisionKind.DESCRIPTION, alias)
    return _DENY


def _dependency_map(
    name: str, vis: MetaVisibility, participants_fn: ParticipantsFn
) -> Decision:
    if name == INDEX_FILE:
        return Decision(DecisionKind.TRANSFORM)
    if name in _DEP_MAP_ALLOWED:
        return _ALLOW
    if name.startswith("_") or not name.endswith(".md"):
        return _DENY
    domain = name[: -len(".md")]
    participants = participants_fn(domain)
    if participants and participants & vis.visible:
        return Decision(DecisionKind.TRANSFORM, domain)
    return _DENY


def _memory(name: str, memory_fn: MemoryFn) -> Decision:
    if not name.endswith(".md"):
        return _DENY
    memory_id = name[: -len(".md")]
    if memory_fn(memory_id):
        return Decision(DecisionKind.MEMORY, memory_id)
    return _DENY


def _xray(scope: str, vis: MetaVisibility) -> Decision:
    if scope == XRAY_ANY_SCOPE or normalize_repo_alias(scope) in vis.visible:
        return _ALLOW
    return _DENY


def classify(
    relative_path: str,
    vis: MetaVisibility,
    participants_fn: ParticipantsFn,
    memory_fn: MemoryFn,
) -> Decision:
    """Decide one catalog path for the caller; unrestricted callers get
    ALLOW before any I/O (neither callback is invoked)."""
    if vis.unrestricted:
        return _ALLOW
    parts = normalize_meta_path(relative_path)
    if parts is None or has_git_segment(parts):
        return _DENY
    if any(seg.startswith(".") for seg in parts) or parts[-1].endswith(".tmp"):
        return _DENY
    if len(parts) == 1:
        return _root(parts[0], vis)
    head = parts[0]
    if head == MEMORIES_DIR and len(parts) == 2:
        return _memory(parts[1], memory_fn)
    if head == DEP_MAP_DIR and len(parts) == 2:
        return _dependency_map(parts[1], vis, participants_fn)
    if head == XRAY_PATTERNS_DIR and len(parts) >= 3:
        return _xray(parts[1], vis)
    return _DENY


LISTABLE_KINDS = frozenset(
    {DecisionKind.ALLOW, DecisionKind.DESCRIPTION, DecisionKind.MEMORY}
)


def _membership_not_read(_domain: str) -> Optional[FrozenSet[str]]:
    """Listings never show dependency-map documents (TRANSFORM or DENY
    alike), so their membership is not read."""
    return None


def is_listable(relative_path: str, vis: MetaVisibility, memory_fn: MemoryFn) -> bool:
    """True when a listing may show this catalog file to the caller."""
    return classify(relative_path, vis, _membership_not_read, memory_fn).kind in (
        LISTABLE_KINDS
    )


def listable_paths(
    paths: Iterable[str], vis: MetaVisibility, memory_fn: MemoryFn
) -> List[str]:
    """The catalog files a listing may show the caller, in input order."""
    return [path for path in paths if is_listable(path, vis, memory_fn)]


def may_list_under(
    relative_path: str, vis: MetaVisibility, memory_fn: MemoryFn
) -> bool:
    """True when a listing could show the caller this catalog path or
    something under it, decided from the path alone (no directory read)."""
    if vis.unrestricted:
        return True
    parts = normalize_meta_path(relative_path)
    if not parts or has_git_segment(parts):
        return False
    if any(seg.startswith(".") for seg in parts):
        return False
    if len(parts) == 1 and parts[0] in (DEP_MAP_DIR, MEMORIES_DIR, XRAY_PATTERNS_DIR):
        return True
    if parts[0] == XRAY_PATTERNS_DIR:
        return _xray(parts[1], vis).kind is DecisionKind.ALLOW
    return is_listable(relative_path, vis, memory_fn)
