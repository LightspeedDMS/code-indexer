"""Bug #2056: `cidx index --fts` leaves the FTS index empty or partial.

Drives the REAL `cidx` CLI (subprocess, the tree under test) in a temporary
git repository against the local fake VoyageAI server
(scripts/analysis/reembed_repro/fake_voyage_server.py, plain HTTP on
127.0.0.1, sentinel key): no real provider call is possible. HOME and the
server data dir are scratch directories.

Root cause (fixed in FileChunkingManager): the per-file FTS supersession
deleted every FTS document of a processed file but re-added only the chunks
embedded in THIS run. A file whose chunks all came from the embedding cache
(unchanged content) therefore lost all of its FTS documents. The FTS
bootstrap of `cidx index --fts`, a repeated `cidx index --fts`, and the
server's golden-repo refresh (`cidx index --fts`, which re-selects recently
modified files through its 60 s safety buffer) all hit that path.
"""

import contextlib
import importlib.util
import json
import os
import pwd
import site
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

import code_indexer
from code_indexer.services.fts_file_documents import mark_fts_content_current
from code_indexer.services.fts_lifecycle import FtsRebuildResult
from code_indexer.services.tantivy_index_manager import TantivyIndexManager

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FAKE_SERVER_PATH = (
    _PROJECT_ROOT / "scripts" / "analysis" / "reembed_repro" / "fake_voyage_server.py"
)
# The tree whose code the CLI subprocess runs: the same one this test imports.
_SRC_DIR = Path(code_indexer.__file__).resolve().parents[1]
_SENTINEL_KEY = "issue-2056-fake-key"
# Same server-context layout args the golden-repo refresh appends
# (server/utils/index_command_layout.py). --server-managed-provider-settings
# is left out on purpose: it pins the real provider endpoint URL.
_SERVER_LAYOUT_ARGS = ["--new-collection-layout=chunks_db", "--ignore-resume-state"]
_CLI_TIMEOUT_SECONDS = 180

# Each test runs 3-5 real `cidx` subprocesses (8-13 s measured), too close
# to fast-automation's 15 s pytest-timeout ceiling to survive gate load.
_PYTEST_TIMEOUT_SECONDS = 120
pytestmark = pytest.mark.timeout(_PYTEST_TIMEOUT_SECONDS)


def _load_fake_server_module():
    spec = importlib.util.spec_from_file_location(
        "fake_voyage_server", _FAKE_SERVER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_voyage_server"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_provider() -> Iterator[object]:
    server = _load_fake_server_module().FakeVoyageServer(_SENTINEL_KEY)
    server.start("127.0.0.1", 0)
    try:
        yield server
    finally:
        server.stop()
    assert server.ledger.violations() == []


def _real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


class _CliRepo:
    """A temporary git repository driven through the real `cidx` CLI."""

    def __init__(self, root: Path, server) -> None:
        self.server = server
        self.path = root / "repo"
        self.path.mkdir()
        home = root / "home"
        home.mkdir()
        self.env: Dict[str, str] = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "HF_HOME": os.environ.get(
                "HF_HOME", str(_real_home() / ".cache" / "huggingface")
            ),
            "HF_HUB_OFFLINE": "1",
            "PYTHONPATH": str(_SRC_DIR),
            "PYTHONUSERBASE": site.getuserbase(),
            "VOYAGE_API_KEY": _SENTINEL_KEY,
            "CIDX_SERVER_DATA_DIR": str(root / "server-data"),
        }
        self._runs = 0
        self.last_output = ""
        # Every repo directory is named "repo"; only the root it was created
        # in tells two repos sharing one fake provider apart.
        self.run_label_prefix = root.name

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, message: str) -> None:
        for args in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=Example",
                "-c",
                "user.email=dev@example.com",
                "commit",
                "-qm",
                message,
            ],
        ):
            subprocess.run(args, cwd=self.path, check=True, capture_output=True)

    def init(self) -> None:
        self.cidx("init")
        config_path = self.path / ".code-indexer" / "config.json"
        config = json.loads(config_path.read_text())
        config.setdefault("voyage_ai", {})["api_endpoint"] = (
            f"{self.server.base_url}/v1/embeddings"
        )
        config_path.write_text(json.dumps(config))

    def cidx(self, *args: str) -> int:
        """Run `cidx <args>`; return the number of inputs sent for embedding."""
        self._runs += 1
        label = f"{self.run_label_prefix}-run-{self._runs}"
        self.server.ledger.begin_run(label)
        proc = subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        self.last_output = proc.stdout
        assert proc.returncode == 0, (
            f"cidx {' '.join(args)} exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        )
        return int(self.server.ledger.run_stats(label)["inputs"])

    def cidx_expect_failure(self, *args: str) -> int:
        """Run `cidx <args>` expecting failure; return its non-zero exit code."""
        self._runs += 1
        self.server.ledger.begin_run(f"{self.run_label_prefix}-run-{self._runs}")
        proc = subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        self.last_output = proc.stdout
        assert proc.returncode != 0, f"cidx {' '.join(args)} unexpectedly succeeded"
        return proc.returncode

    @property
    def fts_dir(self) -> Path:
        return self.path / ".code-indexer" / "tantivy_index"

    def fts_docs_per_path(self) -> Counter:
        import tantivy

        index = tantivy.Index.open(str(self.fts_dir))
        index.reload()
        searcher = index.searcher()
        counts: Counter = Counter()
        if searcher.num_docs == 0:
            return counts
        hits = searcher.search(tantivy.Query.all_query(), searcher.num_docs).hits
        for _score, address in hits:
            counts[searcher.doc(address).get_first("path")] += 1
        return counts

    def fts_documents(self) -> Counter:
        """Every stored FTS document, as a comparable tuple of all its
        stored fields (multiset: duplicates count)."""
        import tantivy

        index = tantivy.Index.open(str(self.fts_dir))
        index.reload()
        searcher = index.searcher()
        documents: Counter = Counter()
        if searcher.num_docs == 0:
            return documents
        hits = searcher.search(tantivy.Query.all_query(), searcher.num_docs).hits
        for _score, address in hits:
            doc = searcher.doc(address)
            documents[
                (
                    doc.get_first("path"),
                    doc.get_first("line_start"),
                    doc.get_first("line_end"),
                    doc.get_first("language"),
                    doc.get_first("content_raw"),
                    tuple(doc.get_all("identifiers")),
                )
            ] += 1
        return documents

    def fts_paths_for(
        self, token: str, path_filters: Optional[List[str]] = None
    ) -> List[str]:
        fts = TantivyIndexManager(self.fts_dir)
        fts.initialize_index(create_new=False)
        try:
            hits = fts.search(token, limit=50, path_filters=path_filters)
            return [hit["path"] for hit in hits]
        finally:
            fts.close()


SMALL_FILES = {
    "src/auth.py": "def check_password(pw):\n    return pw == 'AUTHTOKEN'\n",
    "src/cart.js": "function addToCart(item) { return 'CARTTOKEN'; }\n",
    "tests/test_auth.py": "def test_auth():\n    assert 'TESTTOKEN'\n",
}


def _multi_chunk_file(tail_token: str) -> str:
    """A file several chunks long (voyage-code-3 chunks are 4096 chars):
    HEADTOKEN only in the first chunk, `tail_token` only in the last one.
    Both tail tokens have the same length, so editing the tail never moves
    a chunk boundary and the first chunks stay byte-identical."""
    filler = "".join(f"value_{i:05d} = {i}\n" for i in range(700))
    # TWICETOKEN: one match in the first chunk, one in the last.
    return f"HEADTOKEN = 1  # TWICETOKEN\n{filler}{tail_token} = 2  # TWICETOKEN\n"


def _make_repo(tmp_path: Path, server) -> _CliRepo:
    repo = _CliRepo(tmp_path, server)
    for relative, content in SMALL_FILES.items():
        repo.write(relative, content)
    repo.write("src/big.py", _multi_chunk_file("OLDTAILTOKEN"))
    repo.commit("initial")
    repo.init()
    return repo


ALL_PATHS = {*SMALL_FILES, "src/big.py"}


def _assert_every_file_searchable(repo: _CliRepo) -> None:
    per_path = repo.fts_docs_per_path()
    assert set(per_path) == ALL_PATHS, f"FTS documents per path: {dict(per_path)}"
    for token, path in (
        ("AUTHTOKEN", "src/auth.py"),
        ("CARTTOKEN", "src/cart.js"),
        ("TESTTOKEN", "tests/test_auth.py"),
        ("HEADTOKEN", "src/big.py"),
    ):
        assert repo.fts_paths_for(token) == [path], token


#: The run's output when some files are missing from the FTS index.
_MISSING_FROM_FTS = "1 file(s) missing from the FTS index"


def _assert_partial_fts(repo: _CliRepo, *, missing_path: str) -> None:
    """A run that succeeded with one file missing from FTS: it said so, left
    the index unmarked (the next run retries) and indexed every other file."""
    assert _MISSING_FROM_FTS in repo.last_output, repo.last_output[-3000:]
    assert not (repo.fts_dir / "cidx_fts_content_version").exists()
    assert set(repo.fts_docs_per_path()) == ALL_PATHS - {missing_path}
    for token, path in (
        ("AUTHTOKEN", "src/auth.py"),
        ("CARTTOKEN", "src/cart.js"),
        ("TESTTOKEN", "tests/test_auth.py"),
        ("HEADTOKEN", "src/big.py"),
    ):
        expected = [] if path == missing_path else [path]
        assert repo.fts_paths_for(token) == expected, token


@contextlib.contextmanager
def _all_files_unreadable(repo: _CliRepo) -> Iterator[None]:
    paths = [repo.path / relative for relative in ALL_PATHS]
    for path in paths:
        os.chmod(path, 0)
    try:
        yield
    finally:
        for path in paths:
            os.chmod(path, 0o644)


class TestIndexFtsKeepsEveryFile2056:
    def test_index_fts_on_existing_semantic_index_fills_fts(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """The issue's scenario: semantic index first, then `--fts`."""
        repo = _make_repo(tmp_path, fake_provider)
        assert repo.cidx("index") > 0

        assert repo.cidx("index", "--fts") == 0, "no content changed"

        _assert_every_file_searchable(repo)
        committed = sum(repo.fts_docs_per_path().values())
        assert f"FTS index holds {committed} committed documents" in repo.last_output
        assert "FTS index built from" not in repo.last_output

    def test_repeated_index_fts_keeps_fts_complete_without_duplicates(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        _assert_every_file_searchable(repo)
        first_counts = repo.fts_docs_per_path()

        for _ in range(2):
            assert repo.cidx("index", "--fts") == 0, "no content changed"
            _assert_every_file_searchable(repo)
            assert repo.fts_docs_per_path() == first_counts

    def test_refresh_within_safety_buffer_keeps_fts_complete(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """Server-shaped refreshes: edit one chunk of a multi-chunk file,
        refresh, then refresh again with no change -- all inside the 60 s
        safety buffer, so recently modified files are re-selected."""
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        _assert_every_file_searchable(repo)
        initial_counts = repo.fts_docs_per_path()
        assert initial_counts["src/big.py"] > 1, "big.py must span several chunks"

        repo.write("src/big.py", _multi_chunk_file("NEWTAILTOKEN"))
        repo.commit("edit the last chunk")
        assert repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS) >= 1

        assert repo.fts_paths_for("NEWTAILTOKEN") == ["src/big.py"]
        assert repo.fts_paths_for("OLDTAILTOKEN") == []
        _assert_every_file_searchable(repo)  # incl. big.py's unchanged HEADTOKEN
        assert repo.fts_docs_per_path() == initial_counts

        assert repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS) == 0
        assert repo.fts_paths_for("NEWTAILTOKEN") == ["src/big.py"]
        _assert_every_file_searchable(repo)
        assert repo.fts_docs_per_path() == initial_counts


def _seed_pre_fix_rebuild_index(repo: _CliRepo) -> None:
    """Recreate the FTS index exactly as the pre-fix `--rebuild-fts-index`
    left it: one whole-file document per file, under its ABSOLUTE path."""
    import shutil

    shutil.rmtree(repo.fts_dir)
    fts = TantivyIndexManager(repo.fts_dir)
    fts.initialize_index(create_new=True)
    try:
        for relative in sorted(ALL_PATHS):
            absolute = repo.path / relative
            text = absolute.read_text()
            fts.add_document(
                {
                    "path": str(absolute),
                    "content": text,
                    "content_raw": text,
                    "identifiers": [],
                    "line_start": 1,
                    "line_end": len(text.splitlines()),
                    "language": absolute.suffix.lstrip("."),
                }
            )
        fts.commit()
    finally:
        fts.close()
    # Content-current, so only the absolute-path probe can trigger the heal.
    mark_fts_content_current(repo.fts_dir)
    assert all(Path(p).is_absolute() for p in repo.fts_docs_per_path())


def _empty_fts_index(repo: _CliRepo) -> None:
    """Leave an existing, current-schema FTS index with zero documents --
    the state the pre-fix `cidx index --fts` bootstrap committed."""
    import shutil

    shutil.rmtree(repo.fts_dir)
    fts = TantivyIndexManager(repo.fts_dir)
    fts.initialize_index(create_new=True)
    try:
        fts.commit()
    finally:
        fts.close()
    # Content-current, so only the empty-index probe can trigger the heal.
    mark_fts_content_current(repo.fts_dir)
    assert repo.fts_docs_per_path() == Counter()


class TestIndexHoldingAbsolutePathsSelfHeals2056:
    def test_refresh_heals_index_holding_absolute_paths(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """Repositories whose FTS index was built by the pre-fix rebuild
        (MCP add_golden_repo_index) converge on their next refresh: no
        absolute-path document survives, nothing is duplicated."""
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        _seed_pre_fix_rebuild_index(repo)

        assert repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS) == 0

        _assert_every_file_searchable(repo)
        assert repo.fts_paths_for("AUTHTOKEN", path_filters=["src/*"]) == [
            "src/auth.py"
        ]

    def test_refresh_heals_empty_fts_index_left_by_old_bug(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """The pre-fix bootstrap left repositories with an existing but
        EMPTY FTS index. Once files are older than the 60 s safety buffer a
        no-change refresh selects no file at all, so nothing would ever
        refill it: the refresh must rebuild it from disk instead."""
        repo = _make_repo(tmp_path, fake_provider)
        an_hour_ago = time.time() - 3600
        for relative in ALL_PATHS:
            os.utime(repo.path / relative, (an_hour_ago, an_hour_ago))
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        _empty_fts_index(repo)

        assert repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS) == 0

        _assert_every_file_searchable(repo)

    def test_rebuild_replaces_index_holding_absolute_paths(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        normal = repo.fts_documents()
        _seed_pre_fix_rebuild_index(repo)

        assert repo.cidx("index", "--rebuild-fts-index") == 0

        assert repo.fts_documents() == normal


class TestRebuildFtsIndexStoresRelativePaths2056:
    def test_rebuild_fts_index_stores_repo_relative_paths(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """`cidx index --rebuild-fts-index` (run by the server for MCP
        add_golden_repo_index) must store the same repo-relative paths as
        normal indexing: path filters and per-path supersession depend on
        it."""
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        normal = repo.fts_documents()

        assert repo.cidx("index", "--rebuild-fts-index") == 0

        # Normal indexing's documents, under repo-relative paths.
        assert repo.fts_documents() == normal
        _assert_every_file_searchable(repo)
        assert repo.fts_paths_for("AUTHTOKEN", path_filters=["src/*"]) == [
            "src/auth.py"
        ]

        # A later incremental run supersedes the rebuilt document.
        repo.write("src/auth.py", "def check_password(pw):\n    return 'NEWAUTH'\n")
        repo.commit("edit auth")
        assert repo.cidx("index", "--fts") >= 1
        assert repo.fts_paths_for("NEWAUTH") == ["src/auth.py"]
        assert repo.fts_paths_for("AUTHTOKEN") == []
        assert repo.fts_docs_per_path()["src/auth.py"] == 1


# On-disk contract: the FTS content-version marker inside the index dir.
_CONTENT_MARKER_FILE = "cidx_fts_content_version"
_REBUILT = "Creating new Tantivy index"
_REUSED = "Opening existing Tantivy index"


def _backdate_all_files(repo: _CliRepo) -> None:
    """Move every file past the 60 s safety buffer, so a no-change refresh
    selects no file (the steady state of a long-lived golden repo)."""
    an_hour_ago = time.time() - 3600
    for path in repo.path.rglob("*"):
        relative_parts = path.relative_to(repo.path).parts
        if path.is_file() and relative_parts[0] not in (".git", ".code-indexer"):
            os.utime(path, (an_hour_ago, an_hour_ago))


class TestFtsContentVersionOneTimeRebuild2056:
    """Every FTS index built before the #2056 fix may silently miss
    documents (cache-reused chunks were dropped). Such an index carries no
    content-version marker and is rebuilt from disk ONCE, on its next
    normal `cidx index --fts` (the next golden-repo refresh), never again.

    `_CliRepo.cidx()` asserts exit code 0 and returns the number of inputs
    sent for embedding."""

    def test_fresh_index_carries_marker_and_never_rebuilds(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        counts = repo.fts_docs_per_path()

        for _ in range(2):
            embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
            assert embedded == 0
            assert _REUSED in repo.last_output
            assert _REBUILT not in repo.last_output
            assert repo.fts_docs_per_path() == counts

    def test_index_without_marker_rebuilds_exactly_once(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        _backdate_all_files(repo)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink(missing_ok=True)  # pre-fix

        embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert embedded == 0, "the rebuild reads files from disk, never embeds"
        assert _REBUILT in repo.last_output
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        _assert_every_file_searchable(repo)

        embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert embedded == 0
        assert _REUSED in repo.last_output
        assert _REBUILT not in repo.last_output
        _assert_every_file_searchable(repo)

    def test_partly_emptied_pre_fix_index_is_complete_after_one_refresh(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        _backdate_all_files(repo)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        # What the old cached-chunk bug left: SOME files' documents gone.
        fts = TantivyIndexManager(repo.fts_dir)
        fts.initialize_index(create_new=False)
        try:
            fts.delete_document("src/auth.py")
            fts.delete_document("tests/test_auth.py")
        finally:
            fts.close()
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink(missing_ok=True)  # pre-fix
        assert set(repo.fts_docs_per_path()) == {"src/cart.js", "src/big.py"}

        embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)

        assert embedded == 0
        _assert_every_file_searchable(repo)

    def test_run_failing_after_rebuild_leaves_no_marker_and_rebuilds_next_time(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink(missing_ok=True)  # pre-fix
        repo.write(
            "src/cart.js",
            "function addToCart(item) { return 'CARTTOKEN'; } // CARTV2\n",
        )
        repo.commit("edit cart")
        # A directory where SQLite wants its rollback journal: a real,
        # offline SQLITE_IOERR on the chunk-store write -> the run fails
        # after the FTS rebuild was committed.
        [chunks_db] = list((repo.path / ".code-indexer" / "index").rglob("chunks.db"))
        journal = chunks_db.parent / "chunks.db-journal"
        journal.mkdir()

        repo.cidx_expect_failure("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert _REBUILT in repo.last_output
        assert repo.fts_docs_per_path(), "the rebuild itself was committed"
        assert not (repo.fts_dir / _CONTENT_MARKER_FILE).exists()

        journal.rmdir()
        embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert embedded >= 1, "the edited file is embedded on the retry"
        assert _REBUILT in repo.last_output
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        _assert_every_file_searchable(repo)
        assert repo.fts_paths_for("CARTV2") == ["src/cart.js"]


# Whitespace only: the chunker yields no chunk, so normal indexing gives it
# no FTS document -- every rebuild path must do exactly the same.
_BLANK_FILE = "src/blank.py"


def _match_lines(repo: _CliRepo, token: str) -> List[tuple]:
    fts = TantivyIndexManager(repo.fts_dir)
    fts.initialize_index(create_new=False)
    try:
        hits = fts.search(token, limit=50)
    finally:
        fts.close()
    return sorted((hit["path"], hit["line"]) for hit in hits)


def _make_repo_with_blank_file(root: Path, server) -> _CliRepo:
    root.mkdir()
    repo = _make_repo(root, server)
    repo.write(_BLANK_FILE, "   \n\n")
    repo.commit("blank file")
    return repo


class TestRebuildEqualsNormalIndexing2056:
    """Every FTS rebuild path (#1763 bootstrap, content-version rebuild,
    `--rebuild-fts-index`) must produce EXACTLY the chunk-level documents
    normal indexing produces -- same paths, line ranges, content and
    identifiers -- without any embedding call."""

    def _assert_both_matches_found(self, repo: _CliRepo) -> None:
        matches = _match_lines(repo, "TWICETOKEN")
        assert [path for path, _line in matches] == ["src/big.py", "src/big.py"]
        assert matches[0][1] != matches[1][1], matches

    def test_marker_rebuild_produces_normal_indexing_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo_with_blank_file(tmp_path / "r", fake_provider)
        # Steady state: no file is re-selected by the refresh, so the
        # compared documents come from the rebuild alone.
        _backdate_all_files(repo)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        normal = repo.fts_documents()
        self._assert_both_matches_found(repo)
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink()

        embedded = repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)

        assert embedded == 0
        assert _REBUILT in repo.last_output
        assert repo.fts_documents() == normal
        self._assert_both_matches_found(repo)

    def test_cli_rebuild_fts_index_produces_normal_indexing_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo_with_blank_file(tmp_path / "r", fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        normal = repo.fts_documents()

        embedded = repo.cidx("index", "--rebuild-fts-index")

        assert embedded == 0
        assert repo.fts_documents() == normal
        self._assert_both_matches_found(repo)
        # Marked content-current: the next refresh reuses it.
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert _REUSED in repo.last_output

    def test_bootstrap_produces_normal_indexing_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        fresh = _make_repo_with_blank_file(tmp_path / "fresh", fake_provider)
        fresh.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        normal = fresh.fts_documents()

        # The issue's scenario: semantic index first, then add FTS.
        repo = _make_repo_with_blank_file(tmp_path / "boot", fake_provider)
        # Steady state: the `--fts` run re-selects no file, so its documents
        # come from the bootstrap alone.
        _backdate_all_files(repo)
        repo.cidx("index")
        embedded = repo.cidx("index", "--fts")

        assert embedded == 0
        assert repo.fts_documents() == normal
        self._assert_both_matches_found(repo)


class TestFtsFailuresAreLoud2056:
    """Per-file FTS failures follow the semantic rule: the run fails only
    when no file could be indexed. Otherwise it succeeds, states how many
    files are missing from FTS, and leaves the index without a current
    content marker so the next run retries. Infrastructure failures (setup,
    commit) always fail the run."""

    def test_rebuild_with_unreadable_file_succeeds_unmarked_and_retries_next_run(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        _backdate_all_files(repo)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink()  # a pre-fix index
        unreadable = repo.path / "src" / "cart.js"
        os.chmod(unreadable, 0)
        try:
            # repo.cidx() asserts exit code 0 itself.
            repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        finally:
            os.chmod(unreadable, 0o644)
        _assert_partial_fts(repo, missing_path="src/cart.js")

        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert _REBUILT in repo.last_output
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        _assert_every_file_searchable(repo)

    def test_cli_rebuild_with_unreadable_file_succeeds_unmarked(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        unreadable = repo.path / "src" / "cart.js"
        os.chmod(unreadable, 0)
        try:
            # repo.cidx() asserts exit code 0 itself.
            repo.cidx("index", "--rebuild-fts-index")
        finally:
            os.chmod(unreadable, 0o644)
        _assert_partial_fts(repo, missing_path="src/cart.js")

    def test_bootstrap_with_every_file_unreadable_exits_1(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """Semantic index unchanged (nothing for it to fail), FTS bootstrap
        cannot read any file: the FTS failure alone fails the run."""
        repo = _make_repo(tmp_path, fake_provider)
        _backdate_all_files(repo)
        repo.cidx("index")
        with _all_files_unreadable(repo):
            code = repo.cidx_expect_failure("index", "--fts")
        assert code == 1, "generic failure, never chunk-store codes 86/87"
        assert "FTS" in repo.last_output
        assert not (repo.fts_dir / _CONTENT_MARKER_FILE).exists()

    def test_cli_rebuild_with_every_file_unreadable_exits_1(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        with _all_files_unreadable(repo):
            code = repo.cidx_expect_failure("index", "--rebuild-fts-index")
        assert code == 1
        assert not (repo.fts_dir / _CONTENT_MARKER_FILE).exists()

    def test_fts_setup_failure_fails_the_run_with_generic_code(
        self, tmp_path: Path, fake_provider
    ) -> None:
        import shutil

        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        shutil.rmtree(repo.fts_dir)
        repo.fts_dir.write_text("not a directory")

        code = repo.cidx_expect_failure("index", "--fts")

        assert code == 1, "generic failure, never chunk-store codes 86/87"
        assert "FTS" in repo.last_output

    def test_file_made_blank_loses_its_fts_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert repo.fts_paths_for("CARTTOKEN") == ["src/cart.js"]

        repo.write("src/cart.js", "   \n")
        repo.commit("blank cart")
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)

        assert "src/cart.js" not in repo.fts_docs_per_path()
        assert repo.fts_paths_for("CARTTOKEN") == []

    def test_cli_rebuild_refuses_while_indexing_lock_is_held(
        self, tmp_path: Path, fake_provider
    ) -> None:
        from code_indexer.services.indexing_lock import create_indexing_lock

        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        documents = repo.fts_documents()
        lock = create_indexing_lock(repo.path / ".code-indexer")
        lock.acquire(str(repo.path))
        try:
            code = repo.cidx_expect_failure("index", "--rebuild-fts-index")
        finally:
            lock.release()

        assert code == 1
        assert repo.fts_documents() == documents, "nothing was cleared"


def _start_watch_fts(repo: _CliRepo) -> Optional[FtsRebuildResult]:
    """Exactly what `cidx watch` runs to open its FTS index (the shared
    fts_lifecycle path); returns the rebuild's result, None if reused."""
    from code_indexer.config import ConfigManager
    from code_indexer.services.fts_lifecycle import open_fts_index_for_watch

    config = ConfigManager.load_verified_config(repo.path)
    fts, rebuild = open_fts_index_for_watch(config)
    fts.close()
    return rebuild


class TestWatchStartedIndex2056:
    """`cidx watch` opens its FTS index through the SAME decision and
    rebuild as `cidx index --fts`: whatever it rebuilds ends with exactly
    normal indexing's documents and is marked content-current."""

    def _normal(self, tmp_path: Path, fake_provider) -> Tuple[_CliRepo, Counter]:
        repo = _make_repo_with_blank_file(tmp_path / "r", fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        return repo, repo.fts_documents()

    def test_watch_start_without_index_builds_normal_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        import shutil

        repo, normal = self._normal(tmp_path, fake_provider)
        shutil.rmtree(repo.fts_dir)

        assert _start_watch_fts(repo) is not None
        assert repo.fts_documents() == normal
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()

    def test_watch_start_on_partly_emptied_unmarked_index_rebuilds(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo, normal = self._normal(tmp_path, fake_provider)
        fts = TantivyIndexManager(repo.fts_dir)
        fts.initialize_index(create_new=False)
        try:
            fts.delete_document("src/auth.py")
        finally:
            fts.close()
        (repo.fts_dir / _CONTENT_MARKER_FILE).unlink()  # a pre-#2056 index

        assert _start_watch_fts(repo) is not None
        assert repo.fts_documents() == normal
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()

    def test_watch_start_on_stale_schema_index_rebuilds(
        self, tmp_path: Path, fake_provider
    ) -> None:
        import shutil

        import tantivy

        repo, normal = self._normal(tmp_path, fake_provider)
        shutil.rmtree(repo.fts_dir)
        repo.fts_dir.mkdir()
        builder = tantivy.SchemaBuilder()  # pre-#1761: no exact-path field
        builder.add_text_field("path", stored=True)
        legacy = tantivy.Index(builder.build(), str(repo.fts_dir))
        writer = legacy.writer(50_000_000)
        writer.add_document(tantivy.Document(path="legacy_only.py"))
        writer.commit()
        writer.wait_merging_threads()

        assert _start_watch_fts(repo) is not None
        assert repo.fts_documents() == normal
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()

    def test_watch_start_on_current_index_reuses_it(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo, normal = self._normal(tmp_path, fake_provider)

        assert _start_watch_fts(repo) is None
        assert repo.fts_documents() == normal


# Test-only fault injection at the external FTS library boundary, inside the
# cidx child process: add_document fails for one path.
_FAIL_FTS_ADD_SITECUSTOMIZE = """
import os

_FAIL_PATH = os.environ.get("CIDX_TEST_FAIL_FTS_ADD_PATH")
if _FAIL_PATH:
    from code_indexer.services import tantivy_index_manager as _tim

    _real_add = _tim.TantivyIndexManager.add_document

    def _add_document(self, doc):
        if doc.get("path") == _FAIL_PATH:
            raise OSError("injected FTS write failure")
        return _real_add(self, doc)

    _tim.TantivyIndexManager.add_document = _add_document
"""


# Test-only fault injection at the external embedding-provider boundary,
# inside the cidx child process: every batch holding the token fails (one
# batch per file, so exactly that file's embedding fails).
_FAIL_EMBED_SITECUSTOMIZE = """
import os

_FAIL_TOKEN = os.environ.get("CIDX_TEST_FAIL_EMBED_TOKEN")
if _FAIL_TOKEN:
    import code_indexer.services.voyage_ai as _voyage

    _real_batch = _voyage.VoyageAIClient.get_embeddings_batch

    def _get_embeddings_batch(self, texts, *args, **kwargs):
        if any(_FAIL_TOKEN in text for text in texts):
            raise RuntimeError("injected embedding failure")
        return _real_batch(self, texts, *args, **kwargs)

    _voyage.VoyageAIClient.get_embeddings_batch = _get_embeddings_batch
"""


@contextlib.contextmanager
def _injected(repo: _CliRepo, script: str, env: Dict[str, str]) -> Iterator[None]:
    """Run the repo's next cidx children with `script` as sitecustomize."""
    inject_dir = repo.path.parent / "inject"
    inject_dir.mkdir(exist_ok=True)
    (inject_dir / "sitecustomize.py").write_text(script)
    clean_env = dict(repo.env)
    repo.env["PYTHONPATH"] = f"{inject_dir}{os.pathsep}{clean_env['PYTHONPATH']}"
    repo.env.update(env)
    try:
        yield
    finally:
        repo.env = clean_env


class TestFtsRunPerFileFailures2056:
    """Per-file FTS failures inside a `cidx index --fts` run: the run
    succeeds (other files indexed), says how many files are missing from
    FTS, leaves the index unmarked, and the next run rebuilds it."""

    def test_per_file_fts_write_failure_succeeds_unmarked_and_next_run_heals(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        repo.write(
            "src/cart.js",
            "function addToCart(item) { return 'CARTTOKEN'; } // CARTV2\n",
        )
        repo.commit("edit cart")
        with _injected(
            repo,
            _FAIL_FTS_ADD_SITECUSTOMIZE,
            {"CIDX_TEST_FAIL_FTS_ADD_PATH": "src/cart.js"},
        ):
            # repo.cidx() asserts exit code 0 itself.
            repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        _assert_partial_fts(repo, missing_path="src/cart.js")

        repo.cidx("index", "--fts", *_SERVER_LAYOUT_ARGS)
        assert _REBUILT in repo.last_output
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        _assert_every_file_searchable(repo)
        assert repo.fts_paths_for("CARTV2") == ["src/cart.js"]

    def test_clear_fts_with_unreadable_file_succeeds_unmarked_and_next_run_heals(
        self, tmp_path: Path, fake_provider
    ) -> None:
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        unreadable = repo.path / "src" / "cart.js"
        os.chmod(unreadable, 0)
        try:
            # repo.cidx() asserts exit code 0 itself.
            repo.cidx("index", "--clear", "--fts")
        finally:
            os.chmod(unreadable, 0o644)
        _assert_partial_fts(repo, missing_path="src/cart.js")

        repo.cidx("index", "--fts")
        assert _REBUILT in repo.last_output
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()
        _assert_every_file_searchable(repo)

    def test_file_whose_embedding_fails_still_has_its_fts_documents(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """Opus P3-2: FTS needs no embedding, so a full re-index builds the
        FTS documents of a file whose embedding failed from disk."""
        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")
        with _injected(
            repo,
            _FAIL_EMBED_SITECUSTOMIZE,
            {"CIDX_TEST_FAIL_EMBED_TOKEN": "CARTTOKEN"},
        ):
            # repo.cidx() asserts exit code 0 itself.
            repo.cidx("index", "--clear", "--fts")

        _assert_every_file_searchable(repo)
        assert (repo.fts_dir / _CONTENT_MARKER_FILE).is_file()


_WATCH_READY = "Watchdog observer started monitoring"
_WATCH_START_TIMEOUT_SECONDS = 60


class TestWatchFrontDoor2056:
    def test_watch_starts_on_a_freshly_indexed_repo(
        self, tmp_path: Path, fake_provider
    ) -> None:
        """Real `cidx watch` on a repo just indexed with `cidx index --fts`
        detects its semantic and FTS indexes and starts watching (it used to
        print "No indexes found" and exit 1: wrong index paths)."""
        import queue
        import signal
        import threading

        repo = _make_repo(tmp_path, fake_provider)
        repo.cidx("index", "--fts")

        proc = subprocess.Popen(
            [sys.executable, "-m", "code_indexer.cli", "watch"],
            cwd=repo.path,
            env=repo.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines: "queue.Queue[str]" = queue.Queue()

        def _pump() -> None:
            for line in proc.stdout or []:
                lines.put(line)

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()
        output: List[str] = []
        deadline = time.monotonic() + _WATCH_START_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                try:
                    output.append(lines.get(timeout=1))
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if _WATCH_READY in output[-1]:
                    break
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        text = "".join(output)

        assert "No indexes found" not in text, text
        assert "Semantic index" in text and "FTS index" in text, text
        assert _WATCH_READY in text, text
