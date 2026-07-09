"""Story #1291 code-review Finding 1 (BLOCKER) regression guard.

BUG: `TemporalIndexer.index_commits()` already builds shard sets for EVERY
configured embedder in `temporal.embedders` in a SINGLE call (Story #1291).
The pre-fix `cli.py` `index --index-commits` handler additionally ran a
leftover "additional-temporal-embedder loop" (Story #1290) AFTER the primary
call: it mutated `config.temporal.active_embedder` to promote the next
configured embedder to ACTIVE and called `index_commits()` a SECOND time on
a fresh `TemporalIndexer`.

In the "second embedder has no credentials" scenario (AC4) this double-runs
destructively: the first (correct) `index_commits()` call WARN-skips the
unavailable NON-ACTIVE embedder and returns success (AC4: WARN-and-continue
GREEN). The leftover loop then promotes that SAME unavailable embedder to
ACTIVE and calls `index_commits()` again, which hits the
active-embedder-unavailable branch and raises `RuntimeError` -- crashing
`cidx index` with exit code 1 exactly where AC4 requires the job to stay
GREEN.

This test drives the REAL CLI `index` command (no source-text inspection)
against two FAKE registered `TemporalEmbedder` adapters -- one always
available (the active embedder) and one registered-but-unavailable (mimics
"Cohere key removed", non-active) -- and asserts:
  - the command exits 0 (AC4)
  - the active embedder's shard is built on disk
  - the unavailable embedder is WARN-skipped (never invoked) and logs a
    WARNING naming it
  - `config.temporal.active_embedder` is left UNCHANGED (the removed loop
    used to mutate it in place to promote the next embedder)

See test_temporal_indexer_multi_embedder_1291.py for the underlying
TemporalIndexer.index_commits() AC1/AC4/AC5/AC10 coverage this test does NOT
duplicate -- this test's sole job is proving the CLI wrapper runs the
per-commit indexing pass exactly ONCE.
"""

import logging
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

from click.testing import CliRunner

from code_indexer.cli import index as index_command
from code_indexer.config import Config, TemporalConfig
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)


def _run_git(args: List[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo_with_one_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    (repo / "a.txt").write_text("hello world\n")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "Initial commit"], repo)
    return repo


class _FakeEmbedder(TemporalEmbedder):
    """Deterministic embedder: vector = [len(chunk)] * dims. No network I/O."""

    def __init__(self, name: str, model_slug: str, available: bool = True):
        self.name = name
        self.model_slug = model_slug
        self.dimensions = 4
        self.overlap_percentage = 0.0
        self._available = available
        self.embed_calls: List[List[str]] = []

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(chunks))
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions

    def is_available(self) -> bool:
        return self._available


class TestCliIndexDoesNotDoubleRunEmbedders:
    """Finding 1: `index_commits()` must run exactly ONCE per
    `cidx index --index-commits` invocation, covering every configured
    embedder in that single pass -- no leftover per-embedder CLI loop."""

    def test_two_embedder_missing_credentials_scenario_exits_zero(
        self, tmp_path, caplog
    ):
        active = _FakeEmbedder("fake-active-1291", "fake_active_1291", available=True)
        unavailable = _FakeEmbedder(
            "fake-unavailable-1291", "fake_unavailable_1291", available=False
        )
        register_embedder("fake-active-1291", lambda config, e=active: e)
        register_embedder("fake-unavailable-1291", lambda config, e=unavailable: e)
        try:
            repo = _init_repo_with_one_commit(tmp_path)
            index_dir = repo / ".code-indexer" / "index"
            index_dir.mkdir(parents=True)

            config = Config(codebase_dir=repo, embedding_provider="voyage-ai")
            config.temporal = TemporalConfig(
                embedders=["fake-active-1291", "fake-unavailable-1291"],
                active_embedder="fake-active-1291",
            )

            config_manager = MagicMock()
            config_manager.load.return_value = config
            config_manager.get_config.return_value = config
            config_manager.config_path = repo / ".code-indexer" / "config.json"

            runner = CliRunner()
            with caplog.at_level(logging.WARNING):
                result = runner.invoke(
                    index_command,
                    ["--index-commits"],
                    obj={"config_manager": config_manager},
                    catch_exceptions=False,
                )

            assert result.exit_code == 0, (
                "AC4: an unavailable NON-ACTIVE embedder must WARN-skip and "
                f"the job must stay GREEN. Output: {result.output!r}"
            )
            assert active.embed_calls, "the active embedder must have indexed"
            assert not unavailable.embed_calls, (
                "the unavailable embedder must never be invoked"
            )

            shards = [
                d
                for d in index_dir.iterdir()
                if d.is_dir()
                and d.name.startswith("code-indexer-temporal-fake_active_1291")
            ]
            assert shards, "the active embedder's shard must be built on disk"

            assert any(
                "fake-unavailable-1291" in record.message for record in caplog.records
            ), "a WARNING must name the skipped unavailable embedder"

            # Regression guard for the removed leftover loop: the old buggy
            # loop mutated config.temporal.active_embedder in place to
            # promote the next embedder -- which is exactly what caused the
            # double-run crash. It must be left untouched.
            assert config.temporal.active_embedder == "fake-active-1291"
        finally:
            unregister_embedder_for_tests("fake-active-1291")
            unregister_embedder_for_tests("fake-unavailable-1291")
