"""
TDD driver for #1876 rework, item-3/6 wiring step.

- `normalize_glob_pattern`: the ONE canonical normalization function applied
  before BOTH the indexed matcher and the unindexed `rg -g` command line
  (item 3) -- leading `./` stripped, leading `*/` rewritten to `**/` (bug
  #1211), and a bare directory-like token (`docs`, `docs/`, `tests`)
  expanded into the pinned "safest consistent" recursive-selector form.
- `create_selector`/`PathSelector`: deferred from the matcher-foundation
  turn because building them unconsumed would have been orphan code: this
  step wires them into the 4 duplicated call sites, so they're built here
  alongside that consumption.
"""

import pytest

from code_indexer.services.path_pattern_matcher import (
    InvalidPatternError,
    PathPatternMatcher,
    normalize_glob_pattern,
)


@pytest.fixture()
def matcher() -> PathPatternMatcher:
    return PathPatternMatcher()


# --- normalize_glob_pattern: leading ./ and */ ----------------------------


def test_leading_dot_slash_is_stripped():
    assert normalize_glob_pattern("./src/*.ts") == ["src/*.ts"]


def test_leading_star_slash_rewritten_to_doublestar_slash():
    assert normalize_glob_pattern("*/tests/*") == ["**/tests/*"]


def test_already_doublestar_prefixed_pattern_is_unchanged():
    assert normalize_glob_pattern("**/tests/**") == ["**/tests/**"]


def test_plain_filename_pattern_with_extension_is_unchanged():
    # Has a dot -> looks like a real filename, not a bare directory token.
    assert normalize_glob_pattern("*.min.js") == ["*.min.js"]
    assert normalize_glob_pattern("README.md") == ["README.md"]


# --- normalize_glob_pattern: bare directory-like token (item 3 policy) ---


def test_bare_token_no_slash_no_dot_expands_to_name_and_contents():
    # Pinned "safest consistent" policy: a bare token with no slash, no dot,
    # and no glob metacharacters could be a directory name (`docs`) or an
    # extension-less filename (`Makefile`) -- we cannot tell without a
    # filesystem, so we match BOTH: the name itself at any depth, and
    # everything recursively under it if it turns out to be a directory.
    assert normalize_glob_pattern("docs") == ["**/docs", "**/docs/**"]


def test_trailing_slash_single_segment_matches_bare_token_policy():
    # Bug #1876 round-5 finding F1: HEAD/pathspec's own "non-'**'-
    # terminated segment is directory-or-file-agnostic" convention means
    # a bare directory NAME also matches the directory path itself, not
    # merely contents beneath it -- so a trailing-slash single-segment
    # marker ("docs/") must normalize identically to the bare token
    # ("docs") policy, not to a contents-only single line.
    assert normalize_glob_pattern("docs/") == ["**/docs", "**/docs/**"]


def test_trailing_slash_multi_segment_anchors_at_repo_root():
    # Bug #1876 round-5 finding F2: a MULTI-segment trailing-slash marker
    # (e.g. "src/main/") must anchor at the repository root exactly like
    # gitignore/ripgrep/HEAD -- never "**/" any-depth. Verified against
    # real ripgrep 14.1.1: `rg -g '!src/main' -g '!src/main/**'` excludes
    # src/main/App.java but keeps modA/src/main/B.java.
    assert normalize_glob_pattern("src/main/") == ["src/main", "src/main/**"]
    assert normalize_glob_pattern("src/tests/") == ["src/tests", "src/tests/**"]


def test_trailing_slash_with_leading_star_slash_drops_bogus_directory_marker():
    # Bug #1876 round-5 finding F1 original repro: "*/tests/" (leading
    # */ any-depth rewrite combined with a trailing slash) previously
    # preserved the trailing slash into the rewritten "**/tests/" line --
    # a directory-only gitwildmatch marker that can never match a path
    # without ITS OWN trailing slash, which _normalize_path always strips
    # from a real file path. The trailing slash carries no meaning here
    # (it is not a literal directory name, just the leading-"*/" any-
    # depth prefix's own delimiter) and must be dropped, matching HEAD's
    # unconditional "strip a pattern's trailing slash" normalization.
    #
    # Bug #1876 F10 correction: the single "**/tests" line alone is not
    # enough -- CompiledPatternSet._matches_as_include (used by
    # create_selector's INCLUDE set) rejects a match that relies only on
    # pathspec's optional trailing-directory group, so "**/tests" alone
    # selected NOTHING as an include (verified: real create_selector over
    # tests/t.py, a/tests/t.py, src/tests/u.py, lib/src/tests/f.py
    # returned an empty set). "*/tests/" must behave identically to bare
    # "tests/" (any depth + directory contents), matching the
    # single-segment trailing-slash policy's own two-line form -- and
    # confirmed against real ripgrep 14.1.1: `rg --files -g '**/tests'
    # -g '**/tests/**'` returns exactly those four files.
    assert normalize_glob_pattern("*/tests/") == ["**/tests", "**/tests/**"]


def test_trailing_slash_bare_wildcard_collapses_to_wildcard_alone():
    # Bug #1876 round-5 finding F1: "*/" and "**/" alone are degenerate
    # (no literal directory name survives once the trailing slash is
    # stripped) -- HEAD strips the trailing slash unconditionally before
    # any other normalization, so these collapse to the bare wildcard.
    assert normalize_glob_pattern("*/") == ["*"]
    assert normalize_glob_pattern("**/") == ["**"]


def test_trailing_slash_with_other_wildcard_keeps_directory_only_marker():
    # A wildcard segment that is NOT a literal directory name and not a
    # resolvable leading "*/" prefix (e.g. "src/*/", "src/**/") is a
    # deliberate ripgrep directory-only marker -- real ripgrep -g never
    # lets such a pattern match a file, so the directory-only marker line
    # must be preserved exactly as before (EXCLUDE/matches_pattern
    # parity: unaffected by Bug #1876 F10, verified already correct).
    #
    # Bug #1876 F10: the marker line ALONE is not enough for INCLUDE
    # purposes -- CompiledPatternSet._matches_as_include rejects a match
    # that only succeeds via pathspec's trailing-directory group, so
    # "src/*/" alone selected NOTHING as an include (verified: real
    # create_selector over src/a.py, src/sub/x.py, src/sub/deep/y.py,
    # src/tests/u.py returned an empty set, though ripgrep's own -g
    # semantics -- confirmed live with ripgrep 14.1.1 -- put
    # src/sub/x.py, src/sub/deep/y.py, src/tests/u.py in scope). A
    # second, explicit "/**"-suffixed content line closes that gap.
    assert normalize_glob_pattern("src/*/") == ["src/*/", "src/*/**"]
    assert normalize_glob_pattern("src/**/") == ["src/**/", "src/**/**"]


# --- Bug #1876 F10: wildcard-bearing trailing-slash INCLUDE selects ------
# --- the matched directories' contents (create_selector / CompiledPat-  --
# --- ternSet, not merely normalize_glob_pattern's raw line output)      --


def test_f10_leading_star_slash_trailing_slash_include_selects_contents(matcher):
    # "*/tests/" as an INCLUDE pattern must behave exactly like bare
    # "tests/" -- any depth, directory contents -- not select nothing.
    selector = matcher.create_selector(["*/tests/"], None)
    assert selector.select("tests/t.py") is True
    assert selector.select("a/tests/t.py") is True
    assert selector.select("src/tests/u.py") is True
    assert selector.select("lib/src/tests/f.py") is True
    # A file directly in "src" (not under a "tests" directory) must not
    # be swept in.
    assert selector.select("src/a.py") is False


def test_f10_literal_prefix_wildcard_segment_trailing_slash_include_selects_contents(
    matcher,
):
    # "src/*/" as an INCLUDE pattern must select files under any DIRECT
    # subdirectory of "src", at any depth below that subdirectory --
    # never "src/a.py" itself (a direct child of "src", not of a
    # subdirectory of "src"). Verified against real ripgrep 14.1.1:
    # `rg --files -g 'src/*/' -g 'src/*/**'` returns exactly this set.
    selector = matcher.create_selector(["src/*/"], None)
    assert selector.select("src/sub/x.py") is True
    assert selector.select("src/sub/deep/y.py") is True
    assert selector.select("src/tests/u.py") is True
    assert selector.select("src/a.py") is False
    assert selector.select("tests/t.py") is False


def test_f10_exclude_semantics_unchanged_for_wildcard_trailing_slash(matcher):
    # The companion "/**" content line added for INCLUDE parity must not
    # change EXCLUDE behavior at all -- it is a strict subset of what the
    # directory-only marker line already excludes (both already correct
    # per the F10 investigation).
    selector = matcher.create_selector(None, ["src/*/"])
    assert selector.select("src/sub/x.py") is False
    assert selector.select("src/a.py") is True

    selector = matcher.create_selector(None, ["*/tests/"])
    assert selector.select("src/tests/u.py") is False
    assert selector.select("src/a.py") is True


# --- Bug #1876 F11: leading "*/" not rewritten when the remainder (after --
# --- stripping the trailing "/") still carries a wildcard ------------------


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("*/tests/*/", ["**/tests/*/", "**/tests/*/**"]),
        ("*/test*/", ["**/test*/", "**/test*/**"]),
        ("*/tests/**/", ["**/tests/**/", "**/tests/**/**"]),
        ("*/node_modules/*/", ["**/node_modules/*/", "**/node_modules/*/**"]),
        ("*/src/*/", ["**/src/*/", "**/src/*/**"]),
    ],
)
def test_f11_leading_star_slash_rewritten_when_remainder_has_wildcard(
    pattern, expected
):
    # Bug #1876 F11 (P1 regression vs HEAD, introduced by F10): a leading
    # "*/" whose remainder still carries a wildcard after the trailing
    # slash is stripped previously fell through unrewritten -- matching
    # only exactly one segment below the repository root instead of any
    # depth. The leading "*/" -> "**/" rewrite (bug #1211) must apply on
    # BOTH the directory-only marker line and the explicit "/**" content
    # line, just like it already does for the pure-literal-remainder case
    # ("*/tests/" -> ["**/tests", "**/tests/**"]).
    assert normalize_glob_pattern(pattern) == expected


def test_f11_matches_pattern_any_depth_parity_with_head(matcher):
    # These exact (path, pattern) pairs are the F11 investigation's own
    # HEAD-parity repro: HEAD (strip slash + bug #1211 any-depth "*/" ->
    # "**/" rewrite, applied unconditionally regardless of what followed)
    # matched all of these; F10 regressed every one of them to False.
    assert matcher.matches_pattern("tests/x/y.py", "*/tests/*/") is True
    assert matcher.matches_pattern("a/b/tests/x/y.py", "*/tests/*/") is True
    assert matcher.matches_pattern("node_modules/p/i.js", "*/node_modules/*/") is True
    assert (
        matcher.matches_pattern("web/app/node_modules/p/i.js", "*/node_modules/*/")
        is True
    )
    assert matcher.matches_pattern("src/sub/q.py", "*/src/*/") is True
    assert matcher.matches_pattern("test_x/f.py", "*/test*/") is True
    # Control: a path with no "tests" segment at all must still not match.
    assert matcher.matches_pattern("src/module.py", "*/tests/*/") is False


def test_f11_create_selector_include_selects_any_depth_contents(matcher):
    selector = matcher.create_selector(["*/tests/*/"], None)
    assert selector.select("tests/x/y.py") is True
    assert selector.select("a/b/tests/x/y.py") is True
    # Per the established F10 directory-only-marker policy (unaffected by
    # F11), a file directly IN "tests" (no extra nesting level) is not
    # selected -- the marker still requires a genuine directory boundary
    # after the wildcard segment.
    assert selector.select("tests/t.py") is False
    assert selector.select("src/module.py") is False

    selector = matcher.create_selector(["*/node_modules/*/"], None)
    assert selector.select("node_modules/p/i.js") is True
    assert selector.select("web/app/node_modules/p/i.js") is True
    assert selector.select("src/module.py") is False


def test_f11_create_selector_exclude_matches_pattern_parity(matcher):
    # EXCLUDE must reject exactly the paths matches_pattern/INCLUDE accept
    # for the same rewritten any-depth pattern.
    selector = matcher.create_selector(None, ["*/tests/*/"])
    assert selector.select("tests/x/y.py") is False
    assert selector.select("a/b/tests/x/y.py") is False
    assert selector.select("src/module.py") is True


# --- Bug #1876 F12: single-segment wildcard trailing-slash INCLUDE marker --
# --- ("tests*/", "build-*/", "*.d/") was root-anchored for its content --
# --- line, silently dropping nested matches -------------------------------


def test_f12_single_segment_wildcard_trailing_slash_content_line_any_depth():
    # The content line must gain the same "**/" any-depth prefix the
    # literal single-segment trailing-slash policy ("docs/") already uses
    # -- the marker line ("tests*/") is untouched (it was never anchored:
    # gitignore's trailing-only-slash convention already matches at any
    # depth on its own).
    assert normalize_glob_pattern("tests*/") == ["tests*/", "**/tests*/**"]
    assert normalize_glob_pattern("build-*/") == ["build-*/", "**/build-*/**"]
    assert normalize_glob_pattern("*.d/") == ["*.d/", "**/*.d/**"]


def test_f12_create_selector_include_selects_nested_matches(matcher):
    selector = matcher.create_selector(["tests*/"], None)
    assert selector.select("tests/t.py") is True
    assert selector.select("tests_unit/a.py") is True
    assert selector.select("a/tests/x.py") is True
    assert selector.select("src/tests/y.py") is True
    assert selector.select("lib/src/tests/z.py") is True
    assert selector.select("lib/tests_unit/w.py") is True
    assert selector.select("src/other.py") is False


def test_f12_create_selector_exclude_still_removes_nested_matches(matcher):
    # EXCLUDE already worked at any depth via the marker line alone (the
    # marker was never root-anchored) -- confirm the F12 content-line fix
    # doesn't change that.
    selector = matcher.create_selector(None, ["tests*/"])
    assert selector.select("a/tests/x.py") is False
    assert selector.select("lib/src/tests/z.py") is False
    assert selector.select("src/other.py") is True


def test_bare_token_with_glob_metacharacter_is_not_treated_as_directory():
    assert normalize_glob_pattern("*.md") == ["*.md"]
    assert normalize_glob_pattern("test?.py") == ["test?.py"]


def test_bare_token_with_internal_slash_is_not_directory_expanded():
    assert normalize_glob_pattern("src/tests") == ["src/tests"]


# --- normalize_glob_pattern: does not touch brace groups ------------------


def test_brace_groups_pass_through_unexpanded():
    # Ripgrep expands braces itself on the unindexed `-g` path; the matcher
    # expands them separately (_expand_braces) on the indexed path. This
    # function must not double-handle them.
    assert normalize_glob_pattern("*.{ts,md}") == ["*.{ts,md}"]


# --- create_selector / PathSelector (item 5/6) ----------------------------


def test_selector_include_only(matcher):
    selector = matcher.create_selector(["*.py"], None)
    assert selector.select("src/foo.py") is True
    assert selector.select("src/foo.md") is False


def test_selector_exclude_only(matcher):
    selector = matcher.create_selector(None, ["*.md"])
    assert selector.select("src/foo.py") is True
    assert selector.select("src/foo.md") is False


def test_selector_include_and_exclude(matcher):
    selector = matcher.create_selector(["*.{ts,md}"], ["*.min.*"])
    assert selector.select("app.ts") is True
    assert selector.select("app.min.ts") is False
    assert selector.select("app.json") is False


def test_selector_no_patterns_selects_everything(matcher):
    selector = matcher.create_selector(None, None)
    assert selector.select("anything/at/all.xyz") is True


def test_selector_invalid_include_pattern_raises_at_creation(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.create_selector(["!*.md"], None)


def test_selector_honours_bare_directory_policy(matcher):
    # The selector must apply the SAME normalize_glob_pattern policy the
    # matcher's own compile_patterns path already applies via
    # _validate_and_expand -- i.e. bare `docs` selects its contents.
    selector = matcher.create_selector(["docs"], None)
    assert selector.select("docs/guide.md") is True
    assert selector.select("src/other.md") is False


def test_compile_patterns_trailing_slash_directory_marker_matches_bare_token(matcher):
    # Bug #1876 round-5 finding F1 (P1 regression, dual-review verified):
    # the round-4 "docs/" contents-only single line silently diverged
    # from HEAD, which never distinguished a trailing-slash pattern from
    # its bare-token form at all (HEAD unconditionally strips a
    # pattern's trailing slash before any other normalization). A
    # metacharacter-free trailing-slash directory marker must now
    # normalize identically to the bare token: matches the name itself
    # at any depth AND everything recursively under it.
    #
    # _validate_and_expand normalizes the RAW pattern through
    # _normalize_path before it ever reaches normalize_glob_pattern;
    # that must not lose the trailing "/" before normalize_glob_pattern
    # sees it, so this indexed-matcher call path stays in agreement with
    # the unindexed rg -g path (regex_search.py calls
    # normalize_glob_pattern directly on the untouched raw pattern).
    compiled = matcher.compile_patterns(["docs/"])
    lines = [p.regex.pattern for p in compiled._spec.patterns if p.regex]
    bare_compiled = matcher.compile_patterns(["docs"])
    bare_lines = [p.regex.pattern for p in bare_compiled._spec.patterns if p.regex]
    assert lines == bare_lines, (
        f"compile_patterns(['docs/']) must produce the SAME compiled "
        f"regex lines as the bare token 'docs' (name itself + "
        f"contents), matching normalize_glob_pattern('docs/') == "
        f"['**/docs', '**/docs/**']; got {lines} vs bare {bare_lines}"
    )
    assert len(lines) == 2, f"expected exactly 2 pattern lines; got {lines}"

    selector = matcher.create_selector(["docs/"], None)
    assert selector.select("docs/guide.md") is True
    # A literal FILE (or dir) named exactly "docs" (no extension) IS now
    # selected by the trailing-slash form too, matching HEAD's
    # matches_pattern("docs", "docs/") == True and the bare-token form.
    assert selector.select("docs") is True
    assert selector.select("a/docs") is True

    # The bare form (no trailing slash) behaves identically.
    bare_selector = matcher.create_selector(["docs"], None)
    assert bare_selector.select("docs") is True
    assert bare_selector.select("docs/guide.md") is True


# --- Bug #1876 N4: blank/whitespace-only patterns must fail loud ----------


def test_compile_patterns_rejects_blank_include_pattern(matcher):
    # A blank pattern is a plausible "unfilled field" input, and previously
    # diverged silently: the matcher treated it as "matches nothing" while
    # the unindexed `rg -g ''` include matched EVERY file -- the exact
    # opposite outcomes for the identical caller-supplied pattern.
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns([""])


def test_compile_patterns_rejects_whitespace_only_include_pattern(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns(["   "])


def test_create_selector_rejects_blank_pattern_in_either_list(matcher):
    # `rg -g '!'` (a blank exclude, normalized to a bare negation) produced
    # a different, non-empty result set than the indexed selector's
    # blank-pattern-matches-nothing semantics -- another silent divergence
    # this covers via the exclude side of create_selector.
    with pytest.raises(InvalidPatternError):
        matcher.create_selector([""], None)
    with pytest.raises(InvalidPatternError):
        matcher.create_selector(None, [""])


def test_matches_pattern_single_pattern_api_unaffected_by_blank_rejection(matcher):
    # Bug #1876 N4 scopes the fail-loud rejection to the include/exclude
    # LIST validation path (compile_patterns/create_selector -- the only
    # callers regex_search/xray_search/xray_graph route through). The
    # single-pattern matches_pattern() API is also used by
    # tantivy_index_manager.py and filesystem_vector_store.py, well
    # outside #1876's scope, and its documented "blank pattern matches
    # nothing" contract (tests/unit/cli/test_path_exclusion_edge_cases.py)
    # must keep working unchanged.
    assert matcher.matches_pattern("src/module.py", "") is False
    assert matcher.matches_pattern("src/module.py", "   ") is False


# --- Bug #1876 round-5 finding F6: bare string accepted as pattern list ---


def test_compile_patterns_rejects_bare_string_not_a_list(matcher):
    # A str is itself iterable, so compile_patterns("*.md") previously
    # iterated it character-by-character ('*', '.', 'm', 'd'), each of
    # which passes the existing non-empty-string checks -- silently
    # compiling four single-character glob patterns instead of failing
    # loud on the caller's mistake. regex_search.py's service-layer
    # validation and XRaySearchEngine.run() both route through this
    # exact function, so this single fix closes the hole in both.
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns("*.md")
    # The type check must take priority over the pre-existing falsy
    # short-circuit (an empty string is falsy, but still not a list).
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns("")


def test_create_selector_rejects_bare_string_not_a_list(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.create_selector("*.md", None)
    with pytest.raises(InvalidPatternError):
        matcher.create_selector(None, "*.md")
