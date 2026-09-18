"""
Path pattern matcher for file path exclusions.

Provides glob-style pattern matching with cross-platform support:
- Wildcard patterns (*, **, ?)
- Brace-alternation groups ({a,b}, including nested/multiple groups)
- Character sequences ([seq], [!seq])
- Path normalization (separators, case sensitivity)
- Pattern caching for performance
- Gitignore-style matching via pathspec library
- A compiled selector (`PathPatternMatcher.create_selector`) that builds
  exactly one PathSpec per include/exclude pattern list per query, instead
  of re-normalizing/rebuilding per path (Bug #1876 item 5).
"""

import pathspec
from pathlib import PurePosixPath
from typing import List, Optional

# Bug #1876 N1: pathspec's gitwildmatch grammar compiles every pattern
# whose last segment is not a literal "**" with an OPTIONAL trailing group
# -- "this could be a directory, in which case match everything under it
# too" -- captured under this named group (verified directly against
# pathspec's own GitWildMatchPattern.pattern_to_regex source: every
# non-"**"-terminated segment branch appends
# "(?:(?P<{_DIR_MARK}>/).*)?" when it is the pattern's last segment).
# That is exactly gitignore's own exclude semantics (a directory match
# implies its contents are also excluded) -- correct for EXCLUDE, but not
# what ripgrep's own "-g" INCLUDE semantics do: "*" matches exactly one
# path segment, full stop, and a directory match never implies "and
# everything under it" unless the pattern itself says so (see
# CompiledPatternSet._matches_as_include's docstring for the fix this
# constant enables). Read from pathspec itself via getattr (not hardcoded,
# and not a static `from ... import _DIR_MARK`) so a future pathspec
# release renaming its internal group cannot silently make this check a
# no-op, falling back to the verified literal value if the private
# attribute ever disappears -- and so mypy (which parses pathspec's own
# source under CI's `no_site_packages=false`) never reports attr-defined
# against a pathspec version that lacks the attribute.
import pathspec.patterns.gitwildmatch as _gitwildmatch_module

# Every pattern this module ever compiles goes through
# ``pathspec.PathSpec.from_lines("gitwildmatch", ...)``, so every entry in a
# compiled spec's ``.patterns`` is always a ``GitWildMatchPattern`` in
# practice -- but ``PathSpec.patterns`` is typed as the abstract base
# ``Pattern`` (which has no ``.regex``), so an ``isinstance`` narrowing is
# needed for both mypy and defensive correctness.
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

_PATHSPEC_DIR_MARK: str = getattr(_gitwildmatch_module, "_DIR_MARK", "ps_d")


class InvalidPatternError(ValueError):
    """A glob pattern is malformed or can never produce a match.

    Raised for: non-string pattern items, unbalanced brace groups, and
    gitignore negation/comment syntax (``!x``, ``#x``) that has no
    meaningful effect as a standalone include/exclude pattern (Bug #1876
    item 4) -- callers must surface this as a structured error instead of
    silently skipping the pattern or widening the search.
    """


_MAX_BRACE_EXPANSIONS = 64


def _split_top_level_commas(body: str) -> List[str]:
    """Split ``body`` on commas that are not nested inside a brace group."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in body:
        if ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _expand_braces(pattern: str) -> List[str]:
    """Expand nested/multiple ``{a,b,...}`` groups into concrete alternatives.

    A brace group needs at least one top-level comma to be treated as a
    real alternation -- ``{x}`` with no comma is left untouched as
    literal `{`/`}` characters, and scanning continues past it for a
    genuine group later in the pattern.

    Bug #1876 N6 correction: this does NOT mirror real ripgrep. Verified
    live against ripgrep 14.1.1: ``rg -g '*.{md}'`` matches a file named
    ``x.md`` (ripgrep expands a single-option brace group as an
    alternation of one, equivalent to no braces at all) and does NOT
    require a file literally named ``x.{md}``, whereas this function's
    literal-``{x}`` handling requires exactly that literal text. This
    divergence is a known, deliberate scope boundary (tracked as a
    follow-up alongside Bug #1876's other deferred brace edge cases --
    ``{a,}``, escaped ``\\{a\\}``, nested ``{{a,b}}``, a stray ``}``) --
    not a claim of parity with ripgrep.

    Bounded to ``_MAX_BRACE_EXPANSIONS`` total variants so a pathological
    pattern (many nested groups) cannot blow up compute at ~900-repo scale.

    Raises:
        InvalidPatternError: if a ``{`` has no matching ``}``, or the
            pattern would expand past the bound.
    """
    start = pattern.find("{")
    if start == -1:
        return [pattern]

    depth = 0
    end = -1
    for i in range(start, len(pattern)):
        if pattern[i] == "{":
            depth += 1
        elif pattern[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise InvalidPatternError(f"Unbalanced '{{' in pattern: {pattern!r}")

    prefix = pattern[:start]
    body = pattern[start + 1 : end]
    suffix = pattern[end + 1 :]
    options = _split_top_level_commas(body)

    if len(options) == 1:
        return [
            prefix + "{" + body + "}" + variant for variant in _expand_braces(suffix)
        ]

    results: List[str] = []
    for option in options:
        for variant in _expand_braces(prefix + option + suffix):
            results.append(variant)
            if len(results) > _MAX_BRACE_EXPANSIONS:
                raise InvalidPatternError(
                    f"Pattern expands to more than {_MAX_BRACE_EXPANSIONS} "
                    f"brace alternatives: {pattern!r}"
                )
    return results


_GLOB_METACHARACTERS = set("*?[]{}")


def normalize_glob_pattern(pattern: str) -> List[str]:
    """
    Canonical Bug #1876 item-3 pattern normalization -- the ONE function
    applied before BOTH the indexed matcher (via
    ``PathPatternMatcher._validate_and_expand``) and the unindexed ripgrep
    ``-g`` command line (``regex_search.py``), so the same glob produces
    the same file set on both paths.

    Does NOT touch brace groups (``{a,b}``) -- ripgrep expands those
    itself on the unindexed path, and the matcher's own ``_expand_braces``
    handles them separately on the indexed path.

    Transformations, in order:

    1. A leading ``./`` is stripped (``./src/*.ts`` behaves like
       ``src/*.ts``).
    2. A trailing ``/`` on a metacharacter-free (literal) directory
       marker -- ``docs/`` or ``src/main/`` -- is HEAD-parity (Bug #1876
       round-5 finding F1): HEAD never distinguished a trailing-slash
       pattern from its bare-token form at all (it unconditionally
       stripped a pattern's trailing slash before any other
       normalization), and gitwildmatch's own "a non-``**``-terminated
       segment is directory-or-file-agnostic" convention already means a
       bare directory NAME also matches the directory path itself, not
       merely its contents. So:

       - Single-segment (``docs/``): normalizes identically to the bare
         token policy below -- ``["**/docs", "**/docs/**"]``.
       - Multi-segment (``src/main/``): anchors at the repository root
         exactly like gitignore/ripgrep/HEAD (finding F2 -- never
         ``**/`` any-depth) -- ``["src/main", "src/main/**"]``. The
         explicit ``/**`` line matters only under
         ``compile_patterns(..., is_include=True)``: the bare line's
         content-match relies solely on gitwildmatch's optional
         trailing-directory group, which ``CompiledPatternSet.
         _matches_as_include`` deliberately rejects (matching real
         ripgrep ``-g``'s own INCLUDE semantics) -- the explicit line
         does not rely on that group, so INCLUDE selectors still match
         contents. EXCLUDE/``matches_pattern`` callers are satisfied by
         either line alone.
    3. A "bare token" -- no ``/`` anywhere, no glob metacharacter, no
       ``.`` -- is ambiguous: it could be a directory name (``docs``) or
       an extension-less filename (``Makefile``). The pinned "safest
       consistent" policy (working agreement) matches BOTH: the name
       itself at any depth, and everything recursively under it in case
       it is a directory -- ``["**/NAME", "**/NAME/**"]``.
    4. Otherwise, the pre-existing leading ``*/`` -> ``**/`` rewrite
       (bug #1211: any-depth, not exactly one segment) applies.

    A trailing ``/`` on a pattern that still carries a glob
    metacharacter after the slash is removed is handled narrowly (Bug
    #1876 finding F1, refined by finding F10):

    - A pattern collapsing to pure ``*``/``**`` (i.e. ``*/`` or ``**/``
      alone) becomes the bare wildcard (no literal directory name
      survives to anchor a contents companion line to).
    - A leading ``*/`` whose remainder is itself a pure literal (e.g.
      ``*/tests/``) reduces to exactly the same two-line form the bare
      single/multi-segment trailing-slash policy above produces for that
      remainder (``["**/tests", "**/tests/**"]``) -- the leading ``*/``
      already makes the any-depth intent explicit, so ``*/tests/``
      behaves identically to bare ``tests/``.
    - Any OTHER wildcard-bearing trailing-slash pattern (e.g.
      ``src/*/``, ``src/**/``) is a deliberate ripgrep directory-only
      marker for EXCLUDE purposes -- it must never match a file on its
      own, so that line is preserved unchanged -- but Bug #1876 F10: for
      INCLUDE purposes (``CompiledPatternSet._matches_as_include``,
      ``create_selector``'s include set) that marker line alone selects
      NOTHING, because its only path to matching a file relies on
      pathspec's trailing-directory group, which ``_matches_as_include``
      deliberately rejects (see its docstring). A second, explicit
      ``/**``-suffixed content line is returned alongside it so INCLUDE
      mode can select the matched directories' contents too (verified
      against real ripgrep 14.1.1: ``rg -g 'src/*/' -g 'src/*/**'``
      returns files under any direct subdirectory of ``src``, never
      ``src/a.py`` itself). EXCLUDE/``matches_pattern`` callers are
      unaffected: the added line matches a strict subset of what the
      marker line already excludes.

    Args:
        pattern: A single already-brace-expanded, whitespace-stripped
            glob pattern (forward slashes).

    Returns:
        One or two normalized pattern lines (gitwildmatch/ripgrep ``-g``
        compatible).
    """
    if pattern.startswith("./"):
        pattern = pattern[2:]

    if pattern.endswith("/") and len(pattern) > 1:
        stripped = pattern[:-1]
        if not any(ch in _GLOB_METACHARACTERS for ch in stripped):
            if "/" in stripped:
                # Bug #1876 F2: multi-segment literal directory marker
                # -- anchor at the repository root, never "**/" any-depth.
                return [stripped, stripped + "/**"]
            # Bug #1876 F1: single-segment literal directory marker --
            # identical to the bare-token policy below (rule 3).
            return ["**/" + stripped, "**/" + stripped + "/**"]

        # The stripped pattern still carries a glob metacharacter.  A
        # pure-wildcard collapse ("*/" -> "*", "**/" -> "**") has no
        # literal directory name to anchor a contents line to, so it
        # stays a single line -- matching HEAD's unconditional "strip
        # the trailing slash" normalization. Every other wildcard-
        # bearing shape is a deliberate ripgrep directory-only marker
        # for EXCLUDE purposes (kept exactly as before for that caller),
        # but Bug #1876 F10: for INCLUDE purposes it must ALSO select
        # the matched directories' CONTENTS (consistent with the non-
        # wildcard trailing-slash rules above), which
        # CompiledPatternSet._matches_as_include cannot derive from the
        # directory-only line alone (it deliberately rejects a match
        # that relies only on pathspec's directory-implied group). A
        # second, explicit "/**"-suffixed line closes that gap. EXCLUDE/
        # matches_pattern callers are unaffected: the added line matches
        # a strict subset of what the directory-only line already
        # matches (verified: F10 investigation, zero-flip matrix).
        if stripped and set(stripped) == {"*"}:
            # "*/" -> "*", "**/" -> "**": nothing but the bare wildcard
            # survives once the trailing slash is gone.
            return [stripped]
        if stripped.startswith("*/") and not any(
            ch in _GLOB_METACHARACTERS for ch in stripped[2:]
        ):
            # "*/tests/" (any-depth prefix + literal remainder +
            # trailing slash) reduces to exactly the same two lines the
            # bare single/multi-segment trailing-slash policy above
            # produces for its remainder, since the leading "*/" already
            # makes the any-depth intent explicit -- "*/tests/" behaves
            # identically to bare "tests/".
            remainder = stripped[2:]
            return ["**/" + remainder, "**/" + remainder + "/**"]

        if stripped.startswith("*/"):
            # Bug #1876 F11 (P1 regression vs HEAD, introduced by F10): a
            # leading "*/" whose remainder STILL carries a wildcard after
            # the trailing slash is stripped (e.g. "*/tests/*/",
            # "*/test*/", "*/tests/**/", "*/node_modules/*/",
            # "*/src/*/") fell through to the generic "OTHER wildcard-
            # bearing" branch below unrewritten, so the leading "*/" was
            # never turned into "**/" -- matching only exactly one
            # segment below the repository root instead of any depth.
            # HEAD (pre-F10) applied bug #1211's any-depth "*/" -> "**/"
            # rewrite unconditionally, regardless of what the remainder
            # after the slash looked like, so e.g. "*/tests/*/" matched
            # both "tests/x/y.py" (root) and "a/b/tests/x/y.py" (nested).
            # Apply the identical rewrite here, on BOTH returned lines --
            # the directory-only marker (still trailing-slash-terminated,
            # for EXCLUDE/gitignore-containment parity) and the explicit
            # "/**"-suffixed content line (Bug #1876 F10, INCLUDE parity)
            # -- textually, since the remainder itself may carry
            # additional metacharacters this function does not otherwise
            # inspect.
            rewritten_marker = "**/" + pattern[2:]
            rewritten_stripped = "**/" + stripped[2:]
            return [rewritten_marker, rewritten_stripped + "/**"]

        # Any OTHER wildcard-bearing trailing-slash pattern (e.g.
        # "src/*/", "src/**/") -- keep the directory-only marker line
        # unchanged (EXCLUDE parity) and add the "/**" contents line
        # (Bug #1876 F10, INCLUDE parity).
        if "/" not in stripped:
            # Bug #1876 F12: a SINGLE-segment wildcard marker (e.g.
            # "tests*/", "build-*/", "*.d/") is NOT root-anchored as a
            # directory-only marker line (gitignore: a trailing-only
            # slash never anchors), so it already excludes/selects at any
            # depth on its own -- but the naive "stripped + '/**'"
            # content line DOES contain an internal "/", which gitignore/
            # pathspec anchors at the repository root, silently dropping
            # nested matches (e.g. "a/tests/x.py", "src/tests/y.py") for
            # INCLUDE purposes. Mirror the literal single-segment
            # trailing-slash policy above (rule 3 / the "docs/" case) by
            # prefixing the content line with "**/" so it matches at any
            # depth too, exactly like the marker line already does.
            return [pattern, "**/" + stripped + "/**"]
        return [pattern, stripped + "/**"]

    if (
        "/" not in pattern
        and "." not in pattern
        and not any(ch in _GLOB_METACHARACTERS for ch in pattern)
    ):
        return ["**/" + pattern, "**/" + pattern + "/**"]

    if pattern.startswith("*/"):
        return ["**/" + pattern[2:]]

    return [pattern]


def parse_exclude_patterns(exclude_path: Optional[str]) -> List[str]:
    """
    Split a comma-separated exclude_path string into independent glob patterns.

    This is the single source of truth for parsing the exclude_path parameter.
    Both the semantic leg and the FTS leg must call this before applying exclusions.

    Args:
        exclude_path: Raw exclude_path string (may be None, empty, single pattern,
            or comma-separated patterns).  Whitespace around each pattern is stripped.
            Empty fragments (from leading/trailing/double commas) are dropped.

    Returns:
        List of non-empty, trimmed pattern strings.  Returns [] for None, empty,
        or whitespace-only input.

    Examples:
        >>> parse_exclude_patterns(None)
        []
        >>> parse_exclude_patterns("**/node_modules/**")
        ['**/node_modules/**']
        >>> parse_exclude_patterns("code/dir-a/**,code/dir-b/**")
        ['code/dir-a/**', 'code/dir-b/**']
        >>> parse_exclude_patterns("  *.min.js , **/vendor/**  ")
        ['*.min.js', '**/vendor/**']
    """
    if not exclude_path or not exclude_path.strip():
        return []
    return [p.strip() for p in exclude_path.split(",") if p.strip()]


class PathPatternMatcher:
    """
    Matches file paths against glob patterns with cross-platform support.

    This class provides efficient glob-style pattern matching for file paths,
    with automatic path normalization and pattern caching for performance.

    Features:
    - Cross-platform path separator normalization
    - Standard glob patterns (*, **, ?, [seq], [!seq])
    - Pattern compilation caching
    - Case sensitivity based on platform

    Examples:
        >>> matcher = PathPatternMatcher()
        >>> matcher.matches_pattern("src/tests/test.py", "*/tests/*")
        True
        >>> matcher.matches_pattern("src/module.py", "*/tests/*")
        False
        >>> matcher.matches_any_pattern("dist/app.min.js", ["*.min.js", "*/vendor/**"])
        True
    """

    def __init__(self):
        """Initialize the pattern matcher with empty cache."""
        self._pattern_cache = {}

    def _normalize_path(self, path: str) -> str:
        """
        Normalize path separators and components for consistent matching.

        Converts all paths to forward slashes and resolves . and .. components.
        This ensures patterns work consistently across platforms.

        Args:
            path: Path string to normalize

        Returns:
            Normalized path with forward slashes

        Examples:
            >>> matcher = PathPatternMatcher()
            >>> matcher._normalize_path("src\\\\tests\\\\test.py")
            'src/tests/test.py'
            >>> matcher._normalize_path("src/./tests/../tests/test.py")
            'src/tests/test.py'
        """
        if not path:
            return ""

        # Bug #1876 item 4 re-measurement follow-up: profiling the shared
        # selector's per-path cost over 8,000 paths found this method's
        # unconditional PurePosixPath construction below responsible for
        # ~73% of the total per-file selection time -- dwarfing pathspec's
        # own match_file() cost -- even though the overwhelming majority of
        # paths reaching this per-file hot path (relative paths from
        # os.walk/ripgrep) are already clean POSIX-relative strings that
        # need no `.`/`..` resolution at all. Skip the PurePosixPath
        # parse+rebuild entirely once a handful of cheap substring checks
        # prove none of the conditions it exists to handle apply. Any
        # remaining path (backslashes, `..`, repeated slashes, a leading
        # `./`, an internal `/./`, a trailing `/` or `/.`, or the bare
        # string ".") falls through to the unchanged slow path below.
        if (
            "\\" not in path
            and ".." not in path
            and "//" not in path
            and not path.endswith("/")
            and not path.endswith("/.")
            and not path.startswith("./")
            and "/./" not in path
            and path != "."
        ):
            return path

        # Convert to PurePosixPath for consistent forward slash handling
        # This works on all platforms and normalizes separators
        try:
            # Handle both Windows and Unix paths
            normalized = PurePosixPath(path.replace("\\", "/"))

            # Resolve . and .. components
            # Note: PurePosixPath.parts includes '/' as first element for absolute paths
            parts: List[str] = []
            is_absolute = str(normalized).startswith("/")

            for part in normalized.parts:
                # Skip the root '/' part (it's just a marker for absolute paths)
                if part == "/":
                    continue
                if part == "..":
                    if parts and parts[-1] != "..":
                        parts.pop()
                    else:
                        parts.append(part)
                elif part != "." and part:
                    parts.append(part)

            result = "/".join(parts)

            # Preserve leading slash for absolute paths
            if is_absolute and result:
                result = "/" + result
            elif is_absolute and not result:
                result = "/"

            return result
        except (ValueError, TypeError):
            # Fallback: just replace backslashes
            return path.replace("\\", "/")

    def _validate_and_expand(self, pattern: str) -> List[str]:
        """
        Validate a single (already-typed-as-str) pattern and return its
        normalized, brace-expanded, ``*/``-to-``**/``-normalized gitwildmatch
        lines.

        Returns ``[]`` for a blank pattern (never matches, matching the
        long-standing pre-#1876 behaviour for that case). Raises
        ``InvalidPatternError`` for gitignore negation/comment syntax
        (``!x``, ``#x``) or a malformed brace group -- these are structural
        problems, not "just doesn't match anything today" cases.
        """
        if not pattern or not pattern.strip():
            return []

        stripped = pattern.strip()
        normalized = self._normalize_path(stripped)
        # Re-measurement follow-up (item 4 investigation): _normalize_path
        # exists to resolve real file-path `.`/`..`/backslash noise, but its
        # PurePosixPath-based resolution drops a trailing "/" as a side
        # effect -- semantically significant for a PATTERN, since it is
        # normalize_glob_pattern's own "directory contents only" marker
        # (see its "trailing slash" case below). Restore it here so this
        # indexed-matcher path and the unindexed `rg -g` path (which calls
        # normalize_glob_pattern directly on the untouched raw pattern, so
        # it never loses this) treat "docs/" identically.
        if stripped.endswith("/") and normalized and not normalized.endswith("/"):
            normalized = normalized + "/"
        if not normalized:
            return []

        if normalized.startswith("!"):
            raise InvalidPatternError(
                f"Pattern cannot start with '!': gitignore negation syntax "
                f"is not supported for a standalone include/exclude "
                f"pattern (it can never produce a match on its own): "
                f"{pattern!r}"
            )
        if normalized.startswith("#"):
            raise InvalidPatternError(
                f"Pattern cannot start with '#': gitignore treats this as "
                f"a full-line comment, so the pattern would silently "
                f"match nothing: {pattern!r}"
            )

        expanded = _expand_braces(normalized)
        lines: List[str] = []
        for variant in expanded:
            lines.extend(normalize_glob_pattern(variant))
        return lines

    def matches_pattern(self, path: str, pattern: str) -> bool:
        """
        Check if a path matches a glob pattern using gitignore-style matching.

        This method uses pathspec library for consistent gitignore-style glob matching,
        which properly handles ** patterns as "this directory and all subdirectories"
        rather than requiring at least one subdirectory level.

        Patterns starting with ``*/`` are automatically normalized to ``**/`` so
        they match at any depth including the repository root (bug #1211).
        ``*/tests/*`` therefore matches both ``tests/foo.py`` (root) and
        ``src/tests/foo.py`` (nested).  ``tests/*`` (no leading ``*/``) is
        left unchanged and continues to match root level only.

        Args:
            path: File path to check
            pattern: Glob pattern to match against (gitignore-style)

        Returns:
            True if path matches pattern, False otherwise

        Raises:
            TypeError: If pattern is None
            ValueError: If pattern is invalid

        Examples:
            >>> matcher = PathPatternMatcher()
            >>> matcher.matches_pattern("tests/test.py", "*/tests/*")
            True
            >>> matcher.matches_pattern("src/tests/test.py", "*/tests/*")
            True
            >>> matcher.matches_pattern("src/module.py", "*/tests/*")
            False
            >>> matcher.matches_pattern("code/src/Main.java", "code/src/**/*.java")
            True
            >>> matcher.matches_pattern("code/src/util/Helper.java", "code/src/**/*.java")
            True
        """
        if pattern is None:
            raise TypeError("Pattern cannot be None")

        normalized_path = self._normalize_path(path)

        # Validates, rejects negation/comment syntax, and expands brace
        # groups (Bug #1876 items 1 and 4) on top of the existing */ -> **/
        # normalization (bug #1211).
        lines = self._validate_and_expand(pattern)
        if not lines:
            return False

        try:
            # Check if this exact (possibly brace-expanded) line set is
            # already cached.
            cache_key = tuple(lines)
            if cache_key not in self._pattern_cache:
                # Create PathSpec object and cache it
                # Use "gitwildmatch" for gitignore-style glob matching
                # This properly handles ** patterns and other glob features.
                # A brace-expanded pattern becomes multiple non-negated
                # lines, which pathspec ORs together -- identical semantics
                # to "this pattern matches any of its brace alternatives".
                spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
                self._pattern_cache[cache_key] = spec
            else:
                spec = self._pattern_cache[cache_key]

            # Use pathspec to match the path
            # pathspec.match_file() handles:
            # - ** as "this directory and all subdirectories"
            # - * as single-level wildcard
            # - ?, [seq], [!seq] patterns
            # - Proper path separator handling
            return bool(spec.match_file(normalized_path))

        except InvalidPatternError:
            raise
        except Exception as e:
            # Invalid pattern - treat as ValueError
            raise InvalidPatternError(f"Invalid glob pattern: {pattern!r}") from e

    def matches_any_pattern(self, path: str, patterns: List[str]) -> bool:
        """
        Check if a path matches any of the given patterns.

        Args:
            path: File path to check
            patterns: List of glob patterns to match against

        Returns:
            True if path matches at least one pattern, False otherwise

        Raises:
            InvalidPatternError: If any pattern is malformed or is
                gitignore negation/comment syntax that can never match
                (Bug #1876 item 4) -- this method no longer silently skips
                an invalid pattern, since doing so let an exclude pattern
                fail OPEN (silently widening the search) with zero signal.

        Examples:
            >>> matcher = PathPatternMatcher()
            >>> patterns = ["*/tests/*", "*.min.js", "**/vendor/**"]
            >>> matcher.matches_any_pattern("src/tests/test.py", patterns)
            True
            >>> matcher.matches_any_pattern("src/module.py", patterns)
            False
        """
        if not patterns:
            return False

        # Bug #1876 item 5: compile the WHOLE list into one PathSpec for
        # this call, instead of the old per-pattern matches_pattern loop
        # (each iteration its own cache lookup/spec build).
        compiled = self.compile_patterns(patterns)
        if compiled is None:
            return False
        return compiled.matches(path)

    def compile_patterns(
        self, patterns: Optional[List[str]], *, is_include: bool = False
    ) -> Optional["CompiledPatternSet"]:
        """
        Validate and compile a list of patterns into ONE reusable
        ``CompiledPatternSet`` (Bug #1876 items 4, 5, 6).

        Every pattern is validated and brace-expanded exactly once here,
        and all resulting lines are combined into a single
        ``pathspec.PathSpec``. ``matches_any_pattern`` (used today by every
        regex_search/xray_search include/exclude filter call site) is
        implemented on top of this, so it is exercised on every real
        request, not merely by tests.

        Args:
            patterns: Pattern list, or ``None``/empty for "no filter".
            is_include: Bug #1876 N1. When ``True``, the returned set's
                ``matches()`` uses ripgrep's INCLUDE semantics instead of
                gitignore/pathspec's directory-containment semantics --
                see ``CompiledPatternSet._matches_as_include`` for the
                full rationale. Defaults to ``False`` (unchanged, pre-N1
                pathspec matching) so every OTHER existing caller of this
                method -- ``matches_any_pattern`` (exclude-style, per this
                codebase's own established convention: see
                tantivy_index_manager.py's exclude filtering) and any
                direct caller -- is completely unaffected. ``create_selector``
                is the ONLY caller that passes ``is_include=True``, for its
                include set specifically.

        Returns:
            ``None`` if ``patterns`` is falsy (caller applies no filter).
            Otherwise a ``CompiledPatternSet``.

        Raises:
            InvalidPatternError: If any item is not a string, is blank or
                whitespace-only (Bug #1876 item N4 -- a blank pattern is a
                plausible "unfilled field" mistake that used to diverge
                silently: the matcher treated it as "matches nothing"
                while the unindexed ``rg -g ''``/``rg -g '!'`` command
                line treats it as "matches everything"/a bare negation),
                is malformed, or is gitignore negation/comment syntax.
                Scoped to this list-validation entry point only --
                ``matches_pattern``'s separate single-pattern API (used
                outside #1876's scope by tantivy_index_manager.py and
                filesystem_vector_store.py) keeps its pre-existing "blank
                pattern matches nothing" contract via
                ``_validate_and_expand`` unchanged.
        """
        if patterns is None:
            return None

        # Bug #1876 F6: a bare string is itself iterable (each character
        # is a valid, non-blank "pattern" string), so without this check
        # compile_patterns("*.md") silently iterated to four
        # single-character glob patterns ('*', '.', 'm', 'd') instead of
        # failing loud on the caller's mistake -- the same shape of "fail
        # open" gap N4/N5 close for blank/non-string list ITEMS, just one
        # level up, for the CONTAINER itself. Both regex_search.py's
        # service-layer validation and XRaySearchEngine.run() call this
        # exact method, so this single check closes the hole in both.
        # Checked BEFORE the emptiness check below so even an empty
        # string ("", falsy like an empty list) fails loud rather than
        # being silently treated as "no filter".
        if not isinstance(patterns, (list, tuple)):
            raise InvalidPatternError(
                f"Patterns must be a list (or tuple) of strings, got "
                f"{type(patterns).__name__}: {patterns!r}"
            )

        if not patterns:
            return None

        all_lines: List[str] = []
        for idx, pattern in enumerate(patterns):
            if not isinstance(pattern, str):
                raise InvalidPatternError(
                    f"Pattern at index {idx} must be a string, got "
                    f"{type(pattern).__name__}: {pattern!r}"
                )
            if not pattern.strip():
                raise InvalidPatternError(
                    f"Pattern at index {idx} is blank or whitespace-only: "
                    f"{pattern!r}. A blank pattern is a common 'unfilled "
                    f"field' mistake and previously diverged silently "
                    f"between the indexed matcher (matches nothing) and "
                    f"the unindexed ripgrep -g command line (matches "
                    f"everything for an include, or a bare negation for "
                    f"an exclude) -- reject it explicitly instead."
                )
            all_lines.extend(self._validate_and_expand(pattern))

        if not all_lines:
            return CompiledPatternSet(self, None, is_include=is_include)

        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", all_lines)
        except Exception as e:
            raise InvalidPatternError(f"Invalid glob pattern(s): {patterns!r}") from e

        return CompiledPatternSet(self, spec, is_include=is_include)

    def create_selector(
        self,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
    ) -> "PathSelector":
        """
        Build a ``PathSelector`` for ONE query: an include
        ``CompiledPatternSet`` (or ``None`` for "no include filter") and an
        exclude ``CompiledPatternSet`` (or ``None``), each built exactly
        once -- not once per path.

        This is the single shared helper item 6's four duplicated
        include/exclude blocks (regex_search.py x2, search_engine.py,
        xray_graph.py) are wired onto: build ONE selector before a file
        loop, then call ``selector.select(path)`` per file, instead of
        each site independently normalizing/recompiling per path.

        Bug #1876 N1: the include set is compiled with
        ``is_include=True`` (ripgrep-reference semantics -- a directory
        match never implies its contents also match); the exclude set is
        unchanged (gitignore/pathspec containment semantics, confirmed
        already consistent with the unindexed ``rg -g`` exclude path).
        """
        return PathSelector(
            self.compile_patterns(include_patterns, is_include=True),
            self.compile_patterns(exclude_patterns, is_include=False),
        )


class CompiledPatternSet:
    """
    One compiled ``pathspec.PathSpec`` built from an already-validated,
    brace-expanded pattern list (or an empty/``None`` spec for "matches
    nothing", mirroring a blank pattern's existing semantics).

    Construct via ``PathPatternMatcher.compile_patterns`` -- never directly.
    """

    def __init__(
        self,
        matcher: "PathPatternMatcher",
        spec: Optional["pathspec.PathSpec"],
        is_include: bool = False,
    ) -> None:
        self._matcher = matcher
        self._spec = spec
        self._is_include = is_include

    def matches(self, path: str) -> bool:
        """Normalize ``path`` once and check it against the compiled spec."""
        if self._spec is None:
            return False
        normalized_path = self._matcher._normalize_path(path)
        if self._is_include:
            return self._matches_as_include(normalized_path)
        return bool(self._spec.match_file(normalized_path))

    def _matches_as_include(self, normalized_path: str) -> bool:
        """Bug #1876 N1: ripgrep's ``-g`` INCLUDE semantics (the reference,
        confirmed against real ripgrep 14.1.1) never let a pattern that
        matches a DIRECTORY implicitly include everything below it --
        ``*`` matches exactly one path segment, full stop. pathspec's
        gitwildmatch grammar (built for gitignore's directory-vs-file
        -agnostic EXCLUDE semantics) instead compiles every non-``**``
        -terminated trailing segment with an optional trailing "this
        could be a directory, so match its entire contents too" group
        (pathspec's own internal ``_PATHSPEC_DIR_MARK`` named group) --
        exactly the behavior that makes ``src/*`` wrongly include
        ``src/a/b.ts``.

        A match is accepted for INCLUDE purposes only when at least one
        of the compiled pattern lines matches WITHOUT relying on that
        group -- i.e. a genuine full-path match, never merely "matches a
        directory prefix of this path, so everything under it counts
        too". Excludes are unaffected (this method is never called for
        an exclude ``CompiledPatternSet``) -- confirmed already
        consistent with the unindexed ``rg -g`` exclude path.

        The accepted bare-directory policy (``docs`` -> ``**/docs`` +
        ``**/docs/**``) is unaffected: its explicit ``**/docs/**`` line
        matches contents directly with NO directory-implied group in its
        regex at all (a pattern whose last segment is a literal ``**``
        never gets one -- verified directly against pathspec's compiled
        regex), so it always satisfies this check on its own, at any
        depth, exactly as the unindexed ripgrep walk does.
        """
        assert self._spec is not None
        for pattern in self._spec.patterns:
            if not isinstance(pattern, GitWildMatchPattern) or pattern.regex is None:
                continue
            match = pattern.regex.match(normalized_path)
            if match is None:
                continue
            try:
                matched_only_via_directory_group = (
                    match.group(_PATHSPEC_DIR_MARK) is not None
                )
            except IndexError:
                # This pattern's regex has no such named group at all
                # (e.g. it ends in a literal "**") -- a match here can
                # only be a genuine, direct match.
                matched_only_via_directory_group = False
            if not matched_only_via_directory_group:
                return True
        return False


class PathSelector:
    """
    A precompiled include/exclude filter for one query.

    ``select(path)`` returns ``True`` to keep the path: it must match the
    include set (if any) and must NOT match the exclude set (if any) --
    identical semantics to the pre-existing per-call-site pattern:
    ``if include_patterns and not matches_any_pattern(...): skip`` /
    ``if exclude_patterns and matches_any_pattern(...): skip``.

    Construct via ``PathPatternMatcher.create_selector`` -- never directly.
    """

    def __init__(
        self,
        include_set: Optional[CompiledPatternSet],
        exclude_set: Optional[CompiledPatternSet],
    ) -> None:
        self._include = include_set
        self._exclude = exclude_set

    def select(self, path: str) -> bool:
        if self._include is not None and not self._include.matches(path):
            return False
        if self._exclude is not None and self._exclude.matches(path):
            return False
        return True
