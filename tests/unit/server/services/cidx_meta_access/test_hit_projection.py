"""Catalog search hits projected to the caller's view (real fixture, no mocks)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.cidx_meta_access.hit_projection import (
    CATALOG_HIT_FIELDS,
    TextRematch,
    open_catalog_scope,
    project_meta_hits,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path_factory.mktemp("hit-projection"))


def _scope(fx: CidxMetaFixture, user: str = RESTRICTED_USER) -> Any:
    return open_catalog_scope(user, fx.access_service, fx.root, fx.registry)


def _hit(fx: CidxMetaFixture, path: str, first: int, count: int) -> Dict[str, Any]:
    lines = read_lines(fx.root / path)[first - 1 : first - 1 + count]
    return {
        "file_path": path,
        "line_number": first,
        "code_snippet": "\n".join(lines),
        "similarity_score": 0.9,
        "repository_alias": "cidx-meta-global",
        "source_repo": "cidx-meta-global",
        "metadata": {"commit_message": f"touches {CANARY}"},
        "temporal_context": {"commits": [CANARY]},
        "future_field": CANARY,
    }


def _dump(rows: List[Any]) -> str:
    return json.dumps(rows, default=lambda o: o.__dict__).lower()


def test_admin_gets_no_catalog_scope(fx: CidxMetaFixture) -> None:
    assert _scope(fx, ADMIN_USER) is None


def test_meta_hits_carry_only_allow_listed_fields_and_projected_text(
    fx: CidxMetaFixture,
) -> None:
    hits = [
        _hit(fx, f"{ALPHA}.md", 1, 4),
        _hit(fx, f"{CANARY}.md", 1, 3),
        _hit(fx, ".code-indexer/index/meta.json", 1, 1),
        _hit(fx, "dependency-map/payments.md", 1, 60),
    ]
    out = project_meta_hits(hits, _scope(fx))
    dumped = _dump(out)
    assert CANARY not in dumped and GAMMA not in dumped
    assert [row["file_path"] for row in out] == [
        f"{ALPHA}.md",
        "dependency-map/payments.md",
    ]
    for row in out:
        assert set(row) <= CATALOG_HIT_FIELDS
    alpha = out[0]
    assert alpha["line_number"] == 1
    assert f"{ALPHA} talks to [restricted repository]" in alpha["code_snippet"]
    assert len(alpha["code_snippet"].split("\n")) == 4


def test_projection_is_idempotent(fx: CidxMetaFixture) -> None:
    scope = _scope(fx)
    hits = [
        _hit(fx, f"{ALPHA}.md", 1, 6),
        _hit(fx, "dependency-map/_index.md", 11, 36),
    ]
    once = project_meta_hits(hits, scope)
    twice = project_meta_hits(once, _scope(fx))
    assert once == twice
    assert "| payments |" in once[1]["code_snippet"]


def test_fts_hit_requires_match_in_projected_text(fx: CidxMetaFixture) -> None:
    canary_hit = dict(_hit(fx, f"{ALPHA}.md", 3, 1), match_text=CANARY)
    visible_hit = dict(_hit(fx, f"{ALPHA}.md", 3, 1), match_text="talks")
    scope = _scope(fx)
    assert project_meta_hits([canary_hit], scope, TextRematch(CANARY)) == []
    kept = project_meta_hits([visible_hit], scope, TextRematch("TALKS"))
    assert [row["match_text"] for row in kept] == ["talks"]
    # A row without match_text (REST FTS shape) is re-matched by query term.
    rest_row = {"path": f"{ALPHA}.md", "line_start": 0, "snippet": "alpha talks"}
    rest_row["snippet"] = read_lines(fx.root / f"{ALPHA}.md")[2]
    assert project_meta_hits([rest_row], scope, TextRematch(CANARY)) == []
    assert project_meta_hits([rest_row], scope, TextRematch("http"))[0]["path"] == (
        f"{ALPHA}.md"
    )


def test_regex_rematch_needs_the_engine_match_on_a_verbatim_line(
    fx: CidxMetaFixture,
) -> None:
    scope = _scope(fx)
    rematch = TextRematch("(?<=x", regex=True)  # never evaluated
    changed = _hit(fx, f"{ALPHA}.md", 3, 1)
    verbatim = _hit(fx, f"{ALPHA}.md", 6, 1)
    assert project_meta_hits([changed], scope, rematch) == []
    assert project_meta_hits([dict(changed, match_text="talks")], scope, rematch) == []
    kept = project_meta_hits([dict(verbatim, match_text="ordering")], scope, rematch)
    assert [row["match_text"] for row in kept] == ["ordering"]


def test_partial_edge_lines_survive_only_inside_their_projected_line(
    fx: CidxMetaFixture,
) -> None:
    # A character-cut chunk: the first line is a suffix, the last a prefix.
    hit = dict(
        _hit(fx, f"{ALPHA}.md", 3, 1),
        code_snippet="talks to beta-team-sample and gamma over HTTP.\n"
        "It mirrors omega-sample nightly.\nThe zed ad",
    )
    out = project_meta_hits([hit], _scope(fx))
    assert len(out) == 1
    lines = out[0]["code_snippet"].split("\n")
    assert lines[0] == ""  # the redacted line no longer contains the fragment
    assert lines[2] == "The zed ad"
    assert CANARY not in out[0]["code_snippet"]


def test_dataclass_hits_lose_temporal_fields(fx: CidxMetaFixture) -> None:
    from code_indexer.server.query.semantic_query_manager import QueryResult

    raw = _hit(fx, f"{ALPHA}.md", 1, 4)
    row = QueryResult(
        file_path=raw["file_path"],
        line_number=1,
        code_snippet=raw["code_snippet"],
        similarity_score=0.5,
        repository_alias="cidx-meta-global",
        metadata={"commit_message": CANARY},
        temporal_context={"commits": [CANARY]},
    )
    setattr(row, "_temporal_context", {"x": CANARY})
    (out,) = project_meta_hits([row], _scope(fx))
    assert isinstance(out, QueryResult)
    assert out.metadata is None and out.temporal_context is None
    assert not hasattr(out, "_temporal_context")
    assert CANARY not in out.code_snippet


def test_every_text_field_of_a_hit_is_projected(fx: CidxMetaFixture) -> None:
    lines = read_lines(fx.root / f"{ALPHA}.md")
    hit = dict(
        _hit(fx, f"{ALPHA}.md", 1, 1),
        content="\n".join(lines[2:4]),
        snippet=f"{CANARY} not in this document",
        match_text=CANARY,
    )
    out = project_meta_hits([hit], _scope(fx))
    assert len(out) == 1
    dumped = _dump(out)
    assert CANARY not in dumped and GAMMA not in dumped
    assert "match_text" not in out[0] and "snippet" not in out[0]
    assert out[0]["content"].startswith(f"{ALPHA} talks to [restricted repository]")


def test_stale_chunk_not_in_document_is_dropped(fx: CidxMetaFixture) -> None:
    hit = dict(_hit(fx, f"{ALPHA}.md", 1, 1), code_snippet=f"old text {CANARY}")
    assert project_meta_hits([hit], _scope(fx)) == []
