"""Unit tests for the SCIPGenerator backend seam / DI hook (EVO-65021)."""

from pathlib import Path
from typing import Optional

from code_indexer.scip.generator import SCIPGenerator
from code_indexer.scip.indexers.base import (
    IndexerResult,
    IndexerStatus,
    SCIPIndexer,
)
from code_indexer.scip.indexers.typescript import TypeScriptIndexer


class _RecordingIndexer(SCIPIndexer):
    """Captures the kwargs the generator threads into generate()."""

    def __init__(self):
        self.calls = []

    def generate(
        self,
        project_dir: Path,
        output_dir: Path,
        build_system: str,
        *,
        workspace_id: Optional[str] = None,
        golden_alias: Optional[str] = None,
        language: Optional[str] = None,
    ) -> IndexerResult:
        self.calls.append(
            {
                "project_dir": project_dir,
                "output_dir": output_dir,
                "build_system": build_system,
                "workspace_id": workspace_id,
                "golden_alias": golden_alias,
                "language": language,
            }
        )
        # Return FAILED so the generator does not attempt the DB build.
        return IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr="recorded",
            exit_code=1,
        )

    def is_available(self) -> bool:
        return True

    def get_version(self) -> Optional[str]:
        return "test"


class TestBackendSeam:
    def test_default_map_is_local(self, tmp_path):
        """No indexer_map => local subprocess indexers (behaviour unchanged)."""
        gen = SCIPGenerator(tmp_path)
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None

    def test_injected_map_and_workspace_id_threaded(self, tmp_path):
        """generate() threads workspace_id + language into the indexer."""
        (tmp_path / "package.json").write_text("{}")
        recorder = _RecordingIndexer()
        gen = SCIPGenerator(
            tmp_path,
            indexer_map={"typescript": recorder},
            workspace_id="W-42",
        )

        gen.generate()

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["workspace_id"] == "W-42"
        assert call["golden_alias"] is None
        assert call["language"] == "typescript"
        assert call["build_system"] == "npm"

    def test_injected_map_and_golden_alias_threaded(self, tmp_path):
        """EVO-65365: generate() threads golden_alias into the indexer."""
        (tmp_path / "package.json").write_text("{}")
        recorder = _RecordingIndexer()
        gen = SCIPGenerator(
            tmp_path,
            indexer_map={"typescript": recorder},
            golden_alias="svc-foo",
        )

        gen.generate()

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["golden_alias"] == "svc-foo"
        assert call["workspace_id"] is None
        assert call["language"] == "typescript"

    def test_rebuild_threads_workspace_id(self, tmp_path):
        """rebuild_projects() also threads workspace_id + language."""
        from code_indexer.scip.status import (
            StatusTracker,
            GenerationStatus,
            ProjectStatus,
            OverallStatus,
        )
        from datetime import datetime

        (tmp_path / "package.json").write_text("{}")
        recorder = _RecordingIndexer()
        gen = SCIPGenerator(
            tmp_path,
            indexer_map={"typescript": recorder},
            workspace_id="W-77",
        )

        # Seed a status file with one failed TS project at the repo root.
        tracker = StatusTracker(gen.scip_dir)
        status = GenerationStatus(
            overall_status=OverallStatus.FAILED,
            total_projects=1,
            successful_projects=0,
            failed_projects=1,
            projects={
                ".": ProjectStatus(
                    status=OverallStatus.FAILED,
                    language="typescript",
                    build_system="npm",
                    timestamp=datetime.now().isoformat(),
                )
            },
        )
        tracker.save(status)

        gen.rebuild_projects(["."], force=True)

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["workspace_id"] == "W-77"
        assert call["language"] == "typescript"


class _FixedResultIndexer(SCIPIndexer):
    """Returns a caller-specified FAILED result (exit code / stderr)."""

    def __init__(self, *, exit_code: int, stderr: str = "failed"):
        self._exit_code = exit_code
        self._stderr = stderr

    def generate(
        self,
        project_dir,
        output_dir,
        build_system,
        *,
        workspace_id=None,
        golden_alias=None,
        language=None,
    ):
        return IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr=self._stderr,
            exit_code=self._exit_code,
        )

    def is_available(self) -> bool:
        return True

    def get_version(self) -> Optional[str]:
        return "test"


class TestFreshnessClassification:
    """EVO-65021 freshness thrash fix: exit-69 (unsupported language) is a benign
    outcome that must NOT block freshness, while a real failure must."""

    def test_unsupported_language_exit_69_is_fresh_eligible(self, tmp_path):
        """A discovered project that exits 69 counts as unsupported (benign)."""
        (tmp_path / "package.json").write_text("{}")
        gen = SCIPGenerator(
            tmp_path,
            indexer_map={"typescript": _FixedResultIndexer(exit_code=69)},
        )
        result = gen.generate()
        assert result.total_projects == 1
        assert result.failed_projects == 1
        assert result.unsupported_language_projects == 1
        assert result.real_failed_projects() == 0
        assert result.is_fresh_eligible() is True

    def test_real_failure_is_not_fresh_eligible(self, tmp_path):
        """A genuine build failure (exit 1) is NOT counted as unsupported."""
        (tmp_path / "package.json").write_text("{}")
        gen = SCIPGenerator(
            tmp_path,
            indexer_map={
                "typescript": _FixedResultIndexer(exit_code=1, stderr="toolchain boom")
            },
        )
        result = gen.generate()
        assert result.failed_projects == 1
        assert result.unsupported_language_projects == 0
        assert result.real_failed_projects() == 1
        assert result.is_fresh_eligible() is False

    def test_zero_discovered_projects_is_fresh_eligible(self, tmp_path):
        """EVO-65021: a repo with no discoverable projects is legitimately
        converged (nothing to index), so it is fresh-eligible and does not
        thrash by re-enqueueing an identical empty rebuild forever."""
        # Empty dir → ProjectDiscovery finds nothing.
        gen = SCIPGenerator(tmp_path, indexer_map={})
        result = gen.generate()
        assert result.total_projects == 0
        assert result.failed_projects == 0
        assert result.is_fresh_eligible() is True

    def test_no_indexer_for_language_is_unsupported(self, tmp_path):
        """A discovered language with no indexer in the map is a benign skip."""
        (tmp_path / "package.json").write_text("{}")
        gen = SCIPGenerator(tmp_path, indexer_map={})  # no typescript indexer
        result = gen.generate()
        assert result.total_projects == 1
        assert result.failed_projects == 1
        assert result.unsupported_language_projects == 1
        assert result.is_fresh_eligible() is True


class TestUnsupportedLanguageHelper:
    def test_exit_69_detected(self):
        from code_indexer.scip.generator import (
            indexer_result_is_unsupported_language,
        )

        r = IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr="whatever",
            exit_code=69,
        )
        assert indexer_result_is_unsupported_language(r) is True

    def test_marker_string_detected(self):
        from code_indexer.scip.generator import (
            indexer_result_is_unsupported_language,
        )

        r = IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr="No indexer available for language: ruby",
            exit_code=-1,
        )
        assert indexer_result_is_unsupported_language(r) is True

    def test_real_failure_not_detected(self):
        from code_indexer.scip.generator import (
            indexer_result_is_unsupported_language,
        )

        r = IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr="build failed: missing dependency",
            exit_code=1,
        )
        assert indexer_result_is_unsupported_language(r) is False

    def test_backpressure_429_not_detected(self):
        from code_indexer.scip.generator import (
            indexer_result_is_unsupported_language,
        )

        r = IndexerResult(
            status=IndexerStatus.FAILED,
            duration_seconds=0.0,
            output_file=None,
            stdout="",
            stderr="sandbox_scip failed (HTTP 429): too many requests",
            exit_code=-1,
        )
        assert indexer_result_is_unsupported_language(r) is False
