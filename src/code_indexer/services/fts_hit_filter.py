"""The per-hit filters of a full-text search, as one predicate.

Moved out of ``TantivyIndexManager.search`` so a caller that judges paths
outside the engine (the restricted catalog page) applies the request
filters with exactly the engine's semantics. Precedence: allowed paths,
language exclusions, language inclusions, the file-extension re-check on
the path, path exclusions, path inclusions. Languages are dot-less extensions, as
``LanguageMapper.get_extensions`` returns them and the indexer stores them.
"""

from pathlib import PurePosixPath
from typing import Callable, Collection, List, Optional, Set

HitFilter = Callable[[str, Optional[str]], bool]


def indexed_language(path: str) -> str:
    """The language the indexer records for *path* (its suffix, or txt)."""
    return PurePosixPath(path).suffix.lstrip(".") or "txt"


def build_hit_filter(
    *,
    allowed_paths: Optional[Collection[str]] = None,
    languages: Optional[List[str]] = None,
    exclude_languages: Optional[List[str]] = None,
    path_filters: Optional[List[str]] = None,
    exclude_paths: Optional[List[str]] = None,
    file_extensions: Optional[List[str]] = None,
) -> HitFilter:
    """The predicate ``(path, language) -> kept`` for these filters."""
    from code_indexer.services.extension_filter import (
        normalize_extensions,
        path_matches_extensions,
    )

    allowed_set = None if allowed_paths is None else frozenset(allowed_paths)
    extension_set = normalize_extensions(file_extensions)
    allowed_extensions: Set[str] = set()
    excluded_extensions: Set[str] = set()
    if languages or exclude_languages:
        from code_indexer.services.language_mapper import LanguageMapper

        mapper = LanguageMapper()
        for lang in exclude_languages or []:
            excluded_extensions.update(mapper.get_extensions(lang) or ())
        for lang in languages or []:
            allowed_extensions.update(mapper.get_extensions(lang) or ())
    matcher = None
    if path_filters or exclude_paths:
        from code_indexer.services.path_pattern_matcher import PathPatternMatcher

        matcher = PathPatternMatcher()

    def passes(path: str, language: Optional[str]) -> bool:
        if allowed_set is not None and path not in allowed_set:
            return False
        if excluded_extensions and language in excluded_extensions:
            return False
        if allowed_extensions and language not in allowed_extensions:
            return False
        if extension_set and not path_matches_extensions(path, extension_set):
            return False
        if matcher and exclude_paths:
            if any(matcher.matches_pattern(path, p) for p in exclude_paths):
                return False
        if matcher and path_filters:
            if not any(matcher.matches_pattern(path, p) for p in path_filters):
                return False
        return True

    return passes
