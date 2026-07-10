"""Regex -> required-trigram analysis for index-assisted regex search.

The regex endpoint scans a repository's working tree with ripgrep. On a large
repo hosted on NFS that scan is I/O-bound (tens of seconds). A trigram index
(``trigram -> files``) lets us pre-select a small set of candidate files and run
ripgrep over only those, preserving ripgrep's exact regex semantics.

The pre-filter is only correct if it never drops a real match. This module
derives, from a regex, a set of trigrams that EVERY matching string must
contain -- a *necessary* condition. Candidate files are those containing all of
those trigrams (a guaranteed superset of the true matches). ripgrep then does
the precise matching over that superset.

Safety over cleverness: when the pattern cannot be analysed into a provably
required trigram set, :func:`extract_required_trigrams` returns ``None`` and the
caller falls back to a full scan. It must never return a set that could exclude
a matching file.
"""

from __future__ import annotations

from typing import List, Optional, Set

# Minimum literal-run length that yields at least one trigram.
_TRIGRAM = 3

# Escaped ASCII letters/digits are metacharacter escapes (``\w \s \d \b \n \1``
# ...) rather than literals, so they break a fixed literal run. Escaped
# non-alphanumerics (``\. \+ \/ \\``) are literal characters.
_QUANTIFIER_STARTS = frozenset("?*+{")


def trigrams(text: str) -> Set[str]:
    """Return the set of 3-character substrings (trigrams) of ``text``."""
    return {text[i : i + _TRIGRAM] for i in range(len(text) - _TRIGRAM + 1)}


def _is_quantifier_at(pattern: str, i: int) -> int:
    """If a quantifier starts at ``pattern[i]``, return its length, else 0.

    Recognises ``?``, ``*``, ``+`` and ``{m}`` / ``{m,}`` / ``{m,n}``. A ``{``
    that does not form a valid counted quantifier is treated as a literal.
    """
    if i >= len(pattern):
        return 0
    c = pattern[i]
    if c in "?*+":
        return 1
    if c == "{":
        j = i + 1
        saw_digit = False
        while j < len(pattern) and pattern[j].isdigit():
            j += 1
            saw_digit = True
        if j < len(pattern) and pattern[j] == ",":
            j += 1
            while j < len(pattern) and pattern[j].isdigit():
                j += 1
        if saw_digit and j < len(pattern) and pattern[j] == "}":
            return j - i + 1
    return 0


def _required_literal_runs(pattern: str) -> Optional[List[str]]:
    """Return the maximal runs of fixed, required literal characters, or None.

    A "fixed required literal" is a literal character that appears exactly once
    in every match: it is not a metacharacter, not inside a character class, and
    not followed by any quantifier (``?``/``*``/``+``/``{...}``). Consecutive
    such characters form a run that is guaranteed to appear contiguously in every
    matching string.

    Returns ``None`` (meaning "cannot analyse safely -> full scan") when the
    pattern contains alternation ``|`` or a group ``(`` -- either can make a
    literal optional or introduce OR semantics that this conservative analysis
    does not model.
    """
    runs: List[str] = []
    current: List[str] = []

    def flush() -> None:
        if current:
            runs.append("".join(current))
            current.clear()

    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]

        # Constructs we do not model -> bail to a full scan.
        if c == "|" or c == "(":
            return None

        # Character class: a single variable character -> run breaker. Skip to
        # the matching ']' (respecting an escaped ']' and a literal leading ']').
        if c == "[":
            flush()
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":  # literal ']' as first class member
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            continue

        # Anchors / dot -> run breakers, single char.
        if c in ".^$":
            flush()
            i += 1
            continue

        # A quantifier appearing here applies to the previous atom. If we had a
        # literal in the current run, its last char is quantified (optional or
        # variable) and must be removed from the required run.
        qlen = _is_quantifier_at(pattern, i)
        if qlen:
            if current:
                current.pop()
                flush()
            i += qlen
            continue

        # Escape sequence.
        if c == "\\":
            if i + 1 >= n:  # dangling backslash -> treat as run breaker
                flush()
                i += 1
                continue
            nxt = pattern[i + 1]
            if nxt.isalnum():
                # \w \s \d \b \n \1 ... -> metacharacter/escape -> run breaker.
                flush()
                i += 2
                continue
            # \. \+ \/ \\ ... -> literal punctuation char. It is required only
            # if not immediately quantified.
            after = i + 2
            if _is_quantifier_at(pattern, after):
                flush()  # quantified escaped literal -> not fixed
                i = after + _is_quantifier_at(pattern, after)
                continue
            current.append(nxt)
            i += 2
            continue

        # Plain literal char. Required only if the NEXT token is not a quantifier
        # (a quantifier would make this char optional/variable).
        if _is_quantifier_at(pattern, i + 1):
            flush()
            i += 1 + _is_quantifier_at(pattern, i + 1)
            continue
        current.append(c)
        i += 1

    flush()
    return runs


def extract_required_trigrams(
    pattern: str, case_insensitive: bool = False
) -> Optional[Set[str]]:
    """Return trigrams every match of ``pattern`` must contain, or ``None``.

    The returned set is a *necessary* condition: any string matching ``pattern``
    contains every trigram in it, so files lacking any of them cannot match and
    are safely excluded. Trigrams are lowercased so the same index serves both
    case-sensitive and case-insensitive searches (a superset either way; the
    caller's ripgrep pass enforces exact case).

    Returns ``None`` when no provably-required trigram can be derived (short or
    wildcard-dominated patterns, alternation, groups) -- the caller must then
    scan without pre-filtering. Never returns a set that could exclude a match.
    """
    if not pattern:
        return None
    runs = _required_literal_runs(pattern)
    if runs is None:
        return None
    required: Set[str] = set()
    for run in runs:
        if len(run) >= _TRIGRAM:
            required |= trigrams(run.lower())
    # An empty set would select every file (no pruning) -> signal full scan.
    return required or None
