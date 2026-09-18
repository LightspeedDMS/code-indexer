"""Parity coverage for ripgrep glob semantics on indexed repositories."""

import shutil

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _build_repo(tmp_path):
    repo = tmp_path / "source-repo"
    files = {
        "README.md": "GlobParityNeedle top-level markdown\n",
        "nested/guide.md": "GlobParityNeedle nested markdown\n",
        "src/a.ts": "GlobParityNeedle shallow TypeScript\n",
        "src/a/b.ts": "GlobParityNeedle deep TypeScript\n",
        "docs/guide.md": "GlobParityNeedle docs markdown\n",
        "docs/reference/api.md": "GlobParityNeedle nested docs markdown\n",
        "node_modules/a.js": "GlobParityNeedle dependency JavaScript\n",
        "notes.txt": "GlobParityNeedle plain text\n",
        "tests/unit_test.py": "GlobParityNeedle bare directory token test\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
    return repo


def _index(repo):
    manager = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    manager.build(repo)


async def _matching_files(repo, include_patterns=None, exclude_patterns=None):
    result = await RegexSearchService(repo).search(
        "GlobParityNeedle",
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        max_results=1000,
    )
    return {match.file_path for match in result.matches}


@pytest.mark.parametrize(
    ("include_patterns", "exclude_patterns", "expected_files"),
    [
        (
            ["*.md"],
            None,
            {"README.md", "nested/guide.md", "docs/guide.md", "docs/reference/api.md"},
        ),
        (
            ["**/*.md"],
            None,
            {"README.md", "nested/guide.md", "docs/guide.md", "docs/reference/api.md"},
        ),
        (["src/*.ts"], None, {"src/a.ts"}),
        (["docs/**"], None, {"docs/guide.md", "docs/reference/api.md"}),
        (
            None,
            ["node_modules"],
            {
                "README.md",
                "nested/guide.md",
                "src/a.ts",
                "src/a/b.ts",
                "docs/guide.md",
                "docs/reference/api.md",
                "notes.txt",
                "tests/unit_test.py",
            },
        ),
        (
            None,
            ["*.md"],
            {
                "src/a.ts",
                "src/a/b.ts",
                "node_modules/a.js",
                "notes.txt",
                "tests/unit_test.py",
            },
        ),
        (
            ["**/*.md"],
            ["docs/**"],
            {"README.md", "nested/guide.md"},
        ),
        # Bug #1876 item 3: bare directory tokens (no wildcard, no dot, no
        # slash) -- the pinned "safest consistent" policy matches contents
        # recursively on BOTH the indexed matcher and the unindexed rg -g
        # walk (normalize_glob_pattern applies the same expansion to both).
        (
            ["docs"],
            None,
            {"docs/guide.md", "docs/reference/api.md"},
        ),
        (
            ["docs/"],
            None,
            {"docs/guide.md", "docs/reference/api.md"},
        ),
        (
            ["tests"],
            None,
            {"tests/unit_test.py"},
        ),
        # Bug #1876 item 3: leading './' stripped identically on both paths.
        (
            ["./src/*.ts"],
            None,
            {"src/a.ts"},
        ),
        # Bug #1876 items 1/3: brace-group expansion -- ripgrep expands
        # braces itself on the unindexed path; the matcher's _expand_braces
        # does the equivalent on the indexed path. Bare (no-slash) pattern
        # matches at any depth on both sides.
        (
            ["*.{ts,md}"],
            None,
            {
                "README.md",
                "nested/guide.md",
                "docs/guide.md",
                "docs/reference/api.md",
                "src/a.ts",
                "src/a/b.ts",
            },
        ),
    ],
)
async def test_indexed_and_unindexed_glob_results_are_identical(
    tmp_path, include_patterns, exclude_patterns, expected_files
):
    """The trigram explicit-file path must match the directory-walk path."""
    source_repo = _build_repo(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    _index(indexed_repo)

    walk_files = await _matching_files(walk_repo, include_patterns, exclude_patterns)
    indexed_files = await _matching_files(
        indexed_repo, include_patterns, exclude_patterns
    )

    assert walk_files == expected_files
    assert indexed_files == walk_files


# ---------------------------------------------------------------------------
# Bug #1876 N1: an include that names a directory must return the directory
# itself (if it also happens to be a literal file path segment) and, per
# the accepted bare-directory policy, its recursive contents -- but a
# pattern like "src/*" or "**/docs" must NOT implicitly widen to include
# every file nested arbitrarily deep below a matched prefix. ripgrep is the
# reference (confirmed live against real ripgrep 14.1.1): "*" matches
# exactly one path segment, full stop -- a directory match never implies
# "and everything under it" unless the pattern itself says so (a bare
# directory token, or an explicit "/**" suffix).
#
# This fixture is independent of _build_repo/test_indexed_and_unindexed_
# glob_results_are_identical above (deliberately -- adding these files to
# the shared fixture would change several of that test's already-pinned
# expected sets, e.g. "*.md" gaining "src/c.md"). Every row below was
# verified against REAL ripgrep 14.1.1 (walk mode) before being written
# here -- see the session's verification transcript.
# ---------------------------------------------------------------------------


def _build_repo_n1(tmp_path):
    repo = tmp_path / "n1-repo"
    files = {
        "README.md": "GlobParityNeedle root readme\n",
        "src/a.ts": "GlobParityNeedle shallow ts\n",
        "src/c.md": "GlobParityNeedle shallow md\n",
        "src/a/b.ts": "GlobParityNeedle nested ts under src/a\n",
        "src/tests/t.py": "GlobParityNeedle nested test file\n",
        "tests/unit_test.py": "GlobParityNeedle root-level tests dir content\n",
        "lib/docs/api.md": "GlobParityNeedle lib docs content\n",
        "pkg/docs/x.md": "GlobParityNeedle pkg docs content\n",
        "site/docs/guide.md": "GlobParityNeedle site docs content\n",
        "docs/guide.md": "GlobParityNeedle bare docs dir content\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
    return repo


# The bare-directory policy's "**/docs" + "**/docs/**" expansion matches at
# ANY depth -- lib/docs/, pkg/docs/, site/docs/ all qualify, not just a
# root-level docs/ directory. Verified directly against pathspec: all four
# *.md files under any "docs" directory match "**/docs/**".
_ALL_DOCS_CONTENTS = {
    "docs/guide.md",
    "lib/docs/api.md",
    "pkg/docs/x.md",
    "site/docs/guide.md",
}


@pytest.mark.parametrize(
    ("include_patterns", "expected_files"),
    [
        # src/* -- single path segment under src/ only; must NOT recurse
        # into src/a/ or src/tests/.
        (["src/*"], {"src/a.ts", "src/c.md"}),
        # Raw "**/tests"/"*/tests" (NOT the bare-token form below) are
        # literal, non-recursive path-segment patterns: real ripgrep
        # matches them only against a FILE literally named "tests", and
        # this fixture has none, so both match nothing.
        (["**/tests"], set()),
        (["*/tests"], set()),
        (["lib/docs"], set()),
        # src/a* -- single-segment glob; must NOT recurse into src/a/.
        (["src/a*"], {"src/a.ts"}),
        (["**/docs"], set()),
        # Bug #1876 F10: an explicit trailing-slash / directory-only
        # INCLUDE form must select the matched directories' CONTENTS
        # (real ripgrep confirmed: `rg -g 'src/*/' -g 'src/*/**'` etc --
        # a single directory-only -g glob alone never lists a FILE, but
        # the shared matcher/regex_search machinery always emits a
        # paired "/**" content line alongside it). "src/*/" selects
        # files under any DIRECT subdirectory of src (src/a/, src/tests/)
        # but never a file directly in src/ itself. "src/**/" widens
        # that to ANY depth under src (zero-or-more directories), which
        # includes files directly in src/ too.
        (["src/*/"], {"src/a/b.ts", "src/tests/t.py"}),
        (["src/**/"], {"src/a.ts", "src/c.md", "src/a/b.ts", "src/tests/t.py"}),
        (["src/a"], set()),
        # Accepted bare-directory policy (unchanged, must keep working):
        # a bare token with NO slash/dot/glob metachar (ambiguous -- could
        # be a directory name or an extension-less filename) expands to
        # ["**/NAME", "**/NAME/**"], matching the name at ANY depth AND
        # its contents -- so both tests/ (root) and src/tests/ (nested)
        # qualify, not just the root-level one.
        (["tests"], {"tests/unit_test.py", "src/tests/t.py"}),
        # "docs" (no trailing slash) -- lib/docs/, pkg/docs/, site/docs/
        # all qualify, not just a root-level docs/ directory.
        (["docs"], _ALL_DOCS_CONTENTS),
        # "docs/" (explicit trailing slash) matches contents only -- same
        # result here since no file anywhere is literally named "docs".
        (["docs/"], _ALL_DOCS_CONTENTS),
    ],
)
async def test_n1_include_pattern_parity_directory_vs_contents(
    tmp_path, include_patterns, expected_files
):
    """Every row verified against real ripgrep -- walk, indexed (trigram
    candidate-file selector), and grep-fallback must all agree with it."""
    source_repo = _build_repo_n1(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    grep_repo = tmp_path / "grep-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    shutil.copytree(source_repo, grep_repo)
    _index(indexed_repo)

    walk_files = await _matching_files(walk_repo, include_patterns, None)
    indexed_files = await _matching_files(indexed_repo, include_patterns, None)

    grep_service = RegexSearchService(grep_repo)
    grep_service._search_engine = "grep"
    grep_result = await grep_service.search(
        "GlobParityNeedle", include_patterns=include_patterns, max_results=1000
    )
    grep_files = {match.file_path for match in grep_result.matches}

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for {include_patterns}: got {walk_files}, "
        f"expected {expected_files}"
    )
    assert indexed_files == expected_files, (
        f"indexed (trigram candidate selector) mismatch for {include_patterns}: "
        f"got {indexed_files}, expected {expected_files}"
    )
    assert grep_files == expected_files, (
        f"grep-fallback mismatch for {include_patterns}: got {grep_files}, "
        f"expected {expected_files}"
    )


# ---------------------------------------------------------------------------
# Bug #1876 round-5 finding F2: a multi-segment trailing-slash directory
# marker (e.g. "src/main/") must anchor at the repository ROOT, exactly like
# gitignore/ripgrep/HEAD -- never "**/" any-depth. Reproduced with a
# multi-module Maven-style layout where "src/main" also exists nested under
# a submodule, so an any-depth rewrite would (incorrectly) also match it.
# ---------------------------------------------------------------------------


def _build_repo_f2_maven(tmp_path):
    repo = tmp_path / "maven-repo"
    files = {
        "src/main/App.java": "GlobParityNeedle root module main\n",
        "modA/src/main/B.java": "GlobParityNeedle submodule main\n",
        "modA/src/test/C.java": "GlobParityNeedle submodule test\n",
        "lib/src/tests/foo.py": "GlobParityNeedle nested tests dir\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
    return repo


async def test_f2_multi_module_maven_layout_anchors_trailing_slash_at_root(tmp_path):
    """Verified against real ripgrep 14.1.1: `rg -g '!src/main'
    -g '!src/main/**'` excludes src/main/App.java but KEEPS
    modA/src/main/B.java; `rg -g 'src/tests' -g 'src/tests/**'` returns
    nothing (no root-level src/tests directory exists in this fixture)."""
    source_repo = _build_repo_f2_maven(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    grep_repo = tmp_path / "grep-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    shutil.copytree(source_repo, grep_repo)
    _index(indexed_repo)

    grep_service = RegexSearchService(grep_repo)
    grep_service._search_engine = "grep"

    # exclude_patterns=["src/main/"] must KEEP modA/src/main/B.java --
    # root-anchored, not any-depth -- while excluding src/main/App.java.
    expected_exclude = {
        "modA/src/main/B.java",
        "modA/src/test/C.java",
        "lib/src/tests/foo.py",
    }
    walk_files = await _matching_files(walk_repo, None, ["src/main/"])
    indexed_files = await _matching_files(indexed_repo, None, ["src/main/"])
    grep_result = await grep_service.search(
        "GlobParityNeedle", exclude_patterns=["src/main/"], max_results=1000
    )
    grep_files = {match.file_path for match in grep_result.matches}
    assert walk_files == expected_exclude, (
        f"real ripgrep walk mismatch for exclude src/main/: got {walk_files}, "
        f"expected {expected_exclude}"
    )
    assert indexed_files == expected_exclude
    assert grep_files == expected_exclude

    # include_patterns=["src/tests/"] must NOT return lib/src/tests/foo.py
    # -- root-anchored, no nested "src/tests" under lib/ qualifies.
    walk_files = await _matching_files(walk_repo, ["src/tests/"], None)
    indexed_files = await _matching_files(indexed_repo, ["src/tests/"], None)
    grep_result = await grep_service.search(
        "GlobParityNeedle", include_patterns=["src/tests/"], max_results=1000
    )
    grep_files = {match.file_path for match in grep_result.matches}
    assert walk_files == set(), f"real ripgrep walk mismatch: got {walk_files}"
    assert indexed_files == set()
    assert grep_files == set()


# ---------------------------------------------------------------------------
# Bug #1876 round-5 finding F7: whitespace normalization differed between
# the indexed matcher (which strips surrounding whitespace per pattern via
# PathPatternMatcher._validate_and_expand) and the unindexed ripgrep -g
# argument construction (which passed the raw, unstripped pattern straight
# to normalize_glob_pattern). " *.py " selected 3 files indexed, 0 via real
# ripgrep, before the fix.
# ---------------------------------------------------------------------------


_F7_MAX_RESULTS = 1000


async def _f7_matches_across_engines(walk_repo, indexed_repo, grep_repo, pattern):
    """Run the same include pattern through walk/indexed/grep and return
    their three result sets as a tuple, for direct set-equality asserts."""
    walk_files = await _matching_files(walk_repo, [pattern], None)
    indexed_files = await _matching_files(indexed_repo, [pattern], None)
    grep_service = RegexSearchService(grep_repo)
    grep_service._search_engine = "grep"
    grep_result = await grep_service.search(
        "GlobParityNeedle", include_patterns=[pattern], max_results=_F7_MAX_RESULTS
    )
    grep_files = {match.file_path for match in grep_result.matches}
    return walk_files, indexed_files, grep_files


async def test_f7_whitespace_padded_pattern_matches_identically_across_engines(
    tmp_path,
):
    """A surrounding-whitespace pattern must be stripped identically on
    every engine, producing the SAME result as the already-stripped form
    (proven directly below, not merely against a hardcoded expectation)."""
    source_repo = _build_repo(tmp_path)
    walk_repo, indexed_repo, grep_repo = (
        tmp_path / "walk-repo",
        tmp_path / "indexed-repo",
        tmp_path / "grep-repo",
    )
    for target in (walk_repo, indexed_repo, grep_repo):
        shutil.copytree(source_repo, target)
    _index(indexed_repo)

    expected_files = {
        "README.md",
        "nested/guide.md",
        "docs/guide.md",
        "docs/reference/api.md",
    }
    stripped = await _f7_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, "*.md"
    )
    padded = await _f7_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, " *.md "
    )

    assert stripped == (expected_files, expected_files, expected_files)
    assert padded == stripped, (
        f"whitespace-padded pattern mismatch: got {padded}, expected "
        f"{stripped} (identical to the stripped pattern's own result, "
        f"per-engine: walk, indexed, grep)"
    )


# ---------------------------------------------------------------------------
# Bug #1876 finding F10: a trailing-slash INCLUDE pattern that ALSO carries
# a wildcard (e.g. "*/tests/", "src/*/") previously selected NOTHING on
# every engine, because normalize_glob_pattern emitted only a directory-only
# marker line that CompiledPatternSet._matches_as_include rejects (its
# match relies entirely on pathspec's optional/mandatory trailing-directory
# group). The fix adds an explicit "/**"-suffixed content line alongside
# the marker line. Every row below was verified against REAL ripgrep 14.1.1
# (`rg --files -g <marker> -g <marker>/** .`) before being written here --
# P3 correction: for the "*/tests/" row specifically, "verified against
# real ripgrep" means real ripgrep was invoked on the SAME rewritten globs
# this module's own normalize_glob_pattern already produces
# (`-g '**/tests' -g '**/tests/**'`), not on the literal, unrewritten user
# string "*/tests/". Raw ripgrep `-g '*/tests/' -g '*/tests/**'` (the
# literal glob, no #1211 rewrite applied) matches only files exactly ONE
# wildcard segment above "tests" -- e.g. it misses a root-level
# "tests/t.py" and a two-level-deep "lib/src/tests/f.py" entirely. The
# any-depth result this test suite expects is this module's OWN #1211
# policy choice (leading "*/" -> "**/", applied before the globs ever
# reach ripgrep), not native ripgrep "-g" glob semantics for that literal
# pattern text.
# ---------------------------------------------------------------------------


def _build_repo_f10(tmp_path):
    repo = tmp_path / "f10-repo"
    files = {
        "src/a.py": "GlobParityNeedle direct child of src\n",
        "src/sub/x.py": "GlobParityNeedle one level under src/sub\n",
        "src/sub/deep/y.py": "GlobParityNeedle two levels under src/sub\n",
        "tests/t.py": "GlobParityNeedle root-level tests dir\n",
        "a/tests/t.py": "GlobParityNeedle nested tests dir under a\n",
        "src/tests/u.py": "GlobParityNeedle nested tests dir under src\n",
        "docs/r.md": "GlobParityNeedle root-level docs dir\n",
        "lib/src/tests/f.py": "GlobParityNeedle deeply nested tests dir\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return repo


def _f10_setup_engines(tmp_path):
    """Build the F10 fixture into three identical repos (walk/indexed/
    grep) and return (walk_repo, indexed_repo, grep_repo)."""
    source_repo = _build_repo_f10(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    grep_repo = tmp_path / "grep-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    shutil.copytree(source_repo, grep_repo)
    _index(indexed_repo)
    return walk_repo, indexed_repo, grep_repo


async def _f10_matches_across_engines(
    walk_repo, indexed_repo, grep_repo, include_patterns=None, exclude_patterns=None
):
    """Run the same include/exclude patterns through walk/indexed/grep
    and return their three result sets as a tuple (mirrors
    ``_f7_matches_across_engines`` above)."""
    walk_files = await _matching_files(walk_repo, include_patterns, exclude_patterns)
    indexed_files = await _matching_files(
        indexed_repo, include_patterns, exclude_patterns
    )
    grep_service = RegexSearchService(grep_repo)
    grep_service._search_engine = "grep"
    grep_result = await grep_service.search(
        "GlobParityNeedle",
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        max_results=1000,
    )
    grep_files = {match.file_path for match in grep_result.matches}
    return walk_files, indexed_files, grep_files


_F10_ALL_FILES = {
    "src/a.py",
    "src/sub/x.py",
    "src/sub/deep/y.py",
    "tests/t.py",
    "a/tests/t.py",
    "src/tests/u.py",
    "docs/r.md",
    "lib/src/tests/f.py",
}

# "*/tests/" -- any-depth prefix + literal "tests" + trailing slash
# (contents only) -- identical to bare "tests/": every "tests" directory
# at any depth qualifies, never a file that is merely a direct child of
# "src" (src/a.py) or of a "tests" sibling.
_F10_STAR_SLASH_TESTS_CONTENTS = {
    "tests/t.py",
    "a/tests/t.py",
    "src/tests/u.py",
    "lib/src/tests/f.py",
}

# "src/*/" -- files under any DIRECT subdirectory of "src", at any depth
# below that subdirectory -- never "src/a.py" (a direct child of "src"
# itself, not of a subdirectory of "src").
_F10_SRC_STAR_SLASH_CONTENTS = {"src/sub/x.py", "src/sub/deep/y.py", "src/tests/u.py"}


@pytest.mark.parametrize(
    ("include_patterns", "expected_files"),
    [
        (["*/tests/"], _F10_STAR_SLASH_TESTS_CONTENTS),
        (["src/*/"], _F10_SRC_STAR_SLASH_CONTENTS),
    ],
)
async def test_f10_wildcard_trailing_slash_include_selects_directory_contents(
    tmp_path, include_patterns, expected_files
):
    """Every row verified against real ripgrep -- walk, indexed (trigram
    candidate-file selector), and grep-fallback must all agree with it."""
    walk_repo, indexed_repo, grep_repo = _f10_setup_engines(tmp_path)
    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, include_patterns=include_patterns
    )

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for {include_patterns}: got {walk_files}, "
        f"expected {expected_files}"
    )
    assert indexed_files == expected_files, (
        f"indexed (trigram candidate selector) mismatch for {include_patterns}: "
        f"got {indexed_files}, expected {expected_files}"
    )
    assert grep_files == expected_files, (
        f"grep-fallback mismatch for {include_patterns}: got {grep_files}, "
        f"expected {expected_files}"
    )


@pytest.mark.parametrize(
    ("exclude_patterns", "excluded_contents"),
    [
        (["*/tests/"], _F10_STAR_SLASH_TESTS_CONTENTS),
        (["src/*/"], _F10_SRC_STAR_SLASH_CONTENTS),
    ],
)
async def test_f10_wildcard_trailing_slash_exclude_semantics_unchanged(
    tmp_path, exclude_patterns, excluded_contents
):
    """EXCLUDE behavior for these two patterns is unaffected by the F10
    fix, but the reason differs per pattern -- P3 correction: the
    original docstring here overstated both as identically "already
    excluded contents correctly before the fix" via a "strict subset"
    relationship, which is not accurate for either pattern taken at
    face value:

    - "src/*/" is the pattern F10's own wildcard-bearing-branch fix
      actually touches. Verified directly against pathspec: its
      directory-only marker line ("src/*/") and the added "/**" content
      line ("src/*/**") match the EXACT SAME set of real file paths --
      equal sets, not a strict/proper subset -- because the marker
      line's own optional trailing-directory group already requires the
      same "one more path segment beyond the wildcard" structure the
      content line spells out explicitly. So EXCLUDE was unaffected
      simply because the content line adds nothing new to exclude.
    - "*/tests/" is NOT handled by the branch F10 touched at all -- it
      is classified by an earlier, unrelated branch (the literal-
      remainder "*/X/" case), which already emitted a two-line
      ["**/tests", "**/tests/**"] form before F10 existed. Here the
      content line genuinely IS a strict/proper subset of the marker
      line (the marker additionally matches a bare "tests"/"a/tests"
      path with no further nesting, which the content line does not) --
      but since it is F10-unrelated, "EXCLUDE was unaffected BY THE F10
      FIX" is trivially true for this pattern (there was no F10-era
      change to be affected by), not because both patterns share one
      mechanism.
    """
    walk_repo, indexed_repo, grep_repo = _f10_setup_engines(tmp_path)
    expected_files = _F10_ALL_FILES - excluded_contents

    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, exclude_patterns=exclude_patterns
    )

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for exclude {exclude_patterns}: "
        f"got {walk_files}, expected {expected_files}"
    )
    assert indexed_files == expected_files
    assert grep_files == expected_files


# ---------------------------------------------------------------------------
# Bug #1876 finding F11 (P1 regression vs HEAD, introduced by F10): a
# leading "*/" whose remainder STILL carries a wildcard after the trailing
# slash is stripped (e.g. "*/tests/*/", "*/node_modules/*/", "*/src/*/",
# "*/test*/") previously fell through unrewritten -- matching only exactly
# one segment below the repository root instead of any depth. HEAD
# (pre-F10) applied bug #1211's any-depth "*/" -> "**/" rewrite
# unconditionally regardless of the remainder's shape. Fixed by applying
# the identical rewrite on both the marker and content lines. Every row
# below was verified against real ripgrep 14.1.1 on the REWRITTEN globs
# this module's normalize_glob_pattern produces (the leading "*/" -> "**/"
# any-depth rewrite is this module's own #1211 policy, not native ripgrep
# "-g" glob semantics for the literal, unrewritten pattern text).
# ---------------------------------------------------------------------------


def _build_repo_f11(tmp_path):
    repo = tmp_path / "f11-repo"
    files = {
        "tests/x/y.py": "GlobParityNeedle root tests one nested level\n",
        "a/b/tests/x/y.py": "GlobParityNeedle deeply nested tests dir\n",
        "tests/t.py": "GlobParityNeedle direct child of tests (no marker)\n",
        "node_modules/pkg/index.js": "GlobParityNeedle root node_modules\n",
        "web/app/node_modules/pkg/index.js": "GlobParityNeedle nested node_modules\n",
        "src/sub/q.py": "GlobParityNeedle one level under src/sub\n",
        "src/q.py": "GlobParityNeedle direct child of src (no marker)\n",
        "test_x/f.py": "GlobParityNeedle test_x wildcard segment match\n",
        "docs/r.md": "GlobParityNeedle unrelated control file\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return repo


def _f11_setup_engines(tmp_path):
    """Build the F11 fixture into three identical repos (walk/indexed/
    grep) and return (walk_repo, indexed_repo, grep_repo)."""
    source_repo = _build_repo_f11(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    grep_repo = tmp_path / "grep-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    shutil.copytree(source_repo, grep_repo)
    _index(indexed_repo)
    return walk_repo, indexed_repo, grep_repo


@pytest.mark.parametrize(
    ("include_patterns", "expected_files"),
    [
        (["*/tests/*/"], {"tests/x/y.py", "a/b/tests/x/y.py"}),
        (
            ["*/node_modules/*/"],
            {"node_modules/pkg/index.js", "web/app/node_modules/pkg/index.js"},
        ),
        (["*/src/*/"], {"src/sub/q.py"}),
        # "*/test*/" also sweeps in "tests/x/y.py", "a/b/tests/x/y.py" and
        # "tests/t.py" -- "test*" matches the "tests" segment too (any
        # suffix chars after "test", including "s"), and each of these
        # paths has at least one more path component beneath that
        # matched segment (verified directly against real ripgrep).
        (
            ["*/test*/"],
            {"tests/x/y.py", "a/b/tests/x/y.py", "tests/t.py", "test_x/f.py"},
        ),
    ],
)
async def test_f11_wildcard_trailing_slash_include_matches_any_depth(
    tmp_path, include_patterns, expected_files
):
    """Every row verified against real ripgrep -- walk, indexed (trigram
    candidate-file selector), and grep-fallback must all agree with it."""
    walk_repo, indexed_repo, grep_repo = _f11_setup_engines(tmp_path)
    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, include_patterns=include_patterns
    )

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for {include_patterns}: got {walk_files}, "
        f"expected {expected_files}"
    )
    assert indexed_files == expected_files, (
        f"indexed (trigram candidate-file selector) mismatch for "
        f"{include_patterns}: got {indexed_files}, expected {expected_files}"
    )
    assert grep_files == expected_files, (
        f"grep-fallback mismatch for {include_patterns}: got {grep_files}, "
        f"expected {expected_files}"
    )


async def test_f11_wildcard_trailing_slash_exclude_matches_any_depth(tmp_path):
    """EXCLUDE must reject exactly the paths INCLUDE accepts for the same
    rewritten any-depth pattern -- verified across all three engines."""
    walk_repo, indexed_repo, grep_repo = _f11_setup_engines(tmp_path)
    all_files = {
        "tests/x/y.py",
        "a/b/tests/x/y.py",
        "tests/t.py",
        "node_modules/pkg/index.js",
        "web/app/node_modules/pkg/index.js",
        "src/sub/q.py",
        "src/q.py",
        "test_x/f.py",
        "docs/r.md",
    }
    excluded = {"tests/x/y.py", "a/b/tests/x/y.py"}
    expected_files = all_files - excluded

    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, exclude_patterns=["*/tests/*/"]
    )

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for exclude '*/tests/*/': got "
        f"{walk_files}, expected {expected_files}"
    )
    assert indexed_files == expected_files
    assert grep_files == expected_files


# ---------------------------------------------------------------------------
# Bug #1876 finding F12: a SINGLE-segment wildcard trailing-slash marker
# (e.g. "tests*/", "build-*/", "*.d/") is NOT root-anchored as a
# directory-only marker line (gitignore's trailing-only-slash convention
# already matches at any depth on its own) -- but the naive
# "stripped + '/**'" content line DOES contain an internal "/", which
# gitignore/pathspec anchors at the repository root, silently dropping
# nested matches for INCLUDE purposes. Fixed by prefixing the content line
# with "**/" so it also matches at any depth.
# ---------------------------------------------------------------------------


def _build_repo_f12(tmp_path):
    repo = tmp_path / "f12-repo"
    files = {
        "tests/t.py": "GlobParityNeedle root tests dir\n",
        "tests_unit/a.py": "GlobParityNeedle root tests_unit variant\n",
        "a/tests/x.py": "GlobParityNeedle nested tests dir\n",
        "src/tests/y.py": "GlobParityNeedle nested tests dir under src\n",
        "lib/src/tests/z.py": "GlobParityNeedle deeply nested tests dir\n",
        "lib/tests_unit/w.py": "GlobParityNeedle nested tests_unit variant\n",
        "src/other.py": "GlobParityNeedle unrelated control file\n",
    }
    for relative_path, content in files.items():
        file_path = repo / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return repo


def _f12_setup_engines(tmp_path):
    """Build the F12 fixture into three identical repos (walk/indexed/
    grep) and return (walk_repo, indexed_repo, grep_repo)."""
    source_repo = _build_repo_f12(tmp_path)
    walk_repo = tmp_path / "walk-repo"
    indexed_repo = tmp_path / "indexed-repo"
    grep_repo = tmp_path / "grep-repo"
    shutil.copytree(source_repo, walk_repo)
    shutil.copytree(source_repo, indexed_repo)
    shutil.copytree(source_repo, grep_repo)
    _index(indexed_repo)
    return walk_repo, indexed_repo, grep_repo


_F12_TESTS_STAR_CONTENTS = {
    "tests/t.py",
    "tests_unit/a.py",
    "a/tests/x.py",
    "src/tests/y.py",
    "lib/src/tests/z.py",
    "lib/tests_unit/w.py",
}


async def test_f12_single_segment_wildcard_trailing_slash_include_any_depth(
    tmp_path,
):
    """Every row verified against real ripgrep -- walk, indexed (trigram
    candidate-file selector), and grep-fallback must all agree with it."""
    walk_repo, indexed_repo, grep_repo = _f12_setup_engines(tmp_path)
    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, include_patterns=["tests*/"]
    )

    assert walk_files == _F12_TESTS_STAR_CONTENTS, (
        f"real ripgrep walk mismatch for include 'tests*/': got {walk_files}, "
        f"expected {_F12_TESTS_STAR_CONTENTS}"
    )
    assert indexed_files == _F12_TESTS_STAR_CONTENTS, (
        f"indexed (trigram candidate-file selector) mismatch: got "
        f"{indexed_files}, expected {_F12_TESTS_STAR_CONTENTS}"
    )
    assert grep_files == _F12_TESTS_STAR_CONTENTS, (
        f"grep-fallback mismatch: got {grep_files}, expected {_F12_TESTS_STAR_CONTENTS}"
    )


async def test_f12_single_segment_wildcard_trailing_slash_exclude_any_depth(
    tmp_path,
):
    """EXCLUDE already matched at any depth via the marker line alone
    (never root-anchored) -- confirm the F12 content-line fix leaves that
    unaffected, across all three engines."""
    walk_repo, indexed_repo, grep_repo = _f12_setup_engines(tmp_path)
    all_files = _F12_TESTS_STAR_CONTENTS | {"src/other.py"}
    expected_files = all_files - _F12_TESTS_STAR_CONTENTS

    walk_files, indexed_files, grep_files = await _f10_matches_across_engines(
        walk_repo, indexed_repo, grep_repo, exclude_patterns=["tests*/"]
    )

    assert walk_files == expected_files, (
        f"real ripgrep walk mismatch for exclude 'tests*/': got "
        f"{walk_files}, expected {expected_files}"
    )
    assert indexed_files == expected_files
    assert grep_files == expected_files
