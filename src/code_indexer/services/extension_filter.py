"""One ``file_extensions`` rule for every search mode and front door (#2047).

The rule:

* each value is stripped, lowercased and loses ONE leading dot, so ``py``,
  ``.py``, ``PY`` and ``.PY`` are the same value;
* a value that is then empty, or contains ``.`` or ``/`` (so it can never be
  a file suffix), is rejected with ValueError -- the same error at REST, MCP
  and the CLI;
* a file matches iff its lowercased suffix (without the dot) is one of the
  values -- several values are OR-ed;
* a file without a suffix (``Makefile``, ``.bashrc``) matches no extension
  filter, in every mode (the FTS index stores such files as ``txt``; the
  suffix check below is what keeps them out of a ``txt`` filter);
* a ``language`` / ``exclude_language`` filter keeps its own semantics and the
  two results are intersected (a file must pass both).

Where it is applied:

* FTS and the FTS half of hybrid push the values down into Tantivy as a
  score-neutral OR group on the existing ``language`` text field (the default
  tokenizer lowercases it, so ``PY`` matches; no schema change, no re-index),
  then post-verify every hit with :func:`path_matches_extensions`.
* Semantic search (standalone CLI, daemon and server) pushes the values into
  the vector store as ONE ``any_ext`` filter condition
  (:func:`vector_store_extension_condition`), composed with the language /
  path / exclude conditions; every mode runs it as one store query over the
  same widened candidate window (services/filtered_window).

Pure Python, import-light: the CLI, storage and server layers all import it.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

# Tantivy's default tokenizer drops tokens longer than this many bytes.
_TANTIVY_MAX_TOKEN_LEN = 40


def normalize_extension(raw: str) -> str:
    """One requested extension under the rule, or ValueError."""
    value = str(raw).strip().lower()
    if value.startswith("."):
        value = value[1:]
    if not value:
        raise ValueError(f"Invalid file extension {raw!r}: it is empty")
    if "." in value or "/" in value:
        raise ValueError(
            f"Invalid file extension {raw!r}: a value containing '.' or '/' "
            "can never be a file extension (only the part after the last dot "
            "is one)"
        )
    return value


def normalize_extensions(
    values: Optional[Iterable[str]],
) -> Optional[FrozenSet[str]]:
    """Normalize requested extensions; ``None`` means "no extension filter".

    ``None`` or an empty list is no filter. Every value goes through
    :func:`normalize_extension`; an invalid one is rejected rather than
    silently widening or emptying the answer.
    """
    if values is None:
        return None
    normalized = {normalize_extension(raw) for raw in values}
    return frozenset(normalized) if normalized else None


def validate_file_extensions_field(
    values: Optional[List[str]],
) -> Optional[List[str]]:
    """The single validator for a request's ``file_extensions`` (REST
    single-repo and multi-repo request models, and the server's search entry
    for MCP's raw parameters): ``None`` or ``[]`` is no filter; anything but
    a list is rejected (a bare string would otherwise be read character by
    character); every value must pass :func:`normalize_extension`
    (ValueError -> 422 / MCP error); the stripped values are kept as sent."""
    if values is not None and not isinstance(values, (list, tuple)):
        raise ValueError(
            f"file_extensions must be a list of strings, got {type(values).__name__}"
        )
    if not values:
        return None
    for raw in values:
        normalize_extension(raw)
    return [str(raw).strip() for raw in values]


def path_matches_extensions(path: str, extensions: FrozenSet[str]) -> bool:
    """True iff *path*'s lowercased suffix (without its dot) is requested."""
    suffix = PurePosixPath(path).suffix
    return bool(suffix) and suffix[1:].lower() in extensions


def fts_pushdown_terms(extensions: FrozenSet[str]) -> Optional[List[str]]:
    """The terms to push into Tantivy's ``language`` field, or ``None``.

    Only an ASCII alphanumeric value is exactly one token of the default
    tokenizer; if any value is not (``c++``, ``foo-bar``), nothing is pushed
    down and the caller post-filters only (correct; the caller's bounded
    fill loop keeps the answer full).
    """
    terms = sorted(extensions)
    if all(
        t.isascii() and t.isalnum() and len(t) <= _TANTIVY_MAX_TOKEN_LEN for t in terms
    ):
        return terms
    return None


def vector_store_extension_condition(
    values: Optional[Iterable[str]],
) -> Optional[Dict[str, Any]]:
    """The requested values as ONE vector-store filter condition, or ``None``
    when there is no filter -- the single builder the CLI, the daemon and the
    server compose into their ``must`` conditions (so it intersects with
    language / path / exclude). The store's ``any_ext`` operator applies
    :func:`path_matches_extensions` to each chunk's ``path`` payload -- OR
    over the values, case-insensitive, extensionless files never match.
    Invalid values raise ValueError (:func:`normalize_extension`)."""
    extensions = normalize_extensions(values)
    if extensions is None:
        return None
    return {"key": "path", "match": {"any_ext": sorted(extensions)}}
