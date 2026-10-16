"""#2047: standalone ``cidx query --file-extensions``.

Several extensions are OR-ed (they used to be AND-ed, so ``py,md`` returned
nothing), and ``--fts`` honours the flag (it used to ignore it), intersected
with ``--language``.

Drives the real ``cidx query`` command over the real on-disk semantic and FTS
indexes of the extension_filter_env_2047 corpus; only the embedding provider
(external service) is replaced.
"""

from __future__ import annotations

import gc
import re
from pathlib import Path
from typing import Iterator, List
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.unit.server.query.extension_filter_env_2047 import (
    CORPUS,
    LIMIT,
    QUERY,
    RankedEmbeddingProvider,
    build_corpus_repo,
    expected_fts,
    expected_semantic,
)

_ROW = re.compile(r"^\s*\d+\.\s")
_CORPUS_PATHS = sorted((p for p, _ in CORPUS), key=len, reverse=True)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_corpus_repo(tmp_path_factory.mktemp("cli-ext-2047"))


@pytest.fixture
def run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator:
    from code_indexer.cli import cli

    monkeypatch.chdir(repo)

    def _run(*args: str) -> List[str]:
        with patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            return_value=RankedEmbeddingProvider(),
        ):
            # --rerank-query "": no reranker stage (an external service).
            result = CliRunner().invoke(
                cli,
                ["query", QUERY, "--quiet", "--limit", str(LIMIT)]
                + ["--rerank-query", "", *args],
            )
        exit_code, output = result.exit_code, result.output
        # The CLI FTS path opens the index with a writer and never closes it
        # (a real CLI process exits). The result's SystemExit traceback keeps
        # that writer alive: drop it, then collect, to release the lock
        # before the next in-process invocation.
        del result
        gc.collect()
        assert exit_code == 0, output
        rows = []
        for line in output.splitlines():
            if _ROW.match(line):
                rows.append(next(p for p in _CORPUS_PATHS if p in line))
        return rows

    yield _run


@pytest.mark.parametrize("raw", ["py,md", ".PY,.MD"])
def test_semantic_several_extensions_are_ored(run, raw) -> None:
    got = run("--file-extensions", raw)
    assert sorted(got) == sorted(expected_semantic(["py", "md"], None))


def test_fts_honours_file_extensions(run, repo) -> None:
    got = run("--fts", "--file-extensions", "py,md")
    assert got == expected_fts(repo, ["py", "md"], None)


@pytest.mark.parametrize("mode", [[], ["--fts"]], ids=["semantic", "fts"])
def test_blank_extension_token_is_rejected(repo, monkeypatch, mode) -> None:
    from code_indexer.cli import cli

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        cli, ["query", QUERY, "--quiet", *mode, "--file-extensions", "py, "]
    )
    output = result.output
    exit_code = result.exit_code
    del result
    gc.collect()
    assert exit_code != 0, output
    assert "Invalid file extension ' ': it is empty" in output


def test_fts_extensions_intersect_with_language(run, repo) -> None:
    got = run("--fts", "--file-extensions", "PY,md", "--language", "python")
    assert got == expected_fts(repo, ["py", "md"], "python")
    assert got and all(p.endswith(".py") for p in got)


def test_selective_language_filter_fills_the_limit(run) -> None:
    """A filtered semantic query searches the shared filtered candidate
    window: the Python files rank below 19 others, yet the limit fills."""
    # The language filter keeps its own (case-preserving) value match.
    python_by_rank = [
        p for p, _ in sorted(CORPUS, key=lambda e: e[1]) if p.endswith(".py")
    ]
    assert run("--language", "python") == python_by_rank[:LIMIT]


def test_hybrid_applies_file_extensions(run) -> None:
    got = run("--fts", "--semantic", "--file-extensions", "MD")
    assert got and all(p.lower().endswith(".md") for p in got), got


def test_daemon_mode_forwards_file_extensions(repo, monkeypatch) -> None:
    """Daemon mode hands --file-extensions to the daemon process (whose
    search applies it: tests/unit/daemon/test_daemon_file_extensions_2047.py).
    Replaced: only the cross-process RPC boundary, which is recorded."""
    from code_indexer.cli import cli
    from code_indexer.config import ConfigManager

    monkeypatch.chdir(repo)
    forwarded: List[dict] = []

    def record(**kwargs):
        forwarded.append(kwargs)
        return 0

    with (
        patch.object(
            ConfigManager,
            "get_daemon_config",
            return_value={**ConfigManager.DAEMON_DEFAULTS, "enabled": True},
        ),
        patch("code_indexer.cli_daemon_delegation._query_via_daemon", record),
    ):
        result = CliRunner().invoke(
            cli, ["query", QUERY, "--quiet", "--file-extensions", ".PY,md"]
        )
    assert result.exit_code == 0, result.output
    assert len(forwarded) == 1
    assert forwarded[0]["file_extensions"] == ["py", "md"]
