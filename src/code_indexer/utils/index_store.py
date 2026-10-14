"""Where a repository's cidx index state lives.

cidx keeps a repository's indexes -- vectors, the FTS and trigram indexes, SCIP
databases, metadata and per-repo config -- under ``<repo>/.code-indexer/``. That
couples cidx's own state to the repository volume: removing the repo mount
(the content seam) would delete the index with the checkout, and every pod that
reads an index needs the volume the repository lives on.

The decision (content seam plan, phase 5) is to abstract the state out rather
than move it to another volume. The index engines -- hnswlib graphs, SQLite
stores, tantivy segments -- are embedded libraries that need a local
filesystem, so the abstraction is over WHERE an index lives and, later, how it
is versioned:

1. (this module) callers ask an ``IndexStore`` for a repository's index root
   instead of joining ``.code-indexer`` onto the repo path. The only backend is
   ``LocalIndexStore``, which answers exactly that join -- no behaviour change.
   tests/unit/utils/test_index_store.py fails the build on any new
   direct derivation, so the remaining ones can only migrate behind this seam.
2. (also this module) indexes as versioned artifacts: ``DelegatedIndexStore``
   publishes a built index to an artifact service (workspace-mcp's index store,
   via the ``code_indexer.index_artifacts`` plugin) as an immutable version,
   and ``checkout`` fetches a version, verified, for readers. Selected by the
   server's ``index_store_backend``; ``LocalIndexStore`` publishes and checks
   out nothing, so the CLI and default server are unchanged.
3. an object-store backend, with the alias -> version pointer in Postgres.

Kept free of server imports: plain CLI indexing uses it too.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Union

#: The index directory's name inside a repository under the local backend.
#: Exported for the code that must SKIP it while walking or searching a
#: repository (ripgrep globs, tree walkers): while indexes live in the
#: repository, every reader of the repository has to step around them.
INDEX_DIRNAME = ".code-indexer"


class IndexStore(Protocol):
    """Answers where a repository's cidx state is, for this process to use.

    Step 2 adds the artifact lifecycle: an index built under ``index_root`` is
    ``publish``ed as an immutable version, and ``checkout`` puts a version
    where a reader can open it.
    """

    @property
    def is_local(self) -> bool:
        """True when indexes are read where they are built (no artifacts)."""
        ...

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        """The directory holding ``repo_path``'s indexes, config and metadata."""
        ...

    def publish(self, repo_path: Union[str, Path], built_dir: Path) -> Optional[str]:
        """Publish the index built in ``built_dir``; the new version, or None."""
        ...

    def checkout(
        self, repo_path: Union[str, Path], into: Path, version: Optional[str] = None
    ) -> Path:
        """Make a version readable under ``into``; the index root to read."""
        ...


class LocalIndexStore:
    """Today's layout: the index lives inside the repository it describes."""

    is_local = True

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        return Path(repo_path) / INDEX_DIRNAME

    def publish(self, repo_path: Union[str, Path], built_dir: Path) -> Optional[str]:
        """Nothing to publish: readers read the index where it was built."""
        return None

    def checkout(
        self, repo_path: Union[str, Path], into: Path, version: Optional[str] = None
    ) -> Path:
        """Nothing to fetch: the built index IS the readable one."""
        return self.index_root(repo_path)


#: What the artifact service accepts as an alias (workspace-mcp's index store).
_STORE_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_STORE_ALIAS_MAX = 128
_ALIAS_HASH_CHARS = 8


#: Store aliases of activated (per-user) copies start with this; a golden
#: repo's store alias never does (``index_store_alias``), so the two
#: namespaces cannot collide whatever a golden repo is called.
ACTIVATED_STORE_ALIAS_PREFIX = "activated."
_ACTIVATED_DIGEST_CHARS = 40
_ACTIVATED_STORE_ALIAS_RE = re.compile(
    r"^" + re.escape(ACTIVATED_STORE_ALIAS_PREFIX) + r"[0-9a-f]{40}$"
)


def index_store_alias(repo_name: str) -> str:
    """The artifact-store alias for a golden repo.

    cidx aliases allow upper case; the store's do not. Lower-casing alone
    would let ``MyRepo`` and ``myrepo`` share one version history, so any
    name the store would not accept as-is gets a short hash of the ORIGINAL
    name appended: deterministic, and distinct per cidx alias. A name in the
    activated-copy namespace (``ACTIVATED_STORE_ALIAS_PREFIX``) is hashed and
    moved out of it.
    """
    reserved = repo_name.startswith(ACTIVATED_STORE_ALIAS_PREFIX)
    if _STORE_ALIAS_RE.match(repo_name) and ".." not in repo_name and not reserved:
        return repo_name
    base = re.sub(r"[^a-z0-9._-]+", "-", repo_name.lower())
    base = re.sub(r"\.{2,}", ".", base)
    base = re.sub(r"^[^a-z0-9]+", "", base) or "repo"
    if base.startswith(ACTIVATED_STORE_ALIAS_PREFIX):
        base = "golden-" + base
    digest = hashlib.sha256(repo_name.encode("utf-8")).hexdigest()[:_ALIAS_HASH_CHARS]
    return f"{base[: _STORE_ALIAS_MAX - _ALIAS_HASH_CHARS - 1]}-{digest}"


def activated_index_store_alias(username: str, user_alias: str) -> str:
    """The artifact-store alias for ONE user's activated copy.

    Derived server-side from the registry's ``(username, user_alias)`` pair --
    never from a client value -- as a digest, so it carries no user data and
    two copies (of the same or different users) never share a version
    history. Always in the activated namespace, never a golden alias.
    """
    if not username or not user_alias:
        raise ValueError("an activated copy needs a username and a user alias")
    digest = hashlib.sha256(f"{username}\0{user_alias}".encode("utf-8")).hexdigest()[
        :_ACTIVATED_DIGEST_CHARS
    ]
    return ACTIVATED_STORE_ALIAS_PREFIX + digest


def is_activated_store_alias(store_alias: str) -> bool:
    """True for exactly the aliases ``activated_index_store_alias`` produces."""
    return bool(_ACTIVATED_STORE_ALIAS_RE.match(store_alias))


#: Alias-document field: the store alias a target's index is published under.
INDEX_STORE_ALIAS_FIELD = "index_store_alias"
#: Alias-document field: the immutable version published at the target.
INDEX_VERSION_FIELD = "index_version"


def published_index_fields(store_alias: str, version: str) -> Dict[str, str]:
    """The alias-document fields recording what a publish put at a target, so
    a pod that did not publish it can check the same version out."""
    return {INDEX_STORE_ALIAS_FIELD: store_alias, INDEX_VERSION_FIELD: version}


class DelegatedIndexStore:
    """Indexes as versioned artifacts held by another service (step 2).

    Indexes are still BUILT here, under ``index_root`` (local scratch on a pod
    without the repository). ``publish`` hands the built tree to the artifact
    service as an immutable version; ``checkout`` fetches a version, verified,
    into a directory a reader opens. ``artifacts`` is the plugin's client
    (``code_indexer.index_artifacts``): ``current_version``, ``publish``,
    ``checkout``.
    """

    is_local = False

    def __init__(self, artifacts: Any) -> None:
        self._artifacts = artifacts

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        return Path(repo_path) / INDEX_DIRNAME

    def publish(self, repo_path: Union[str, Path], built_dir: Path) -> Optional[str]:
        """Publish on top of the store's current version (its compare-and-swap
        base): a concurrent publish in between is refused, not overwritten."""
        return self.publish_as(index_store_alias(Path(repo_path).name), built_dir)

    def checkout(
        self, repo_path: Union[str, Path], into: Path, version: Optional[str] = None
    ) -> Path:
        """Check ``version`` (default: current) out to ``into/<index dir>``."""
        return self.checkout_as(index_store_alias(Path(repo_path).name), into, version)

    def publish_as(self, store_alias: str, built_dir: Path) -> str:
        """``publish`` under an explicit store alias (the caller derived it)."""
        base = self._artifacts.current_version(store_alias)
        return self.publish_on(store_alias, built_dir, base)

    def current_version_as(self, store_alias: str) -> Optional[str]:
        """The store's current version of ``store_alias`` (None: nothing yet)."""
        current = self._artifacts.current_version(store_alias)
        return str(current) if current else None

    def publish_on(
        self, store_alias: str, built_dir: Path, base_version: Optional[str]
    ) -> str:
        """Publish on top of ``base_version`` exactly (None: the first version).

        For an index built FROM a known version: if anyone published since,
        the store refuses (compare-and-swap) instead of this build silently
        replacing theirs.
        """
        return str(self._artifacts.publish(store_alias, base_version, str(built_dir)))

    def delete_as(self, store_alias: str) -> int:
        """Delete every published version of ``store_alias``; how many went.

        Requires an index-artifacts client with ``delete_alias``
        (cidx-content-backend >= 0.9.0). Idempotent; every failure raises.
        """
        delete_alias = getattr(self._artifacts, "delete_alias", None)
        if delete_alias is None:
            raise RuntimeError(
                "The index-artifacts client cannot delete an alias; "
                "cidx-content-backend >= 0.9.0 is required"
            )
        return int(delete_alias(store_alias))

    def checkout_as(
        self, store_alias: str, into: Path, version: Optional[str] = None
    ) -> Path:
        """``checkout`` under an explicit store alias: ``into/<index dir>``."""
        dest = Path(into) / INDEX_DIRNAME
        self._artifacts.checkout(store_alias, str(dest), version)
        return dest


_DEFAULT: IndexStore = LocalIndexStore()


def get_index_store() -> IndexStore:
    """The index store for path questions (``index_root``) in this process.

    Always local: where an index lives on disk is the same under either
    backend. The server builds the publishing store it hands to refresh
    (``server/startup/index_store_wiring.py``).
    """
    return _DEFAULT


def index_root(repo_path: Union[str, Path]) -> Path:
    """Shorthand for ``get_index_store().index_root(repo_path)``."""
    return get_index_store().index_root(repo_path)
