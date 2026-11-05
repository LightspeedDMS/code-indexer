"""EVO-66445: unit tests for the FTS backfill policy + its POD_PULL build executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import code_indexer.server.services.fts_backfill.policy as policy_mod
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.fts_backfill.policy import (
    FTS_BACKFILL_BUILD_OP,
    FtsBackfillPolicy,
    fts_backfill_build_executor,
)
from code_indexer.server.services.fts_index_builder import FTS_INDEX_DIRNAME


def _repo(tmp_path: Path, *, fts: bool = False, skip: bool = False) -> Path:
    ci = tmp_path / ".code-indexer"
    ci.mkdir(parents=True)
    if fts:
        d = ci / FTS_INDEX_DIRNAME
        d.mkdir()
        (d / "meta.json").write_text("{}")
    if skip:
        (ci / "fts_backfill_no_files.skip").write_text("")
    return tmp_path


class _BJM:
    def __init__(self, *, raise_dup: bool = False) -> None:
        self.raise_dup = raise_dup
        self.calls: List[dict] = []

    def submit_job(self, op, fn, **kw: Any) -> str:
        if self.raise_dup:
            raise DuplicateJobError(op, kw.get("repo_alias", ""), "existing")
        self.calls.append({"op": op, "fn": fn, **kw})
        return "job-1"


def test_is_present_reflects_tantivy_index(tmp_path: Path) -> None:
    built = _repo(tmp_path / "a", fts=True)
    missing = _repo(tmp_path / "b", fts=False)
    p = FtsBackfillPolicy(_BJM())
    assert p.is_present(built) is True
    assert p.is_present(missing) is False


def test_eligibility_skips_no_files_marked_repo(tmp_path: Path) -> None:
    p = FtsBackfillPolicy(_BJM())
    eligible = _repo(tmp_path / "ok", fts=False)
    marked = _repo(tmp_path / "empty", fts=False, skip=True)
    assert p.eligibility(marked) is None  # permanent no-files skip
    meta = p.eligibility(eligible)
    assert meta == {"repo_path": str(eligible), "alias": "ok"}


def test_enqueue_submits_pod_pull_build_job(tmp_path: Path) -> None:
    bjm = _BJM()
    p = FtsBackfillPolicy(bjm)
    repo = tmp_path / "svc-api"
    meta = {"repo_path": str(repo), "alias": "svc-api"}
    p.enqueue("svc-api", repo, meta)
    assert len(bjm.calls) == 1
    call = bjm.calls[0]
    assert call["op"] == FTS_BACKFILL_BUILD_OP
    assert call["repo_alias"] == "svc-api"
    assert call["metadata"] == meta  # pod-pull reconstruction params
    assert call["is_admin"] is True


def test_enqueue_swallows_duplicate_job(tmp_path: Path) -> None:
    p = FtsBackfillPolicy(_BJM(raise_dup=True))
    # a build already pending/running for this repo -> benign, no raise
    p.enqueue("svc-api", tmp_path, {"repo_path": str(tmp_path), "alias": "svc-api"})


def test_executor_reconstructs_build_from_metadata_and_returns_status(
    tmp_path: Path, monkeypatch
) -> None:
    """The pod-pull executor rebuilds one repo purely from the row metadata and maps
    the FtsBuildOutcome to a serializable status dict. Config read is best-effort — a
    failure just leaves timeout unset (None), never blocks the build."""
    captured = {}

    class _Outcome:
        status = type("S", (), {"value": "rebuilt"})()
        alias = "svc-api"
        detail = "built"

    def fake_ensure(repo_path, *, alias, subprocess_env, timeout, local_content):
        captured.update(
            repo_path=repo_path,
            alias=alias,
            timeout=timeout,
            local_content=local_content,
        )
        return _Outcome()

    monkeypatch.setattr(policy_mod, "ensure_fts_index", fake_ensure)

    result = fts_backfill_build_executor(
        {"repo_path": str(tmp_path / "svc-api"), "alias": "svc-api"}
    )

    assert captured["repo_path"] == Path(tmp_path / "svc-api")
    assert captured["alias"] == "svc-api"
    assert captured["local_content"] is False  # no registry: not local://
    assert result == {"status": "rebuilt", "alias": "svc-api", "detail": "built"}
