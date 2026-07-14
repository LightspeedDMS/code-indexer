"""Regression tests for Bug #1401.

xray_search background jobs failed with a pathlib ValueError whenever the
trigram pre-filter path was used against a repository whose data directory
sits behind a symlink. Root cause: RegexSearchService.__init__ stored
self.repo_path verbatim (never re-resolved), while _prefilter_candidate_files
resolved candidate paths -- desyncing every subsequent relative_to()
comparison against self.repo_path. Several output-parsing call sites also
silently fell back to storing an absolute (symlink-resolved) path as
file_path on a relative_to() ValueError, which later broke an unguarded
relative_to() call downstream in XRaySearchEngine.

Fix direction (see issue #1401): canonicalize self.repo_path ONCE at
construction, and share one containment-checked absolute-or-relative ->
repo-relative conversion helper across every output-parsing site (ripgrep
JSON, grep-mode, Python-multiline fallback). A path that does not resolve
inside the canonical repo root -- whether reported as absolute or as a
relative string that escapes via "../" or an internal symlink -- must be
dropped with a logged warning, never silently included as file_path.
"""

import json
import logging
import os
import shutil

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

pytestmark_rg = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


def _mk_match(
    path_text: str, lines_text: str, line_number: int, submatch_text: str
) -> dict:
    """Build a ripgrep JSON match entry (matches real `rg --json` schema)."""
    return {
        "type": "match",
        "data": {
            "path": {"text": path_text},
            "lines": {"text": lines_text},
            "line_number": line_number,
            "absolute_offset": 0,
            "submatches": [
                {
                    "match": {"text": submatch_text},
                    "start": 0,
                    "end": len(submatch_text),
                }
            ],
        },
    }


def _build_repo(tmp_path):
    """Physical repo directory with a couple of real files."""
    repo = tmp_path / "physical" / "repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "LSAuthenticator.java").write_text(
        "package auth;\npublic class LSAuthenticator {\n  void login() {}\n}\n"
    )
    (repo / "README.md").write_text("This project has authentication.\n")
    return repo


def _symlinked_alias(tmp_path, repo):
    """Symlinked alias root pointing at the physical repo (production shape:
    /opt/code-indexer/.cidx-server -> /mnt/codeindexer-data/cidx-server)."""
    alias_root = tmp_path / "alias"
    alias_root.mkdir()
    link = alias_root / "repo"
    link.symlink_to(repo, target_is_directory=True)
    return link


class TestToRepoRelativeHelper:
    """Unit tests for the shared containment-checked path conversion helper."""

    def test_absolute_path_inside_repo_converted_to_relative(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("x = 1\n")
        service = RegexSearchService(repo)

        result = service._to_repo_relative(str(repo / "src" / "main.py"))

        assert result == "src/main.py"

    def test_absolute_path_outside_repo_rejected_and_logged(self, tmp_path, caplog):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("nope\n")
        service = RegexSearchService(repo)

        with caplog.at_level(logging.WARNING):
            result = service._to_repo_relative(str(outside))

        assert result is None
        assert any(
            "outside" in rec.message.lower() or "escape" in rec.message.lower()
            for rec in caplog.records
        ), "expected a warning to be logged when a match path escapes the repo root"

    def test_relative_path_inside_repo_returned_unchanged(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("x = 1\n")
        service = RegexSearchService(repo)

        result = service._to_repo_relative("src/main.py")

        assert result == "src/main.py"

    def test_relative_path_with_dotdot_escape_rejected_and_logged(
        self, tmp_path, caplog
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("nope\n")
        service = RegexSearchService(repo)

        with caplog.at_level(logging.WARNING):
            result = service._to_repo_relative("../outside.txt")

        assert result is None
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_relative_path_escaping_via_internal_symlink_rejected(self, tmp_path):
        """'Genuinely relative' must mean relative AND contained -- an internal
        symlink that resolves outside the repo is rejected identically to an
        absolute-outside-repo path (issue fix-direction requirement #4)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("nope\n")
        (repo / "escape_link").symlink_to(outside_dir, target_is_directory=True)
        service = RegexSearchService(repo)

        result = service._to_repo_relative("escape_link/secret.txt")

        assert result is None

    def test_symlinked_repo_root_absolute_physical_path_converted(self, tmp_path):
        """The exact production shape: repo_path passed to the service is an
        unresolved symlink; a subprocess reports back the resolved PHYSICAL
        absolute path. Must still convert to repo-relative correctly."""
        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)
        service = RegexSearchService(link)

        physical_abs = str(
            (physical / "src" / "auth" / "LSAuthenticator.java").resolve()
        )
        result = service._to_repo_relative(physical_abs)

        assert result == "src/auth/LSAuthenticator.java"


class TestConstructionCanonicalizesRepoPath:
    def test_symlinked_repo_path_resolved_at_construction(self, tmp_path):
        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)

        service = RegexSearchService(link)

        assert service.repo_path == physical.resolve()

    def test_non_symlinked_repo_path_unchanged(self, tmp_path):
        """Regression: normal (non-symlinked) construction must be unaffected."""
        repo = tmp_path / "repo"
        repo.mkdir()

        service = RegexSearchService(repo)

        assert service.repo_path == repo.resolve()


class TestRipgrepJsonParsingContainment:
    def test_match_outside_repo_root_dropped_not_included(self, tmp_path, caplog):
        repo = tmp_path / "repo"
        repo.mkdir()
        service = RegexSearchService(repo)
        outside_path = str(tmp_path / "outside" / "secret.txt")
        output = json.dumps(_mk_match(outside_path, "hello world\n", 1, "hello"))

        with caplog.at_level(logging.WARNING):
            matches, total = service._parse_ripgrep_json_output(output, 100, 0)

        assert matches == []
        assert total == 0
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_match_inside_symlinked_repo_root_normalized_to_relative(self, tmp_path):
        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)
        service = RegexSearchService(link)

        # Simulate ripgrep echoing back the resolved PHYSICAL path, as it does
        # when handed an already-resolved absolute file path (trigram
        # pre-filter branch -- the exact Bug #1401 reproduction shape).
        physical_abs = str(
            (physical / "src" / "auth" / "LSAuthenticator.java").resolve()
        )
        output = json.dumps(
            _mk_match(physical_abs, "public class LSAuthenticator {\n", 2, "class")
        )

        matches, total = service._parse_ripgrep_json_output(output, 100, 0)

        assert total == 1
        assert len(matches) == 1
        assert matches[0].file_path == "src/auth/LSAuthenticator.java"


class TestGrepOutputParsingContainment:
    def test_grep_match_escaping_repo_root_dropped(self, tmp_path, caplog):
        repo = tmp_path / "repo"
        repo.mkdir()
        service = RegexSearchService(repo)
        output = "../outside.txt:1:secret content here\n"

        with caplog.at_level(logging.WARNING):
            matches, total = service._parse_grep_output(output, 100, 0)

        assert matches == []
        assert total == 0
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_grep_match_legitimate_relative_path_retained(self, tmp_path):
        """Grep-mode sometimes legitimately reports relative filenames --
        those must still be accepted (not blanket-rejected)."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("x = 1\n")
        service = RegexSearchService(repo)
        output = "src/main.py:1:x = 1\n"

        matches, total = service._parse_grep_output(output, 100, 0)

        assert total == 1
        assert matches[0].file_path == "src/main.py"


class TestPythonMultilineFallbackContainment:
    def test_symlinked_repo_returns_repo_relative_paths(self, tmp_path):
        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)
        service = RegexSearchService(link)

        matches, total = service._search_python_multiline(
            pattern=r"class[\s\S]*login",
            search_path=service.repo_path,
            include_patterns=None,
            exclude_patterns=None,
            case_sensitive=True,
            max_results=100,
        )

        assert total == 1
        assert matches[0].file_path == "src/auth/LSAuthenticator.java"


@pytestmark_rg
class TestSymlinkedRepoEndToEnd:
    """Real subprocess (ripgrep) end-to-end reproduction of Bug #1401."""

    @pytest.fixture(autouse=True)
    def _no_lazy_build(self, monkeypatch):
        monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")

    async def test_trigram_prefilter_branch_symlinked_repo_no_exception(self, tmp_path):
        """Reproduces the exact production failure: trigram index present,
        repo reached via a symlinked alias root. Must not raise ValueError,
        and results must be repo-relative."""
        from code_indexer.global_repos.trigram_index_manager import (
            TrigramIndexManager,
        )

        physical = _build_repo(tmp_path)
        TrigramIndexManager(physical / ".code-indexer" / "trigram_index").build(
            physical
        )
        link = _symlinked_alias(tmp_path, physical)

        service = RegexSearchService(link)
        result = await service.search("Authenticator", max_results=1000)

        assert result.total_matches >= 1
        for m in result.matches:
            assert not os.path.isabs(m.file_path), (
                f"match file_path must be repo-relative, got {m.file_path!r}"
            )
        assert {m.file_path for m in result.matches} == {
            "src/auth/LSAuthenticator.java"
        }

    async def test_fullscan_branch_symlinked_repo_matches_prefilter_branch(
        self, tmp_path
    ):
        """Full-scan (no trigram index) and pre-filtered (indexed) branches
        must return identical results for the same symlinked repo -- the
        prefilter is an optimization, not a behavior change."""
        from code_indexer.global_repos.trigram_index_manager import (
            TrigramIndexManager,
        )

        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)

        # Full-scan: no trigram index yet.
        assert not (physical / ".code-indexer" / "trigram_index").exists()
        service_fullscan = RegexSearchService(link)
        fullscan_result = await service_fullscan.search(
            "Authenticator", max_results=1000
        )

        # Pre-filtered: build the trigram index, then search again.
        TrigramIndexManager(physical / ".code-indexer" / "trigram_index").build(
            physical
        )
        service_prefilter = RegexSearchService(link)
        prefilter_result = await service_prefilter.search(
            "Authenticator", max_results=1000
        )

        def _key(result):
            return sorted((m.file_path, m.line_number) for m in result.matches)

        assert _key(fullscan_result) == _key(prefilter_result)

    async def test_legacy_cli_pattern_symlinked_repo_returns_relative_not_absolute(
        self, tmp_path
    ):
        """Mirrors cli.py:18883-18891's legacy `global regex-search` command:
        `repo_path = Path(index_path)` (NEVER resolved) then
        `RegexSearchService(repo_path)`. Per issue #1401, this caller's
        observable file_path output CHANGES from an incorrect
        physical-absolute path to a correct repo-relative path for a
        symlinked repo -- documented as a bug fix, not a regression."""
        physical = _build_repo(tmp_path)
        link = _symlinked_alias(tmp_path, physical)

        # Exact call shape from cli.py's global_regex_search command.
        repo_path = link  # Path(index_path), unresolved -- as cli.py does it
        service = RegexSearchService(repo_path)
        result = await service.search("Authenticator", max_results=1000)

        assert len(result.matches) >= 1
        for m in result.matches:
            assert not os.path.isabs(m.file_path)
        assert {m.file_path for m in result.matches} == {
            "src/auth/LSAuthenticator.java"
        }
