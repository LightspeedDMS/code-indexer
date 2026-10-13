"""X-Ray's Python reads go through the content seam on a mount-less pod.

Phase 1 of docs/content-seam-xray-sandbox.md: candidate selection, the
ast_debug re-parse, analyze_graph's candidate walk and xray_dump_ast stop
reading the repository off this pod's disk when the server's content backend is
delegated. (xray-cli's own compile and scan stay local in this phase; those are
mocked here, and line_content is covered by test_xray_line_content_emitted.)

Every test builds ONE real repository and runs the same call twice:

* locally, on the repository path, with no server backends (today's path);
* delegated: a NON-local content backend serving that tree from memory, and a
  NON-local ripgrep backend, with the call made on the pod-local snapshot path,
  which holds only ``.code-indexer``. Any Python read under the golden-repos
  directory raises, so delegation cannot quietly use the disk.

The two must agree, except for the documented symlink rule: the manifest
reports a link and never follows it.
"""

from __future__ import annotations

import builtins
import dataclasses
import gc
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

import pytest

from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.startup import ripgrep_backend_wiring as rg_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.server.storage.shared.ripgrep_backend import (
    LocalRipgrepBackend,
    RipgrepRun,
)
from code_indexer.xray import search_engine as se_mod
from code_indexer.xray.search_engine import XRaySearchEngine

REPO = "example-repo"
SNAPSHOT_LEAF = "v_1760000000"
LINK = "link.py"
EXTRACTORS = {"py": "python", "java": "java"}

needs_rg = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep binary not installed"
)


class MemoryContentBackend:
    """A NON-local content backend over a snapshot of the tree taken up front.

    Serving from memory is what lets the tests forbid every Python read of the
    repository: the backend itself never touches the disk after construction.
    """

    def __init__(self, root: Path, page_size: int = 3) -> None:
        local = LocalContentBackend()
        self.root = str(root)
        self._page_size = page_size
        self._entries: List[ContentEntry] = []
        cursor: Optional[str] = None
        while True:
            page = local.manifest(root, cursor=cursor)
            self._entries.extend(page.entries)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        files = [e.path for e in self._entries if not e.is_symlink]
        self._bytes = {f.path: f.data for f in local.fetch(root, files)}
        self.manifest_calls: List[Dict[str, Any]] = []
        self.fetch_calls: List[List[str]] = []
        self.fail_fetch: Optional[Exception] = None
        #: Seconds each manifest page takes, for the Phase 1 deadline tests.
        self.page_delay = 0.0
        #: ("fetch", paths) per fetch, in order, shared with a parse recorder.
        self.events: List[Any] = []
        #: Empty pages served before the first real one: a backend may page
        #: through pruned stretches, and the Phase 1 loop never sees an entry.
        self.empty_pages_first = 0
        #: Overrides the size the manifest reports, by path.
        self.reported_size: Dict[str, int] = {}

    @property
    def is_local(self) -> bool:
        return False

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = 1000,
        prune_dirs: Optional[Sequence[str]] = None,
    ) -> ContentPage:
        assert str(repo_path) == self.root
        prune = set(prune_dirs or ())
        self.manifest_calls.append({"cursor": cursor, "prune": sorted(prune)})
        if self.page_delay:
            time.sleep(self.page_delay)
        if cursor is None or cursor.startswith("\x00"):
            served = 0 if cursor is None else int(cursor[1:])
            if served < self.empty_pages_first:
                return ContentPage(entries=(), next_cursor=f"\x00{served + 1}")
            cursor = None
        kept = [
            e
            for e in self._entries
            if not prune & set(PurePosixPath(e.path).parts[:-1])
            and (cursor is None or e.path > cursor)
        ]
        page = [
            dataclasses.replace(e, size=self.reported_size.get(e.path, e.size))
            for e in kept[: self._page_size]
        ]
        more = len(kept) > self._page_size
        return ContentPage(
            entries=tuple(page), next_cursor=page[-1].path if more else None
        )

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        assert str(repo_path) == self.root
        self.fetch_calls.append(list(paths))
        self.events.append(("fetch", list(paths)))
        if self.fail_fetch is not None:
            raise self.fail_fetch
        for rel in paths:
            if rel == LINK:
                yield FetchedFile(rel, None, b"", error="symlink not followed")
            elif rel in self._bytes:
                yield FetchedFile(rel, "sha", self._bytes[rel])
            else:
                yield FetchedFile(rel, None, b"", error="No such file or directory")


class DelegatedRipgrep:
    """A NON-local ripgrep backend: runs the real binary, records the root."""

    def __init__(self) -> None:
        self._inner = LocalRipgrepBackend()
        self.working_dirs: List[str] = []

    @property
    def is_local(self) -> bool:
        return False

    async def run(self, **kwargs: Any) -> RipgrepRun:
        self.working_dirs.append(kwargs["working_dir"])
        return await self._inner.run(**kwargs)

    async def supports_pcre2(self) -> bool:
        return await self._inner.supports_pcre2()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _write(path: Path, data: Union[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


@pytest.fixture(autouse=True)
def _empty_manifest_cache():
    fs_mod._collect_cache.clear()
    yield
    fs_mod._collect_cache.clear()


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """golden-repos/ holding one real repository, its snapshot, and a registry."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    repo = base / REPO
    _write(repo / "src" / "app.py", "def handler():\n    return 1\n\nX = 2\n")
    _write(repo / "src" / "pkg" / "deep.py", "def deep():\n    handler()\n")
    _write(repo / "src" / "pkg" / "Deep.java", "class Deep { void handler() {} }\n")
    _write(repo / "src" / "notes.txt", "handler mentioned in prose\n")
    _write(repo / "crlf.py", b"def handler():\r\n    pass\r\n")
    _write(repo / "docs" / "guide.md", "guide\n")
    _write(repo / "build" / "gen.py", "def handler(): pass\n")
    _write(repo / "node_modules" / "dep" / "index.js", "module.exports = 1\n")
    _write(repo / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(repo / ".code-indexer" / "index" / "handler.py", "def handler(): 0\n")
    _write(repo / "big.py", "# " + "x" * (300 * 1024) + "\n")
    os.symlink("src/app.py", repo / LINK)

    snapshot = base / ".versioned" / REPO / SNAPSHOT_LEAF
    _write(snapshot / ".code-indexer" / "metadata.json", "{}")

    manager = SimpleNamespace(
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=str(repo)) if name == REPO else None
        )
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _repo(golden: Path) -> Path:
    return golden / REPO


def _snapshot(golden: Path) -> Path:
    return golden / ".versioned" / REPO / SNAPSHOT_LEAF


def _delegate(
    monkeypatch: pytest.MonkeyPatch, golden: Path
) -> "tuple[MemoryContentBackend, DelegatedRipgrep]":
    """Delegate content and ripgrep, then forbid every local repository read."""
    content = MemoryContentBackend(_repo(golden))
    ripgrep = DelegatedRipgrep()
    monkeypatch.setattr(wiring, "server_content_backend", lambda: content)
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)
    _forbid_local_reads(monkeypatch, golden)
    return content, ripgrep


def _forbid_local_reads(monkeypatch: pytest.MonkeyPatch, golden: Path) -> None:
    """Every Python listing or read under golden-repos/ raises.

    Path metadata (``resolve``) is not guarded: the delegated regex service
    already canonicalises its root, and that reads no repository content."""
    fence = str(golden)

    def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(target: Any, *args: Any, **kwargs: Any) -> Any:
            if str(target).startswith(fence):
                raise AssertionError(
                    f"local repository read under delegation: {target}"
                )
            return original(target, *args, **kwargs)

        return guarded

    for name in ("read_bytes", "read_text", "rglob", "glob", "iterdir", "is_file"):
        monkeypatch.setattr(Path, name, _guard(getattr(Path, name)))
    monkeypatch.setattr(builtins, "open", _guard(builtins.open))
    for name in ("walk", "scandir", "listdir"):
        monkeypatch.setattr(os, name, _guard(getattr(os, name)))


# --------------------------------------------------------------------------
# Phase 1: filename driver
# --------------------------------------------------------------------------

FILENAME_CASES = [
    {"regex": r".*", "include": [], "exclude": []},
    {"regex": r"\.py$", "include": [], "exclude": ["build/**"]},
    {"regex": r"deep", "include": ["src/**"], "exclude": []},
    {"regex": r".*", "include": ["**/*.java", "nomatch/*.py"], "exclude": []},
]


def _filename_phase1(root: Path, case: Dict[str, Any]) -> "tuple[list, list]":
    engine = XRaySearchEngine()
    candidates = engine._run_phase1_driver(
        root, case["regex"], "filename", case["include"], case["exclude"]
    )
    rels = [p.relative_to(root).as_posix() for p in candidates]
    return rels, engine._last_phase1_warnings


@pytest.mark.parametrize("case", FILENAME_CASES)
def test_filename_candidates_match_local(
    golden: Path, monkeypatch: pytest.MonkeyPatch, case: Dict[str, Any]
) -> None:
    local, local_warnings = _filename_phase1(_repo(golden), case)

    content, _ = _delegate(monkeypatch, golden)
    delegated, delegated_warnings = _filename_phase1(_snapshot(golden), case)

    # The documented rule: a link is never followed, so it is never a candidate.
    assert [r for r in local if r != LINK] == delegated
    assert delegated_warnings == local_warnings
    assert len(content.manifest_calls) > 1  # paged, and every page pruned
    assert all(c["prune"] == [".code-indexer", ".git"] for c in content.manifest_calls)


def test_filename_candidates_join_the_snapshot_path(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidates are the alias target's paths -- what xray-cli opens, still
    locally, in this phase -- never the backend's root."""
    _delegate(monkeypatch, golden)
    candidates = XRaySearchEngine()._run_phase1_driver(
        _snapshot(golden), r"app\.py$", "filename", [], []
    )
    assert candidates == [_snapshot(golden) / "src" / "app.py"]


def test_filename_manifest_failure_fails_loud(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _ = _delegate(monkeypatch, golden)

    def _down(*_a: Any, **_k: Any) -> ContentPage:
        raise ConnectionError("workspace-mcp unreachable")

    monkeypatch.setattr(content, "manifest", _down)
    with pytest.raises(ConnectionError):
        XRaySearchEngine()._run_phase1_driver(
            _snapshot(golden), r".*", "filename", [], []
        )


def test_filename_manifest_walk_honours_the_phase1_deadline(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow paged manifest stops at the deadline with the partial
    candidates, exactly the local walk's timeout -- it does not fetch every
    page first and only then notice -- and the partial walk is not cached."""
    content, _ = _delegate(monkeypatch, golden)
    content._page_size = 2
    complete, _ = _filename_phase1(_snapshot(golden), FILENAME_CASES[0])
    pages = len(content.manifest_calls)
    assert pages > 4
    fs_mod._collect_cache.clear()
    content.manifest_calls.clear()

    content.page_delay = 0.4
    with pytest.raises(se_mod.XRayPhase1TimeoutError) as raised:
        XRaySearchEngine()._run_phase1_filename(
            _snapshot(golden), r".*", [], [], timeout_seconds=1
        )
    assert 1 < len(content.manifest_calls) < pages
    partial = [
        p.relative_to(_snapshot(golden)).as_posix() for p in raised.value.candidates
    ]
    assert partial and set(partial) < set(complete)
    assert partial == sorted(partial)
    # Release the walk the exception's traceback still holds, so a partial
    # walk that WOULD be cached on close is cached before the next call.
    del raised
    gc.collect()

    content.page_delay = 0.0
    content.manifest_calls.clear()
    assert _filename_phase1(_snapshot(golden), FILENAME_CASES[0])[0] == complete
    assert len(content.manifest_calls) == pages  # walked again: nothing cached


def test_filename_deadline_holds_while_the_manifest_yields_no_entries(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages with no entries never reach the per-entry check, so only the
    deadline passed down to the walk can stop it -- as the same timeout."""
    content, _ = _delegate(monkeypatch, golden)
    content.empty_pages_first = 20
    content.page_delay = 0.3
    with pytest.raises(se_mod.XRayPhase1TimeoutError) as raised:
        XRaySearchEngine()._run_phase1_filename(
            _snapshot(golden), r".*", [], [], timeout_seconds=1
        )
    assert raised.value.candidates == []
    assert len(content.manifest_calls) < 20


def test_filename_deadline_yields_the_run_timeout_shape(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _ = _delegate(monkeypatch, golden)
    content.page_delay = 0.4
    engine = XRaySearchEngine()
    result = engine.run(
        repo_path=_snapshot(golden),
        driver_regex=r".*",
        evaluator_code="unused: Phase 1 times out",
        search_target="filename",
        timeout_seconds=1,
    )
    assert result["partial"] is True and result["timeout"] is True
    assert result["matches"] == []


def test_iter_manifest_entries_checks_the_deadline_before_each_page(
    golden: Path,
) -> None:
    content = MemoryContentBackend(_repo(golden))
    source = fs_mod.ContentSeamSource(backend=content, root=content.root)
    with pytest.raises(fs_mod.ManifestDeadlineExceeded):
        next(
            fs_mod.iter_manifest_entries(
                source, [".git"], deadline=time.monotonic() - 1
            )
        )
    assert content.manifest_calls == []

    streamed = list(fs_mod.iter_manifest_entries(source, [".git"]))
    calls = len(content.manifest_calls)
    assert tuple(streamed) == fs_mod.manifest_entries(source, [".git"])
    assert list(fs_mod.iter_manifest_entries(source, [".git"])) == streamed
    assert len(content.manifest_calls) == calls  # completed walk replayed


# --------------------------------------------------------------------------
# Phase 1: content driver
# --------------------------------------------------------------------------


def _content_phase1(root: Path, include: List[str]) -> "tuple[dict, list]":
    engine = XRaySearchEngine()
    candidates = engine._run_phase1_driver(
        root, r"handler", "content", include, [], context_lines=1
    )
    positions = {
        p.relative_to(root).as_posix(): engine._last_phase1_positions[p]
        for p in candidates
    }
    return positions, engine._last_phase1_warnings


@needs_rg
@pytest.mark.parametrize("include", [[], ["**/*.py", "nomatch/**"]])
def test_content_candidates_and_positions_match_local(
    golden: Path, monkeypatch: pytest.MonkeyPatch, include: List[str]
) -> None:
    local, local_warnings = _content_phase1(_repo(golden), include)

    _, ripgrep = _delegate(monkeypatch, golden)
    delegated, delegated_warnings = _content_phase1(_snapshot(golden), include)

    assert delegated == local
    assert "src/app.py" in delegated
    assert delegated_warnings == local_warnings
    # ripgrep ran where the repository is: the base clone, never the snapshot.
    assert ripgrep.working_dirs
    assert set(ripgrep.working_dirs) == {str(_repo(golden).resolve())}


def test_content_local_selection_builds_the_service_as_before(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local ripgrep backend on the server changes nothing: same root, the
    service's own default backend."""
    monkeypatch.setattr(
        rg_wiring, "server_ripgrep_backend", lambda: LocalRipgrepBackend()
    )
    service = se_mod._phase1_regex_service(_snapshot(golden))
    assert service.repo_path == _snapshot(golden).resolve()
    assert isinstance(service._ripgrep_backend, LocalRipgrepBackend)


def test_content_delegated_selection_targets_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ripgrep = DelegatedRipgrep()
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)
    service = se_mod._phase1_regex_service(_snapshot(golden))
    assert service.repo_path == _repo(golden).resolve()
    assert service._ripgrep_backend is ripgrep


# --------------------------------------------------------------------------
# ast_debug re-parse
# --------------------------------------------------------------------------


def _fake_run_batch(**kwargs: Any) -> List[Any]:
    """xray-cli stands in: one finding on line 1 of every candidate."""
    return [
        (
            [
                {
                    "line_number": 1,
                    "file_path": spec["file_path"],
                    "language": spec["lang"],
                    "pattern": "p",
                    "snippet": "",
                    "line_content": "",
                }
            ],
            [],
            None,
        )
        for spec in kwargs["file_specs"]
    ]


def _run_with_ast_debug(
    root: Path, events: Optional[List[Any]] = None
) -> Dict[str, Any]:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    engine = XRaySearchEngine()
    setattr(engine.rust_backend, "run_batch", _fake_run_batch)
    if events is not None:
        parse = engine.ast_engine.parse

        def _recording_parse(source: Any, lang: str) -> Any:
            events.append(("parse", lang))
            return parse(source, lang)

        setattr(engine.ast_engine, "parse", _recording_parse)
    result = engine.run(
        repo_path=root,
        driver_regex=r"\.(py|java)$",
        evaluator_code="unused: run_batch is faked",
        search_target="filename",
        # The link is the documented symlink divergence, covered elsewhere.
        exclude_patterns=["build/**", LINK],
        include_ast_debug=True,
    )
    result.pop("elapsed_seconds")
    return result


def test_ast_debug_matches_local_with_one_batched_fetch(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _run_with_ast_debug(_repo(golden))

    content, _ = _delegate(monkeypatch, golden)
    delegated = _run_with_ast_debug(_snapshot(golden))

    assert json.dumps(delegated, sort_keys=True) == json.dumps(local, sort_keys=True)
    assert all("ast_debug" in m for m in delegated["matches"])
    # ONE fetch for every matched file, never one call per file.
    assert content.fetch_calls == [
        sorted({m["file_path"] for m in delegated["matches"]})
    ]


def test_ast_debug_per_file_fetch_error_is_that_files_failure(
    golden: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-file error record behaves exactly as a local read failure does:
    logged, that match keeps no ast_debug, the rest are enriched."""
    content, _ = _delegate(monkeypatch, golden)
    del content._bytes["src/app.py"]
    with caplog.at_level(logging.WARNING):
        result = _run_with_ast_debug(_snapshot(golden))
    by_path = {m["file_path"]: m for m in result["matches"]}
    assert "ast_debug" not in by_path["src/app.py"]
    assert "ast_debug" in by_path["crlf.py"]
    assert "ast_debug: failed to parse src/app.py" in caplog.text


def test_ast_debug_backend_failure_fails_loud(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _ = _delegate(monkeypatch, golden)
    content.fail_fetch = ConnectionError("workspace-mcp unreachable")
    with pytest.raises(ConnectionError):
        _run_with_ast_debug(_snapshot(golden))


def test_ast_debug_fetches_in_bounded_batches_and_parses_as_they_arrive(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes are fetched a batch at a time -- never per file, never all at
    once -- and each file is parsed before the next batch is requested, so at
    most one batch of sources is ever in memory."""
    local = _run_with_ast_debug(_repo(golden))

    monkeypatch.setattr(se_mod, "_AST_DEBUG_FETCH_BATCH_FILES", 2)
    content, _ = _delegate(monkeypatch, golden)
    delegated = _run_with_ast_debug(_snapshot(golden), events=content.events)

    assert json.dumps(delegated, sort_keys=True) == json.dumps(local, sort_keys=True)
    matched = sorted({m["file_path"] for m in delegated["matches"]})
    assert len(matched) > 4
    assert [len(batch) for batch in content.fetch_calls] == [2] * (
        len(matched) // 2
    ) + ([1] if len(matched) % 2 else [])
    assert sorted(p for batch in content.fetch_calls for p in batch) == matched
    kinds = [kind for kind, _ in content.events]
    # fetch, parse, parse, fetch, parse, parse, ...: never two fetches ahead.
    assert kinds[:4] == ["fetch", "parse", "parse", "fetch"]


# --------------------------------------------------------------------------
# analyze_graph candidates
# --------------------------------------------------------------------------


def _graph_candidates(root: Path, include: List[str], max_files: int = 1000) -> Any:
    from code_indexer.server.mcp.handlers.xray_graph._candidates import (
        _collect_graph_candidate_files,
    )

    return _collect_graph_candidate_files(
        root, include, [], max_files=max_files, extractor_extensions=EXTRACTORS
    )


@pytest.mark.parametrize("include", [[], ["src/**"], ["**/*.java"]])
def test_graph_candidates_match_local(
    golden: Path, monkeypatch: pytest.MonkeyPatch, include: List[str]
) -> None:
    local = _graph_candidates(_repo(golden), include)

    content, _ = _delegate(monkeypatch, golden)
    delegated = _graph_candidates(_snapshot(golden), include)

    paths, truncated, with_ex, without_ex, languages = local
    if LINK in paths:
        # Never followed, so never read: counted as an excluded file whose
        # language has an extractor -- fact_graph_complete is downgraded,
        # never silently thinned.
        paths = [p for p in paths if p != LINK]
        with_ex += 1
        languages = sorted(set(languages) | {"python"})
    assert delegated == (paths, truncated, with_ex, without_ex, languages)
    assert "build/gen.py" not in delegated[0]
    assert all(
        c["prune"]
        == sorted(
            {".git", ".code-indexer", "node_modules", ".venv", "__pycache__"}
            | {"target", "dist", "build"}
        )
        for c in content.manifest_calls
    )


def test_graph_candidates_cap_is_honest_under_delegation(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _delegate(monkeypatch, golden)
    paths, truncated, *_ = _graph_candidates(_snapshot(golden), [], max_files=2)
    assert truncated is True
    assert len(paths) == 2


# --------------------------------------------------------------------------
# xray_dump_ast
# --------------------------------------------------------------------------


def _dump(
    monkeypatch: pytest.MonkeyPatch, root: Path, file_path: str
) -> Dict[str, Any]:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp.handlers.xray import _dump_ast, handle_xray_dump_ast

    monkeypatch.setattr(_dump_ast, "_resolve_repo_path", lambda _alias: str(root))
    user = User(
        username="example-user",
        password_hash="unused",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    result = handle_xray_dump_ast(
        {"repository_alias": f"{REPO}-global", "file_path": file_path}, user
    )
    return dict(json.loads(result["content"][0]["text"]))


DUMP_CASES = [
    "src/app.py",
    "src/pkg/Deep.java",
    "crlf.py",
    "src/../src/app.py",
    "missing.py",
    "docs/guide.md",
    "big.py",
    "../outside.py",
    "src/../../outside.py",
    "/etc/passwd",
]


@pytest.mark.parametrize("file_path", DUMP_CASES)
def test_dump_ast_matches_local(
    golden: Path, monkeypatch: pytest.MonkeyPatch, file_path: str
) -> None:
    local = _dump(monkeypatch, _repo(golden), file_path)

    content, _ = _delegate(monkeypatch, golden)
    delegated = _dump(monkeypatch, _snapshot(golden), file_path)

    if local.get("error") == "file_not_found":
        # The delegated message also names the backend's reason.
        assert delegated["error"] == "file_not_found"
        assert delegated["message"].startswith(local["message"])
    else:
        assert delegated == local
    assert len(content.fetch_calls) <= 1


def test_dump_ast_symlink_is_refused_not_followed(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented divergence: locally a link inside the repository is
    followed; the backend refuses it, and that refusal is reported."""
    assert "ast_tree" in _dump(monkeypatch, _repo(golden), LINK)
    _delegate(monkeypatch, golden)
    delegated = _dump(monkeypatch, _snapshot(golden), LINK)
    assert delegated["error"] == "file_not_found"
    assert "symlink not followed" in delegated["message"]


def test_dump_ast_traversal_never_reaches_the_backend(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _ = _delegate(monkeypatch, golden)
    for file_path in ("../outside.py", "/etc/passwd", "a/../../x.py"):
        response = _dump(monkeypatch, _snapshot(golden), file_path)
        assert response["error"] == "path_traversal_rejected"
    assert content.fetch_calls == []


def test_dump_ast_oversized_is_refused_before_any_fetch(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _dump(monkeypatch, _repo(golden), "big.py")
    content, _ = _delegate(monkeypatch, golden)
    delegated = _dump(monkeypatch, _snapshot(golden), "big.py")
    assert local["error"] == "file_too_large"
    assert delegated == local
    assert content.fetch_calls == []  # refused on the manifest's size


def test_dump_ast_size_is_rechecked_on_the_fetched_bytes(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: a manifest that understates the size (a file that
    grew since it was taken) is still refused before any parse."""
    local = _dump(monkeypatch, _repo(golden), "big.py")
    content, _ = _delegate(monkeypatch, golden)
    content.reported_size["big.py"] = 10
    delegated = _dump(monkeypatch, _snapshot(golden), "big.py")
    assert delegated == local
    assert content.fetch_calls == [["big.py"]]


def test_dump_ast_backend_failure_fails_loud(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _ = _delegate(monkeypatch, golden)
    content.fail_fetch = ConnectionError("workspace-mcp unreachable")
    with pytest.raises(ConnectionError):
        _dump(monkeypatch, _snapshot(golden), "src/app.py")


# --------------------------------------------------------------------------
# the default is unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", [None, LocalContentBackend()])
def test_no_seam_unless_the_backend_is_delegated(
    golden: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)
    assert se_mod.xray_content_source(_repo(golden)) is None


def test_the_seam_reads_the_base_clone_behind_a_snapshot(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = MemoryContentBackend(_repo(golden))
    monkeypatch.setattr(wiring, "server_content_backend", lambda: content)
    source = se_mod.xray_content_source(_snapshot(golden))
    assert source is not None
    assert source.backend is content
    assert source.root == str(_repo(golden))
