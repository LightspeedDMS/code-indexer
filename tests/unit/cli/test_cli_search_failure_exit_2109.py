"""``cidx query`` reports a failed search and exits non-zero, even with
``--quiet``: a failure is never printed as a warning followed by an empty,
successful result.

Drives the real ``cidx query`` command over the real on-disk semantic and FTS
indexes of the extension_filter_env_2047 corpus; replaced: the embedding
provider (external service) and, to make the search fail the way storage
does, the store search call.
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.unit.server.query.extension_filter_env_2047 import (
    QUERY,
    RankedEmbeddingProvider,
    build_corpus_repo,
)

STORE_SEARCH = (
    "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore.search"
)
FAILURE = "index storage is unavailable"


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_corpus_repo(tmp_path_factory.mktemp("cli-failure-2109"))


def _invoke(mode: List[str]) -> tuple:
    from code_indexer.cli import cli

    with (
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            return_value=RankedEmbeddingProvider(),
        ),
        patch(STORE_SEARCH, side_effect=OSError(FAILURE)),
    ):
        result = CliRunner().invoke(
            cli, ["query", QUERY, "--quiet", "--rerank-query", "", *mode]
        )
    exit_code, output = result.exit_code, result.output
    # Release the FTS writer the in-process CLI keeps alive (see 2047 test).
    del result
    gc.collect()
    return exit_code, output


@pytest.mark.parametrize(
    "mode", [[], ["--fts", "--semantic"]], ids=["semantic", "hybrid"]
)
def test_failed_search_exits_non_zero_with_error(repo, monkeypatch, mode) -> None:
    monkeypatch.chdir(repo)
    exit_code, output = _invoke(mode)
    assert exit_code != 0, output
    assert FAILURE in output
