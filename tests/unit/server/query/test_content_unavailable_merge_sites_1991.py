"""Bug #1991: QueryResult rebuild sites keep the content_unavailable signal.

The hybrid RRF merge and the multimodal supplement both construct NEW
QueryResult objects; neither may drop the flag the store reported.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.models.api_models import QueryResultItem, SearchResultItem
from code_indexer.server.query.semantic_query_manager import (
    QueryResult,
    SemanticQueryManager,
)


def _manager(tmp_path: Path) -> SemanticQueryManager:
    return SemanticQueryManager(
        data_dir=str(tmp_path / "server-data"),
        activated_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )


def _row(path: str, unavailable: bool) -> QueryResult:
    return QueryResult(
        file_path=path,
        line_number=1,
        code_snippet="" if unavailable else "code",
        similarity_score=0.9,
        repository_alias="example-repo",
        content_unavailable=unavailable,
    )


def test_hybrid_merge_preserves_content_unavailable(tmp_path):
    merged = _manager(tmp_path)._merge_hybrid_results(
        fts_results=[_row("a.py", False)],
        semantic_results=[_row("Broken.cs", True), _row("a.py", False)],
        limit=10,
    )

    flags = {r.file_path: r.content_unavailable for r in merged}
    assert flags == {"Broken.cs": True, "a.py": False}
    broken = next(r for r in merged if r.file_path == "Broken.cs")
    assert broken.to_dict()["content_unavailable"] is True


def test_multimodal_supplement_preserves_content_unavailable(tmp_path):
    item = SearchResultItem(
        score=0.8,
        file_path="docs/diagram.png",
        line_start=1,
        line_end=1,
        content="",
        language=None,
        file_last_modified=None,
        indexed_timestamp=None,
        content_unavailable=True,
    )
    with patch(
        "code_indexer.server.services.search_service.SemanticSearchService"
        ".query_multimodal_only",
        return_value=[item],
    ):
        merged = _manager(tmp_path)._merge_multimodal_supplement(
            repo_path=str(tmp_path),
            query_text="diagram",
            limit=10,
            results=[],
            repository_alias="example-repo",
            min_score=None,
            file_extensions=None,
            language=None,
            exclude_language=None,
            path_filter=None,
            exclude_path=None,
            accuracy=None,
            activation_id=None,
        )

    assert [r.content_unavailable for r in merged] == [True]


def test_composite_cli_output_marker_maps_to_flag(tmp_path):
    cli_output = (
        "0.95 repo1/Broken.cs:1-3\n"
        "  [content unavailable: file could not be read]\n"
        "\n"
        "0.85 repo2/good.py:5-6\n"
        "  5: def ok():\n"
        "  6:     return 1\n"
    )

    results = _manager(tmp_path)._parse_cli_output(cli_output, repo_path=tmp_path)

    rows = {r.file_path: r for r in results}
    assert rows["repo1/Broken.cs"].content_unavailable is True
    assert rows["repo1/Broken.cs"].code_snippet == ""
    assert rows["repo2/good.py"].content_unavailable is False
    assert "def ok" in rows["repo2/good.py"].code_snippet


def test_rest_result_item_model_keeps_flag():
    # The REST hybrid path builds QueryResultItem(**QueryResult.to_dict()).
    flagged = QueryResultItem(**_row("Broken.cs", True).to_dict())
    assert flagged.content_unavailable is True

    normal_dict = _row("a.py", False).to_dict()
    assert "content_unavailable" not in normal_dict
    normal = QueryResultItem(**normal_dict)
    assert normal.content_unavailable is False

    # Serialized only when true; every other field (defaults included) stays.
    assert "content_unavailable" not in normal.model_dump()
    assert "content_unavailable" not in normal.model_dump_json()
    assert normal.model_dump()["file_last_modified"] is None
    assert flagged.model_dump()["content_unavailable"] is True


def test_result_models_keep_typed_serialization_schema():
    from code_indexer.server.models.api_models import SemanticSearchResponse

    search_props = SearchResultItem.model_json_schema(mode="serialization")[
        "properties"
    ]
    assert {"score", "file_path", "content", "content_unavailable"} <= set(search_props)
    query_props = QueryResultItem.model_json_schema(mode="serialization")["properties"]
    assert {
        "file_path",
        "similarity_score",
        "code_snippet",
        "content_unavailable",
    } <= set(query_props)

    # The OpenAPI response model nesting SearchResultItem stays typed too.
    response_defs = SemanticSearchResponse.model_json_schema(mode="serialization")[
        "$defs"
    ]
    assert "score" in response_defs["SearchResultItem"]["properties"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_search_result_item_serializes_flag_only_when_true(unavailable):
    item = SearchResultItem(
        score=0.5,
        file_path="a.py",
        line_start=1,
        line_end=2,
        content="",
        language=None,
        file_last_modified=None,
        indexed_timestamp=None,
        content_unavailable=unavailable,
    )
    dumped = item.model_dump()
    assert ("content_unavailable" in dumped) is unavailable
    assert dumped["language"] is None
