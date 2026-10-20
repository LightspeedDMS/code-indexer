"""A real catalog (cidx-meta) served as ``cidx-meta-global`` by a real app.

The neutral visibility fixture (tests/shared/cidx_meta_visibility_fixture)
is built inside an isolated ``create_app()`` app's golden-repos directory,
extended with ranked ``widget`` pattern files -- 25 under the canary's X-Ray
scope (hidden from the restricted user) that rank first, 10 under
``__any__`` (visible) that rank after them -- and indexed into a real
Tantivy index and a real FilesystemVectorStore (line-aligned chunks).

Only the embedding provider (an external service) is replaced, through
``ranked_store_search`` (the #2047 environment's deterministic provider).
"""

from __future__ import annotations

import json
import math
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, List

import pytest

from code_indexer.config import Config, ConfigManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.services.fts_file_documents import chunk_fts_documents
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)
from tests.unit.server.query.content_unavailable_env_1991 import (
    COLLECTION,
    VECTOR_DIM,
)
from tests.unit.server.query.extension_filter_env_2047 import (
    RankedEmbeddingProvider,
)

CATALOG_ALIAS = "cidx-meta-global"
QUERY = "widget"
HIDDEN_WIDGETS = 25
VISIBLE_WIDGETS = 10
CHUNK_LINES = 4
_SKIP_DIRS = {".git", ".code-indexer"}


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name,
        password_hash="$2b$12$hash",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


RESTRICTED = _user(RESTRICTED_USER, UserRole.NORMAL_USER)
ADMIN = _user(ADMIN_USER, UserRole.ADMIN)


@dataclass
class CatalogEnv:
    app: Any
    fx: CidxMetaFixture

    @property
    def root(self) -> Path:
        return self.fx.root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_widgets(root: Path) -> None:
    for n in range(1, HIDDEN_WIDGETS + 1):
        _write(
            root / "xray-patterns" / CANARY / f"w{n:02d}.yaml",
            f"name: widget widget widget {CANARY} rank{n:02d}\n",
        )
    for n in range(HIDDEN_WIDGETS + 1, HIDDEN_WIDGETS + VISIBLE_WIDGETS + 1):
        _write(
            root / "xray-patterns" / "__any__" / f"w{n:02d}.yaml",
            f"name: widget rank{n:02d}\n",
        )


def catalog_files(root: Path) -> List[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and not (set(p.relative_to(root).parts) & _SKIP_DIRS)
    )


_UNRANKED_STEP = 0.001
_RANK_TOKEN = re.compile(r"rank\d+")


def _chunk_vector(provider: RankedEmbeddingProvider, chunk: str, n: int) -> List[float]:
    """The provider's vector for a ranked chunk; an unranked chunk gets its
    own angle just below 0 (same order against every query), because
    identical vectors leave the HNSW graph degenerate and its recall
    nondeterministic."""
    if _RANK_TOKEN.search(chunk):
        return provider.get_embedding(chunk)
    angle = -_UNRANKED_STEP * (n + 1)
    return [math.cos(angle), math.sin(angle)] + [0.0] * (VECTOR_DIM - 2)


def _line_chunks(root: Path, rel: str) -> List[dict]:
    """The file's chunks: CHUNK_LINES lines each, with 1-based line ranges."""
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    return [
        {
            "text": "\n".join(lines[start : start + CHUNK_LINES]),
            "line_start": start + 1,
            "line_end": min(start + CHUNK_LINES, len(lines)),
        }
        for start in range(0, len(lines), CHUNK_LINES)
    ]


def _add_fulltext(root: Path, rel: str, chunks: List[dict], fts: Any) -> None:
    """One full-text document per chunk, through the production builder."""
    for document in chunk_fts_documents(chunks, root / rel, root):
        fts.add_document(document)


def _points(root: Path, fts: TantivyIndexManager) -> List[dict]:
    """The vector points of every file, and its full-text documents added
    to *fts* (the same chunks)."""
    provider = RankedEmbeddingProvider()
    points: List[dict] = []
    for rel in catalog_files(root):
        suffix = PurePosixPath(rel).suffix.lstrip(".") or "txt"
        chunks = _line_chunks(root, rel)
        for chunk in chunks:
            points.append(
                {
                    "id": f"chunk-{len(points)}",
                    "vector": _chunk_vector(provider, chunk["text"], len(points)),
                    "payload": {
                        "path": rel,
                        "line_start": chunk["line_start"],
                        "line_end": chunk["line_end"],
                        "content": chunk["text"],
                        "language": suffix,
                        "type": "content",
                        "git_available": True,
                    },
                }
            )
        _add_fulltext(root, rel, chunks, fts)
    return points


def rebuild_fulltext(root: Path) -> None:
    """Rebuild only the full-text index of *root* from its current files."""
    index_dir = root / ".code-indexer" / "tantivy_index"
    shutil.rmtree(index_dir)
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=True)
    try:
        for rel in catalog_files(root):
            _add_fulltext(root, rel, _line_chunks(root, rel), fts)
        fts.commit()
    finally:
        fts.close()


def _index(root: Path) -> None:
    ConfigManager(root / ".code-indexer" / "config.json").save(
        Config(codebase_dir=root)
    )
    store = FilesystemVectorStore(
        base_path=root / ".code-indexer" / "index", project_root=root
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    fts = TantivyIndexManager(root / ".code-indexer" / "tantivy_index")
    fts.initialize_index(create_new=True)
    try:
        points = _points(root, fts)
        fts.commit()
    finally:
        fts.close()
    store.begin_indexing(COLLECTION)
    try:
        store.upsert_points(COLLECTION, points)
    finally:
        store.end_indexing(COLLECTION)


def rebuild_indexes(root: Path) -> None:
    """Rebuild the vector and full-text indexes of *root* from its files."""
    for name in ("index", "tantivy_index"):
        shutil.rmtree(root / ".code-indexer" / name)
    _index(root)


@dataclass
class _Registries:
    """The app's backend registry slot: the fixture's real SQLite registry
    (every REGISTERED_ALIASES entry, cidx-meta-global included)."""

    global_repos: Any


ORDINARY_REPOS = {
    f"{ALPHA}-global": ("alpha-repo", f"payments ledger owned by {ALPHA}\n"),
    f"{CANARY}-global": ("beta-repo", f"payments ledger owned by {CANARY}\n"),
}


def _register(golden: Path, root: Path) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager

    aliases = AliasManager(str(golden / "aliases"))
    aliases.create_alias(CATALOG_ALIAS, str(root), "cidx-meta")
    for alias, (directory, text) in ORDINARY_REPOS.items():
        repo = golden / directory
        _write(repo / "README.md", text)
        _index(repo)
        aliases.create_alias(alias, str(repo), alias.removesuffix("-global"))


@contextmanager
def catalog_app(tmp: Path) -> Iterator[CatalogEnv]:
    """The isolated app serving the indexed fixture catalog."""
    from tests.unit.server._isolated_app import isolated_app

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("CO_API_KEY", raising=False)
        mp.delenv("VOYAGE_API_KEY", raising=False)
        with isolated_app(tmp / "app") as app:
            arm = app.state.semantic_query_manager.activated_repo_manager
            golden = Path(arm.activated_repos_dir).parent / "golden-repos"
            golden.mkdir(parents=True, exist_ok=True)
            fx = build_cidx_meta_fixture(golden)
            _write_widgets(fx.root)
            _index(fx.root)
            _register(golden, fx.root)
            names = ("access_filtering_service", "backend_registry", "golden_repos_dir")
            previous = {n: getattr(app.state, n, None) for n in names}
            app.state.access_filtering_service = fx.access_service
            app.state.backend_registry = _Registries(global_repos=fx.registry)
            app.state.golden_repos_dir = previous["golden_repos_dir"] or str(golden)
            try:
                yield CatalogEnv(app=app, fx=fx)
            finally:
                for name, value in previous.items():
                    setattr(app.state, name, value)


def dump(value: Any) -> str:
    """The whole serialized response, lower-cased, for canary checks."""
    return json.dumps(value, default=str).lower()
