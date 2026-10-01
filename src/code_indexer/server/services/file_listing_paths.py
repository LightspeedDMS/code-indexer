"""
Shared path / gitignore-style pattern helpers for repository file-listing
front doors (Bug #1886).

Extracted from file_service.py and routers/inline_repos_v2.py (round 4) into
one focused module so MCP browse_directory/list_files
(mcp/handlers/files.py), REST GET /api/repositories/{repo_id}/files
(routers/inline_repos_v2.py), and FileListingService itself (file_service.py)
all agree on:

  - normalize_listing_path(path): strip leading/trailing "/", collapse a
    leading "./" repeat-safely, and map "." to the repository root ("").
  - escape_gitwildmatch_literal(segment): escape gitwildmatch metacharacters
    in a LITERAL repo-relative path segment (never a caller-supplied glob
    pattern) before it is spliced into a gitignore-style pattern string.
  - is_absolute_path_pattern(user_path_pattern): whether a caller-supplied
    path_pattern carries its own directory component and should therefore
    override `path` entirely rather than be combined with it.
  - literal_dir_prefix_of_pattern(pattern): the glob-free directory prefix
    of a path_pattern, or None when the directory portion itself carries a
    glob char (the pattern already expresses its own depth).
  - compute_direct_children_of(normalized_path, recursive, final_path_pattern,
    pattern_overrides_path): the FileListQueryParams.direct_children_of
    value to apply.
  - build_non_composite_path_pattern(path, user_path_pattern): the REST
    non-composite GET /api/repositories/{repo_id}/files pattern builder.
"""

from typing import Optional

# Characters that make a path_pattern segment a glob rather than a literal
# directory name (Bug #1886).
_GLOB_CHARS = frozenset("*?[{")

# gitwildmatch metacharacters that must be escaped when a LITERAL
# repo-relative path segment (never a caller-supplied glob pattern) is
# spliced into a gitignore-style pattern string (Bug #1886, R4). Mirrors
# pathspec's own GitWildMatchPattern.escape() meta-character set
# (`[`, `]`, `!`, `*`, `#`, `?`) plus the backslash escape character itself,
# which that helper does not cover -- an unescaped backslash in a literal
# path would otherwise begin its own (bogus) escape sequence when parsed.
_GITWILDMATCH_META_CHARS = frozenset("\\[]!*?#")


def normalize_listing_path(path: Optional[str]) -> str:
    """Normalize a repo-relative browse/list path (Bug #1886, R2).

    Strips leading/trailing "/" and a leading "./" (repeat-safe, e.g.
    "././src/" -> "src"), and maps a bare "." to the repository root ("").
    Shared by every path/recursive-aware listing front door (MCP
    browse_directory, MCP list_files, REST GET
    /api/repositories/{repo_id}/files) so a recursive and a non-recursive
    request against the same input agree on which subtree is meant, and so
    the built path_pattern never carries a leading "/" or "./" that would
    silently fail to match (pathspec's gitwildmatch does not treat a
    leading "./" as a no-op).
    """
    if not path:
        return ""
    normalized = path.strip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:].strip("/")
    if normalized == ".":
        normalized = ""
    return normalized


def escape_gitwildmatch_literal(segment: str) -> str:
    """Escape gitwildmatch metacharacters in a LITERAL repo-relative path
    (Bug #1886, R4).

    `path`/`path` query parameters name a real, literal directory on disk
    -- they are never a caller-supplied glob. When such a literal is
    spliced verbatim into a gitignore-style pattern string (e.g.
    "{path}/**/*"), any of "\\[]!*#?" it happens to contain is
    misinterpreted as a glob metacharacter instead of a literal character
    -- most commonly a bracket directory from a Next.js/SvelteKit/Nuxt/
    Astro dynamic route (`app/[slug]/page.tsx`), where pathspec's
    gitwildmatch treats "[slug]" as a character class ("any one of s, l,
    u, g") rather than the literal directory name, silently losing files
    inside it and matching unrelated single-character-named siblings
    instead.

    Never apply this to a caller-supplied path_pattern -- only to the
    literal path component a builder prefixes onto one.
    """
    if not segment:
        return segment
    return "".join(
        "\\" + ch if ch in _GITWILDMATCH_META_CHARS else ch for ch in segment
    )


def is_absolute_path_pattern(user_path_pattern: Optional[str]) -> bool:
    """True when a caller-supplied path_pattern is "absolute" -- i.e. it
    carries its own directory component ("/" anywhere) or starts with
    "**" -- and should therefore override `path` entirely rather than be
    combined with it.

    Shared by build_non_composite_path_pattern (REST) and
    mcp/handlers/files.py's _build_browse_path_pattern so the override
    rule is defined exactly once.
    """
    if not user_path_pattern:
        return False
    return "/" in user_path_pattern or user_path_pattern.startswith("**")


def literal_dir_prefix_of_pattern(pattern: str) -> Optional[str]:
    """Return the literal (glob-free) directory prefix of a path_pattern.

    Bug #1886 (R1): when a caller supplies an absolute-looking path_pattern
    (one that can override `path` entirely, e.g. browse_directory's
    "code/src/*.java" case), the depth base for a recursive=False
    single-level restriction must come from the PATTERN's own literal
    directory segments, not from a (possibly irrelevant/overridden) `path`
    parameter.

    Splits the pattern on "/" and drops the final segment (the filename
    component, which is expected to carry the glob, e.g. "*.java" or a
    trailing bare "*"). If any of the REMAINING (directory) segments
    themselves contain a glob character (*, ?, [, {) -- e.g. "**/*.py"
    ("**") or "src/*/x.py" ("*") -- the pattern already expresses its own
    depth and applying a partial literal prefix as a depth restriction
    would incorrectly exclude legitimate deeper matches, so None is
    returned (no restriction should be derived). Otherwise the joined
    directory segments are returned verbatim (possibly "" when the pattern
    has no "/" at all, e.g. a bare "*.java").

    Bug #1886 (R3): a caller-supplied absolute-LOOKING pattern (one whose
    own leading "/" -- or "./" -- made `_build_browse_path_pattern`/
    `_build_path_pattern` return it verbatim, overriding `path`) must have
    that leading "/"/"./" stripped BEFORE the directory prefix is derived.
    pathspec's gitwildmatch happily matches "/src/*.py" against "src/x.py"
    (a leading "/" is a harmless root anchor there), but FileInfo.path
    values never carry a leading slash (built via Path.relative_to()), so
    an unstripped "/src" base would never equal a real file's dirname
    ("src") and silently zero out every result.

    Only ever called on a RAW caller-supplied absolute pattern (never on a
    pattern this codebase built by escaping+prefixing `path`), so this
    function never needs to unescape gitwildmatch metacharacters itself.
    """
    normalized_pattern = pattern.lstrip("/")
    while normalized_pattern.startswith("./"):
        normalized_pattern = normalized_pattern[2:].lstrip("/")
    segments = normalized_pattern.split("/")
    dir_segments = segments[:-1]
    if any(any(c in _GLOB_CHARS for c in seg) for seg in dir_segments):
        return None
    return "/".join(dir_segments)


def compute_direct_children_of(
    normalized_path: str,
    recursive: bool,
    final_path_pattern: Optional[str],
    pattern_overrides_path: bool = False,
) -> Optional[str]:
    """Compute FileListQueryParams.direct_children_of (Bug #1886).

    recursive=True: no restriction (None) -- unchanged behavior.

    recursive=False: the depth base is `normalized_path` (the real,
    unescaped literal directory) UNLESS `pattern_overrides_path` is True,
    in which case a caller-supplied absolute path_pattern genuinely
    overrode `path` (e.g. browse_directory's R1 case) and the base must
    instead be derived from that pattern's own literal directory prefix
    (see literal_dir_prefix_of_pattern).

    Bug #1886 (R4): `final_path_pattern` must NEVER be used as the
    depth-base source when it was built by escaping+prefixing
    `normalized_path` (escape_gitwildmatch_literal) -- re-parsing it with
    literal_dir_prefix_of_pattern would either see an escaped
    metacharacter (e.g. "\\[") as a "glob char" and wrongly yield None (no
    restriction, leaking nested files), or yield a base carrying escape
    backslashes that can never equal a real file's (unescaped) dirname.
    `pattern_overrides_path` is the caller's explicit signal that this
    pattern is NOT one of those built-from-path patterns, but the raw
    caller-supplied pattern itself, safe to parse directly.
    """
    if recursive:
        return None
    if pattern_overrides_path and final_path_pattern:
        return literal_dir_prefix_of_pattern(final_path_pattern)
    return normalized_path


def build_non_composite_path_pattern(
    path: Optional[str], user_path_pattern: Optional[str]
) -> Optional[str]:
    """Build the effective path_pattern for a non-composite GET
    /api/repositories/{repo_id}/files listing (Bug #1886, R3 revision;
    R4: literal `path` escaping).

    `recursive` is composite-only for this route (per its own docstring)
    and applies NO restriction whatsoever on this branch -- `path` is the
    only new axis of control gained over HEAD, expressed purely as a
    path_pattern so it flows through FileListingService's existing
    gitignore-style glob matching (no direct_children_of, no depth
    restriction):

    - path absent/normalises to the repository root -> `user_path_pattern`
      unchanged (byte-identical to HEAD, which never looked at `path` for
      non-composite repos at all).
    - path given, no path_pattern -> "<escaped path>/**/*" (the whole
      subtree).
    - path given + a RELATIVE path_pattern (no "/", doesn't start with
      "**") -> "<escaped path>/**/<path_pattern>" (subtree, filtered).
    - path given + an ABSOLUTE-looking path_pattern (contains "/" or
      starts with "**") -> the pattern verbatim, overriding `path`
      entirely -- the same rule MCP's `_build_browse_path_pattern` uses,
      so a caller-supplied "code/src/*.java" style pattern keeps matching
      regardless of what (if anything) `path` was also set to.

    The literal `path` component is escaped (escape_gitwildmatch_literal)
    before being spliced into the pattern so a real directory name
    containing gitwildmatch metacharacters (e.g. a Next.js dynamic route
    "app/[slug]") is matched literally rather than as a glob. The
    caller-supplied `user_path_pattern` is NEVER auto-escaped -- it is
    already a pattern by contract.
    """
    normalized_path = normalize_listing_path(path)
    if not normalized_path:
        return user_path_pattern

    escaped_path = escape_gitwildmatch_literal(normalized_path)

    if not user_path_pattern:
        return f"{escaped_path}/**/*"

    if is_absolute_path_pattern(user_path_pattern):
        return user_path_pattern

    return f"{escaped_path}/**/{user_path_pattern}"
