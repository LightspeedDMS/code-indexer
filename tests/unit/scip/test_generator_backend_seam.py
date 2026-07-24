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
        language: Optional[str] = None,
    ) -> IndexerResult:
        self.calls.append(
            {
                "project_dir": project_dir,
                "output_dir": output_dir,
                "build_system": build_system,
                "workspace_id": workspace_id,
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
        assert call["language"] == "typescript"
        assert call["build_system"] == "npm"

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
