"""Full-text queries re-evaluated on the text one restricted caller sees.

A restricted caller's full-text hit on a catalog document is kept only
when every required word of the query occurs in the text the caller is
shown, so whether a document appears depends only on that text. A word is
the engine's own unit: the sanitized query split on whitespace, each word
reduced to its tokens exactly as the index analyzes text (``engine_tokens``:
tantivy's ``default`` tokenizer of the content fields, i.e. the simple
tokenizer, tokens of 40 bytes or more dropped, lower-cased), and matched as
a contiguous phrase within one line. Only the shown text is read, never
the raw document: a placeholder or any other text the caller is shown is
ordinary shown text, so the verdict is a function of the shown text alone,
computed in time linear in its size.

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
from functools import lru_cache
from typing import (
    Any,
    FrozenSet,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
)

FULLTEXT_FORM_REFUSAL = "this full-text query form is not available for this repository"
FULLTEXT_TOO_BROAD = (
    "this full-text query matches too many documents in this repository; "
    "use more specific words"
)

_OPERATORS = frozenset({"AND", "OR", "NOT"})
_GRAMMAR_CHARS = frozenset("*^~")
# The engine drops a token of this many bytes or more (remove_long).
_MAX_TOKEN_BYTES = 40
_ASCII_RUN = re.compile(r"[A-Za-z0-9]+")

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
    is asked for it, its token phrase (original case when case-sensitive),
    the single fuzzy word with its distance (None without fuzzy), and the
    anchors: engine queries for tokens projection cannot introduce, so any
    document showing every word holds an anchor in its raw text (empty when
    every token could be introduced)."""

    words: Tuple[str, ...]
    phrases: Tuple[Tuple[str, ...], ...]
    fuzzy_word: Optional[str] = None
    edit_distance: int = 0
    case_sensitive: bool = False
    anchors: Tuple[str, ...] = field(default=(), compare=False)

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


def raise_fulltext_too_broad() -> NoReturn:
    """Refuse a query whose catalog candidates cannot be gathered whole."""
    from ...query.semantic_query_manager import SearchParameterError

    raise SearchParameterError(FULLTEXT_TOO_BROAD)


@lru_cache(maxsize=1)
def introducible_tokens() -> FrozenSet[str]:
    """The tokens projection can add to a document outside the
    synthesizing set besides numbers: those of the marker strings."""
    from .depmap_projection import DOMAIN_PLACEHOLDER, RESTRICTED_PLACEHOLDER

    return frozenset(
        str(form)
        for marker in (RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER)
        for form, _, _ in engine_tokens(marker)
        if form
    )


def _introducible(token: str) -> bool:
    return token.isdigit() or token in introducible_tokens()


def _fuzzy_may_match_introducible(word: str, distance: int) -> bool:
    """True when *word* is within *distance* of a marker token or could
    become a number by editing its non-digit characters."""
    if sum(not ch.isdigit() for ch in word) <= distance:
        return True
    return any(_within_distance(word, t, distance) for t in introducible_tokens())


def _anchors(
    words: Sequence[str], forms: Sequence[Sequence[str]], edit_distance: int
) -> Tuple[str, ...]:
    if edit_distance > 0:
        fuzzy = words[0]
        if _fuzzy_may_match_introducible(fuzzy.lower(), edit_distance):
            return ()
        return (fuzzy,)
    safe = [t for word in forms for t in word if not _introducible(t)]
    return tuple(dict.fromkeys(safe))


def parse_fulltext_terms(
    query: str, edit_distance: int, case_sensitive: bool = False
) -> Optional[FullTextTerms]:
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
    tokens = [engine_tokens(word) for word in words]
    if not all(tokens) or any(f is None for t in tokens for f, _, _ in t):
        return None
    forms = [[str(f) for f, _, _ in t] for t in tokens]
    phrases = tuple(
        tuple(word[s:e] for _, s, e in t) if case_sensitive else tuple(f)
        for word, t, f in zip(words, tokens, forms)
    )
    if edit_distance > 0 and len(words) != 1:
        return None
    return FullTextTerms(
        tuple(words),
        phrases,
        words[0] if edit_distance > 0 else None,
        edit_distance,
        case_sensitive,
        _anchors(words, forms, edit_distance),
    )


def _shown_runs(shown: str, case_sensitive: bool = False) -> List[List[Token]]:
    """The shown line's engine tokens, in runs of adjacent tokens (a token
    the engine drops ends a run, as it breaks a phrase in the index); a
    case-sensitive query compares each token's original text."""
    runs: List[List[Token]] = []
    run: List[Token] = []
    for form, start, end in engine_tokens(shown):
        if form is None:
            if run:
                runs.append(run)
            run = []
        else:
            run.append((shown[start:end] if case_sensitive else form, start, end))
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
    shown_lines: Sequence[str], terms: FullTextTerms
) -> Optional[VisibleMatch]:
    """Where the shown document first holds the first required word, with
    the shown occurrence count of all required words, when it holds every
    required word; else None. Only the shown text is read, once."""
    counts = [0] * (1 if terms.fuzzy_word is not None else len(terms.phrases))
    first: Optional[VisibleMatch] = None
    for number, shown in enumerate(shown_lines, start=1):
        runs = _shown_runs(shown, terms.case_sensitive)
        for index in range(len(counts)):
            spans = _matches(runs, terms, index)
            if index == 0 and spans and first is None:
                first = VisibleMatch(number, shown[spans[0][0] : spans[0][1]])
            counts[index] += len(spans)
    if first is None or not all(counts):
        return None
    return VisibleMatch(first.line, first.text, sum(counts))
