"""Full-text queries re-evaluated on the text one restricted caller sees.

A restricted caller's full-text hit on a catalog document is kept only
when every required word of the query occurs in the text the caller is
shown, so whether a document appears depends only on that text. A word is
the engine's own unit: the sanitized query split on whitespace, each word
reduced to its tokens exactly as the index analyzes text (``engine_tokens``:
tantivy's ``default`` tokenizer of the content fields, i.e. the simple
tokenizer, tokens of 40 bytes or more dropped, lower-cased), and matched as
a contiguous phrase within one line. Only text the
projection kept verbatim from the document counts (never a placeholder,
never text a rewrite introduced), so a kept hit also matches the document
the engine searched.

Forms whose outcome cannot be decided on the shown text alone are refused
before searching: boolean operators, wildcard, boost and slop syntax, a
word with no token, an over-long token, a word longer than the chunker's
overlap (it may be split across indexed chunks), and fuzzy matching of
more than one word. One fuzzy word is judged by edit distance (an adjacent
transposition costs one) against each shown token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, List, Mapping, NoReturn, Optional, Sequence, Tuple

from .depmap_projection import DOMAIN_PLACEHOLDER, RESTRICTED_PLACEHOLDER

FULLTEXT_FORM_REFUSAL = "this full-text query form is not available for this repository"

_OPERATORS = frozenset({"AND", "OR", "NOT"})
_GRAMMAR_CHARS = frozenset("*^~")
# The engine drops a token of this many bytes or more (remove_long).
_MAX_TOKEN_BYTES = 40
_ASCII_RUN = re.compile(r"[A-Za-z0-9]+")
_PLACEHOLDERS = (RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER)

# (indexed form, or None when the engine drops the token; start; end)
Token = Tuple[Optional[str], int, int]


@lru_cache(maxsize=1)
def _max_word_chars() -> int:
    """The chunker's smallest overlap: a longer word may be split across
    indexed chunks, so the engine could miss a word the caller sees."""
    from ....indexing.fixed_size_chunker import FixedSizeChunker

    smallest = min(FixedSizeChunker.MODEL_CHUNK_SIZES.values())
    return int(smallest * FixedSizeChunker.OVERLAP_PERCENTAGE)


@lru_cache(maxsize=1)
def _analyzer() -> Any:
    """The analyzer of the index's content fields (tantivy ``default``)."""
    from tantivy import Filter, TextAnalyzerBuilder, Tokenizer

    return (
        TextAnalyzerBuilder(Tokenizer.simple())
        .filter(Filter.remove_long(_MAX_TOKEN_BYTES))
        .filter(Filter.lowercase())
        .build()
    )


@lru_cache(maxsize=65536)
def _engine_char(ch: str) -> str:
    """*ch* as the engine indexes it: lower-cased when the tokenizer keeps
    it inside a token, empty when it separates tokens."""
    tokens = _analyzer().analyze(ch)
    return str(tokens[0]) if tokens else ""


def _runs(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Spans of the maximal runs of token characters in text[start:end]."""
    if text.isascii():
        return [m.span() for m in _ASCII_RUN.finditer(text, start, end)]
    spans: List[Tuple[int, int]] = []
    run_start: Optional[int] = None
    for index in range(start, end):
        if _engine_char(text[index]):
            run_start = index if run_start is None else run_start
        elif run_start is not None:
            spans.append((run_start, index))
            run_start = None
    if run_start is not None:
        spans.append((run_start, end))
    return spans


def engine_tokens(text: str, start: int = 0, end: Optional[int] = None) -> List[Token]:
    """The tokens of text[start:end] as the engine indexes them."""
    tokens: List[Token] = []
    for s, e in _runs(text, start, len(text) if end is None else end):
        raw = text[s:e]
        if len(raw.encode("utf-8")) >= _MAX_TOKEN_BYTES:
            tokens.append((None, s, e))
        elif raw.isascii():
            tokens.append((raw.lower(), s, e))
        else:
            tokens.append(("".join(_engine_char(ch) for ch in raw), s, e))
    return tokens


@dataclass(frozen=True)
class FullTextTerms:
    """The required words of one full-text query: each word as the engine
    is asked for it, its token phrase, and the single fuzzy word with its
    distance (None without fuzzy)."""

    words: Tuple[str, ...]
    phrases: Tuple[Tuple[str, ...], ...]
    fuzzy_word: Optional[str] = None
    edit_distance: int = 0

    def engine_queries(self) -> Tuple[str, ...]:
        """One single-word engine query per required word."""
        return self.words


@dataclass(frozen=True)
class VisibleMatch:
    """Where a document first shows its first required word (the 1-based
    line and the matched text as shown), and how many times the required
    words occur in the shown text."""

    line: int
    text: str
    count: int = 0


@dataclass(frozen=True)
class FulltextOptions:
    """How a request asks the engine for full-text hits: case, snippet
    size, minimum score, and the request filters as
    ``TantivyIndexManager.search`` keyword arguments."""

    case_sensitive: bool = False
    snippet_lines: int = 5
    min_score: Optional[float] = None
    filters: Mapping[str, Any] = field(default_factory=dict, compare=False)


def effective_edit_distance(fuzzy: bool, edit_distance: int) -> int:
    """The edit distance a search runs with (``fuzzy`` alone means 1)."""
    return 1 if fuzzy and edit_distance == 0 else edit_distance


def raise_fulltext_refusal() -> NoReturn:
    """Refuse the request with the neutral full-text form message."""
    from ...query.semantic_query_manager import SearchParameterError

    raise SearchParameterError(FULLTEXT_FORM_REFUSAL)


def parse_fulltext_terms(query: str, edit_distance: int) -> Optional[FullTextTerms]:
    """The query's required words, or None when its outcome cannot be
    decided on shown text alone (the form is refused)."""
    from ....services.tantivy_index_manager import sanitize_fts_query

    words = sanitize_fts_query(query).split()
    if not words or any(word in _OPERATORS for word in words):
        return None
    if any(len(word) > _max_word_chars() for word in words):
        return None
    if any(ch in _GRAMMAR_CHARS for word in words for ch in word):
        return None
    word_tokens = [[form for form, _, _ in engine_tokens(word)] for word in words]
    if not all(word_tokens) or any(None in forms for forms in word_tokens):
        return None
    phrases = tuple(tuple(str(form) for form in forms) for forms in word_tokens)
    if edit_distance > 0:
        if len(words) != 1:
            return None
        return FullTextTerms(tuple(words), phrases, words[0], edit_distance)
    return FullTextTerms(tuple(words), phrases)


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
    return (
        0 < at < len(raw)
        and bool(_engine_char(raw[at - 1]))
        and bool(_engine_char(raw[at]))
    )


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
        for token in engine_tokens(shown, b, b + size):
            raw_start, raw_end = a + token[1] - b, a + token[2] - b
            masked = any(s < token[2] and token[1] < e for s, e in hidden)
            dropped = token[0] is None
            if masked or dropped or _cut(raw, raw_start) or _cut(raw, raw_end):
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


def _matches(
    runs: Sequence[List[Token]], terms: FullTextTerms, index: int
) -> List[Tuple[int, int]]:
    """Shown offsets of every occurrence of required word *index*."""
    found: List[Tuple[int, int]] = []
    if terms.fuzzy_word is not None:
        for run in runs:
            for token, start, end in run:
                if token is not None and _within_distance(
                    terms.fuzzy_word, token, terms.edit_distance
                ):
                    found.append((start, end))
        return found
    phrase = terms.phrases[index]
    for run in runs:
        words = [token for token, _, _ in run]
        for i in range(len(run) - len(phrase) + 1):
            if tuple(words[i : i + len(phrase)]) == phrase:
                found.append((run[i][1], run[i + len(phrase) - 1][2]))
    return found


def first_visible_match(
    raw_lines: Sequence[str], shown_lines: Sequence[str], terms: FullTextTerms
) -> Optional[VisibleMatch]:
    """Where the shown document first holds the first required word, with
    the shown occurrence count of all required words, when it holds every
    required word; else None. The whole document is read (the count)."""
    counts = [0] * (1 if terms.fuzzy_word is not None else len(terms.phrases))
    first: Optional[VisibleMatch] = None
    for number, (raw, shown) in enumerate(zip(raw_lines, shown_lines), start=1):
        runs = _visible_runs(raw, shown)
        for index in range(len(counts)):
            spans = _matches(runs, terms, index)
            if index == 0 and spans and first is None:
                first = VisibleMatch(number, shown[spans[0][0] : spans[0][1]])
            counts[index] += len(spans)
    if first is None or not all(counts):
        return None
    return VisibleMatch(first.line, first.text, sum(counts))
