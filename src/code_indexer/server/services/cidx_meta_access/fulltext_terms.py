"""Full-text queries re-evaluated on the text one restricted caller sees.

A restricted caller's full-text hit on a projected catalog document is kept
only when every required word of the query occurs in the text the caller
is shown, so whether a document appears depends only on that text. A word
is the engine's own unit: the sanitized query split on whitespace, each
word reduced to its lower-cased alphanumeric tokens (the index tokenizer)
and matched as a contiguous phrase within one line. Only text the
projection kept verbatim from the document counts (never a placeholder,
never text a rewrite introduced), so a kept hit also matches the document
the engine searched.

Forms whose outcome cannot be decided on the shown text alone are refused
before searching: boolean operators, wildcard, boost and slop syntax, a
word with no token, an over-long token, and fuzzy matching of more than
one word. One fuzzy word is judged by edit distance (an adjacent
transposition costs one) against each shown token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, NoReturn, Optional, Sequence, Tuple

from .depmap_projection import DOMAIN_PLACEHOLDER, RESTRICTED_PLACEHOLDER

FULLTEXT_FORM_REFUSAL = "this full-text query form is not available for this repository"

_OPERATORS = frozenset({"AND", "OR", "NOT"})
_GRAMMAR_CHARS = frozenset("*^~")
_MAX_TOKEN_BYTES = 40
_TOKEN = re.compile(r"[^\W_]+")
_PLACEHOLDERS = (RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER)

Token = Tuple[str, int, int]


@dataclass(frozen=True)
class FullTextTerms:
    """The required words of one full-text query: a token phrase per word,
    and the single fuzzy word with its distance (None without fuzzy)."""

    phrases: Tuple[Tuple[str, ...], ...]
    fuzzy_word: Optional[str] = None
    edit_distance: int = 0


@dataclass(frozen=True)
class VisibleMatch:
    """Where a document first shows its first required word: the 1-based
    line and the matched text as shown."""

    line: int
    text: str


def effective_edit_distance(fuzzy: bool, edit_distance: int) -> int:
    """The edit distance a search runs with (``fuzzy`` alone means 1)."""
    return 1 if fuzzy and edit_distance == 0 else edit_distance


def raise_fulltext_refusal() -> NoReturn:
    """Refuse the request with the neutral full-text form message."""
    from ...query.semantic_query_manager import SearchParameterError

    raise SearchParameterError(FULLTEXT_FORM_REFUSAL)


def _tokens(text: str) -> List[str]:
    return [token.lower() for token in _TOKEN.findall(text)]


def parse_fulltext_terms(query: str, edit_distance: int) -> Optional[FullTextTerms]:
    """The query's required words, or None when its outcome cannot be
    decided on shown text alone (the form is refused)."""
    from ....services.tantivy_index_manager import sanitize_fts_query

    words = sanitize_fts_query(query).split()
    if not words or any(word in _OPERATORS for word in words):
        return None
    if any(ch in _GRAMMAR_CHARS for word in words for ch in word):
        return None
    phrases = tuple(tuple(_tokens(word)) for word in words)
    if not all(phrases):
        return None
    if any(len(t.encode("utf-8")) > _MAX_TOKEN_BYTES for p in phrases for t in p):
        return None
    if edit_distance > 0:
        if len(words) != 1:
            return None
        return FullTextTerms(phrases, words[0], edit_distance)
    return FullTextTerms(phrases)


def _spans(text: str, start: int, end: int) -> List[Token]:
    return [
        (m.group(0).lower(), m.start(), m.end())
        for m in _TOKEN.finditer(text, start, end)
    ]


def _placeholder_spans(shown: str) -> List[Tuple[int, int]]:
    found: List[Tuple[int, int]] = []
    for placeholder in _PLACEHOLDERS:
        start = shown.find(placeholder)
        while start >= 0:
            found.append((start, start + len(placeholder)))
            start = shown.find(placeholder, start + len(placeholder))
    return found


def _cut(raw: str, at: int) -> bool:
    """True when a token of *raw* continues across offset *at*."""
    return 0 < at < len(raw) and raw[at - 1].isalnum() and raw[at].isalnum()


def _visible_runs(raw: str, shown: str) -> List[List[Token]]:
    """Runs of consecutive tokens of the shown line that are tokens of the
    raw line too, adjacent there as well, outside every placeholder."""
    if not shown.strip():
        return []
    if raw == shown:
        blocks = [(0, 0, len(raw))]
    else:
        matcher = SequenceMatcher(None, raw, shown, autojunk=False)
        blocks = [(a, b, n) for a, b, n in matcher.get_matching_blocks() if n]
    hidden = _placeholder_spans(shown)
    runs: List[List[Token]] = []
    for a, b, size in blocks:
        run: List[Token] = []
        for token in _spans(shown, b, b + size):
            raw_start, raw_end = a + token[1] - b, a + token[2] - b
            masked = any(s < token[2] and token[1] < e for s, e in hidden)
            if masked or _cut(raw, raw_start) or _cut(raw, raw_end):
                if run:
                    runs.append(run)
                run = []
                continue
            run.append(token)
        if run:
            runs.append(run)
    return runs


def _within_distance(word: str, token: str, limit: int) -> bool:
    """Optimal string alignment distance of *word* and *token* <= *limit*."""
    if abs(len(word) - len(token)) > limit:
        return False
    previous2: List[int] = []
    previous = list(range(len(token) + 1))
    for i in range(1, len(word) + 1):
        current = [i] + [0] * len(token)
        for j in range(1, len(token) + 1):
            cost = 0 if word[i - 1] == token[j - 1] else 1
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost
            )
            if (
                i > 1
                and j > 1
                and word[i - 1] == token[j - 2]
                and word[i - 2] == token[j - 1]
            ):
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return False
        previous2, previous = previous, current
    return previous[-1] <= limit


def _find(
    runs: Sequence[List[Token]], terms: FullTextTerms, index: int
) -> Optional[Tuple[int, int]]:
    """Shown offsets of the first occurrence of required word *index*."""
    if terms.fuzzy_word is not None:
        for run in runs:
            for token, start, end in run:
                if _within_distance(terms.fuzzy_word, token, terms.edit_distance):
                    return start, end
        return None
    phrase = terms.phrases[index]
    for run in runs:
        words = [token for token, _, _ in run]
        for i in range(len(run) - len(phrase) + 1):
            if tuple(words[i : i + len(phrase)]) == phrase:
                return run[i][1], run[i + len(phrase) - 1][2]
    return None


def first_visible_match(
    raw_lines: Sequence[str], shown_lines: Sequence[str], terms: FullTextTerms
) -> Optional[VisibleMatch]:
    """Where the shown document first holds the first required word, when
    it holds every required word; else None."""
    pending = set(range(1 if terms.fuzzy_word is not None else len(terms.phrases)))
    first: Optional[VisibleMatch] = None
    for number, (raw, shown) in enumerate(zip(raw_lines, shown_lines), start=1):
        runs = _visible_runs(raw, shown)
        for index in sorted(pending):
            found = _find(runs, terms, index)
            if found is None:
                continue
            pending.discard(index)
            if index == 0:
                first = VisibleMatch(number, shown[found[0] : found[1]])
        if not pending:
            return first
    return None
