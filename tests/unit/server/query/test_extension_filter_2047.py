"""#2047 (S21): the extension rule helper and the merge sites that use it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.models.api_models import SearchResultItem
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.services.extension_filter import (
    fts_pushdown_terms,
    normalize_extensions,
    path_matches_extensions,
    validate_file_extensions_field,
    vector_store_extension_condition,
)

# ------------------------------------------------------------- the rule


@pytest.mark.parametrize("values", [None, []])
def test_no_values_is_no_filter(values) -> None:
    assert normalize_extensions(values) is None


def test_values_are_lowercased_and_lose_one_leading_dot() -> None:
    assert normalize_extensions([" .PY ", "md", ".Js"]) == frozenset({"py", "md", "js"})


@pytest.mark.parametrize("blank", ["", "  ", ".", " . "])
def test_blank_value_is_rejected(blank) -> None:
    with pytest.raises(ValueError, match="empty"):
        normalize_extensions(["py", blank])


@pytest.mark.parametrize("never", ["tar.gz", "..py", "a/b", ".x.y"])
def test_value_that_can_never_be_a_suffix_is_rejected(never) -> None:
    with pytest.raises(ValueError, match="never be a file extension"):
        normalize_extensions(["py", never])


def test_symbol_extensions_are_accepted() -> None:
    assert normalize_extensions(["c++", ".Foo-Bar", "a_b"]) == frozenset(
        {"c++", "foo-bar", "a_b"}
    )


@pytest.mark.parametrize("not_a_list", ["py", {"py": 1}, 7])
def test_field_validator_rejects_a_non_list(not_a_list) -> None:
    """A bare string would otherwise be read character by character."""
    with pytest.raises(ValueError, match="must be a list"):
        validate_file_extensions_field(not_a_list)


def test_field_validator_accepts_lists() -> None:
    assert validate_file_extensions_field(None) is None
    assert validate_file_extensions_field([]) is None
    assert validate_file_extensions_field([" .PY ", "md"]) == [".PY", "md"]


@pytest.mark.parametrize(
    "path, expected",
    [
        ("pkg/m.py", True),
        ("pkg/M.PY", True),
        ("a/b.c/Makefile", False),
        (".bashrc", False),
        ("notes/x.txt", False),
        ("archive.tar.gz", False),
    ],
)
def test_path_matches_on_lowercased_suffix_only(path, expected) -> None:
    assert path_matches_extensions(path, frozenset({"py"})) is expected


def test_pushdown_terms_are_sorted_alphanumeric_values() -> None:
    assert fts_pushdown_terms(frozenset({"py", "md"})) == ["md", "py"]


@pytest.mark.parametrize("odd", ["c++", "foo-bar", "a_b", "pÿ", "x" * 41])
def test_no_pushdown_when_a_value_is_not_one_token(odd) -> None:
    assert fts_pushdown_terms(frozenset({"py", odd})) is None


# ------------------------------------------- the shared store condition


def test_vector_store_condition_uses_any_ext() -> None:
    condition = vector_store_extension_condition(frozenset({"py", "md"}))
    assert condition == {"key": "path", "match": {"any_ext": ["md", "py"]}}


def test_vector_store_condition_normalizes_raw_values_or_is_none() -> None:
    """The ONE builder the CLI, daemon and server share takes the request's
    raw values: no values -> no condition; raw values go through the rule."""
    assert vector_store_extension_condition(None) is None
    assert vector_store_extension_condition([]) is None
    assert vector_store_extension_condition([".PY", " md "]) == {
        "key": "path",
        "match": {"any_ext": ["md", "py"]},
    }
    with pytest.raises(ValueError):
        vector_store_extension_condition(["py", "tar.gz"])


def test_server_filter_conditions_compose_any_ext_with_language() -> None:
    from code_indexer.server.services.search_service import SemanticSearchService
    from code_indexer.services.language_mapper import LanguageMapper

    build = SemanticSearchService()._build_filter_conditions
    language_condition = LanguageMapper().build_language_filter("python")

    assert build(None, "python", None, None) == {"must": [language_condition]}
    assert build(None, "python", None, None, file_extensions=[".PY"]) == {
        "must": [
            language_condition,
            {"key": "path", "match": {"any_ext": ["py"]}},
        ]
    }


def test_filtered_window_is_empty_without_filters_and_widened_with_any() -> None:
    from code_indexer.server.models.api_models import MAX_CANDIDATE_LIMIT
    from code_indexer.server.services import search_service
    from code_indexer.services.filtered_window import filtered_window_kwargs

    # ONE definition: the server uses the rule the CLI and daemon share.
    assert search_service.filtered_window_kwargs is filtered_window_kwargs

    widened = {"prefetch_limit": 2 * MAX_CANDIDATE_LIMIT, "lazy_load": True}
    assert filtered_window_kwargs({}, MAX_CANDIDATE_LIMIT) == {}
    assert filtered_window_kwargs(None, 10) == {}
    language_only = {"must": [{"key": "language", "match": {"value": "py"}}]}
    assert filtered_window_kwargs(language_only, MAX_CANDIDATE_LIMIT) == widened
    extension_only = {"must": [vector_store_extension_condition(["py"])]}
    assert filtered_window_kwargs(extension_only, 3) == widened
    # A CLI store limit above the server cap never narrows the window.
    assert filtered_window_kwargs(language_only, 1000) == {
        "prefetch_limit": 2000,
        "lazy_load": True,
    }


@pytest.mark.parametrize(
    "path, expected",
    [
        ("pkg/a.Py", True),
        ("pkg/A.PY", True),
        ("docs/b.md", True),
        ("Makefile", False),
        ("x.js", False),
        (["x.js", "y.MD"], True),
        (["x.js"], False),
        ([], False),
        (None, False),
        (7, False),
    ],
)
def test_store_any_ext_operator(tmp_path: Path, path, expected) -> None:
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(base_path=tmp_path / "index", project_root=tmp_path)
    condition = vector_store_extension_condition(frozenset({"py", "md"}))
    evaluate = store._parse_filter({"must": [condition]})
    assert evaluate({"path": path}) is expected


# ------------------------------------------------- multimodal merge site


def _item(path: str, score: float) -> SearchResultItem:
    return SearchResultItem(
        score=score,
        file_path=path,
        line_start=1,
        line_end=1,
        content="x",
        language=None,
        file_last_modified=None,
        indexed_timestamp=None,
    )


def test_multimodal_supplement_applies_extension_rule(tmp_path: Path) -> None:
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "server-data"),
        activated_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )
    items = [
        _item("docs/diagram.PNG", 0.9),
        _item("docs/photo.jpg", 0.8),
        _item("LICENSE", 0.7),
    ]
    with patch(
        "code_indexer.server.services.search_service.SemanticSearchService"
        ".query_multimodal_only",
        return_value=items,
    ) as supplement:
        merged = manager._merge_multimodal_supplement(
            repo_path=str(tmp_path),
            query_text="diagram",
            limit=10,
            results=[],
            repository_alias="example-repo",
            min_score=None,
            file_extensions=["png"],
            language=None,
            exclude_language=None,
            path_filter=None,
            exclude_path=None,
            accuracy=None,
            activation_id=None,
        )

    assert [r.file_path for r in merged] == ["docs/diagram.PNG"]
    # The store query itself carries the extensions (push-down).
    assert supplement.call_args.kwargs["file_extensions"] == ["png"]


def test_multimodal_only_query_leaves_the_window_to_the_service() -> None:
    """The multimodal-only query pushes file_extensions down; the filtered
    candidate window is applied by MultiIndexQueryService from the store limit
    it sends (test_multi_index_filtered_window_2047), never by this caller.
    Replaced: backend/provider factories, the repo config read and the
    multi-index service (the store boundary, recorded)."""
    import code_indexer.server.services.search_service as ss_mod
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = MagicMock(spec=FilesystemVectorStore)
    store.resolve_collection_name.return_value = "coll"
    multi = MagicMock()
    multi.has_multimodal_index.return_value = True
    multi.query_multimodal_index_only.return_value = ([], {})
    with (
        patch.object(ss_mod, "_load_repo_config", return_value={}),
        patch.object(ss_mod, "BackendFactory") as factory,
        patch.object(ss_mod, "EmbeddingProviderFactory"),
        patch(
            "code_indexer.services.multi_index_query_service.MultiIndexQueryService",
            return_value=multi,
        ),
    ):
        factory.create.return_value.get_vector_store_client.return_value = store
        ss_mod.SemanticSearchService().query_multimodal_only(
            "/fake/repo", "q", 5, file_extensions=["PNG"]
        )
    kwargs = multi.query_multimodal_index_only.call_args.kwargs
    assert kwargs["filter_conditions"] == {
        "must": [{"key": "path", "match": {"any_ext": ["png"]}}]
    }
    assert "prefetch_limit" not in kwargs
    assert "lazy_load" not in kwargs


def test_server_multimodal_query_window_follows_the_store_limit(
    tmp_path: Path,
) -> None:
    """A filtered server query on a repo with a multimodal collection goes
    through the real MultiIndexQueryService: each store call asks for
    2 x 200 results over a 2 x 400 window. Replaced: backend/provider
    factories, the repo config read, the multimodal provider and the store
    (the boundary, recorded)."""
    import code_indexer.server.services.search_service as ss_mod
    from code_indexer.services.multi_index_query_service import (
        MultiIndexQueryService,
    )
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    (tmp_path / ".code-indexer" / "index" / "voyage-multimodal-3").mkdir(parents=True)
    store = MagicMock(spec=FilesystemVectorStore)
    store.resolve_collection_name.return_value = "coll"
    calls: list = []

    def record(**kw):
        calls.append(kw)
        return [], {}

    store.search.side_effect = record
    with (
        patch.object(ss_mod, "_load_repo_config", return_value={}),
        patch.object(ss_mod, "BackendFactory") as factory,
        patch.object(ss_mod, "EmbeddingProviderFactory"),
        patch.object(
            MultiIndexQueryService, "_get_multimodal_provider", return_value=object()
        ),
    ):
        factory.create.return_value.get_vector_store_client.return_value = store
        ss_mod.SemanticSearchService()._perform_semantic_search(
            str(tmp_path),
            "q",
            200,
            False,
            hnsw_cache=None,
            enable_multimodal=True,
            file_extensions=["py"],
        )
    assert len(calls) == 2
    assert all(c["limit"] == 400 for c in calls)
    assert all(c["prefetch_limit"] == 800 for c in calls)
    assert all(c["lazy_load"] is True for c in calls)


def test_non_filesystem_store_rejects_filter_conditions() -> None:
    """The sequential (non-FilesystemVectorStore) branch cannot apply
    filter conditions: a filtered query fails loudly instead of returning an
    unfiltered answer. Replaced: the backend factory (no such store exists in
    production) and the repo config read."""
    import code_indexer.server.services.search_service as ss_mod

    store = MagicMock()
    store.resolve_collection_name.return_value = "coll"
    with (
        patch.object(ss_mod, "_load_repo_config", return_value={}),
        patch.object(ss_mod, "BackendFactory") as factory,
        patch.object(ss_mod, "EmbeddingProviderFactory"),
    ):
        factory.create.return_value.get_vector_store_client.return_value = store
        with pytest.raises(RuntimeError, match="filter conditions"):
            ss_mod.SemanticSearchService()._perform_semantic_search(
                "/fake/repo", "q", 5, False, file_extensions=["py"]
            )
    store.search.assert_not_called()
