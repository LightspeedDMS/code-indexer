"""``rust_backend._build_matches`` enriches each finding with
``line_content`` by re-reading the candidate file from ``abs_path``,
independently of whatever candidate-collection filtering already ran.
This is the last point before a match is returned, so it must also
refuse to read a path that does not resolve inside the resolved
repository root passed to it (defense in depth alongside the
candidate-collection fix in ``search_engine._run_phase1_filename``).

Pure unit tests of the module-level function -- no CLI invocation, no
mocking of core logic (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.xray.rust_backend import _build_matches


class TestBuildMatchesRootContainment:
    def test_outside_root_path_is_never_read(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        outside_target = tmp_path / "outside_target.py"
        outside_target.write_text("outside_marker_value = 1\n")

        spec = {"lang": "python", "file_path": "escape_link.py"}
        findings = [{"line": 1, "pattern": "any", "snippet": ""}]

        matches = _build_matches(
            spec,
            findings,
            abs_path=str(outside_target),
            resolved_root=root.resolve(),
        )

        assert len(matches) == 1
        assert matches[0]["line_content"] == "", (
            f"A path resolving outside the given root must never be read; "
            f"got: {matches[0]}"
        )

    def test_inside_root_path_is_read_normally(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        inside_target = root / "legit.py"
        inside_target.write_text("legit_marker_value = 1\n")

        spec = {"lang": "python", "file_path": "legit.py"}
        findings = [{"line": 1, "pattern": "any", "snippet": ""}]

        matches = _build_matches(
            spec,
            findings,
            abs_path=str(inside_target),
            resolved_root=root.resolve(),
        )

        assert len(matches) == 1
        assert matches[0]["line_content"] == "legit_marker_value = 1", (
            f"A path resolving inside the given root must be read "
            f"normally (no regression); got: {matches[0]}"
        )

    def test_no_resolved_root_argument_reads_normally(self, tmp_path: Path) -> None:
        """Backward-compatible: omitting resolved_root (existing callers
        that have not been updated) preserves today's behaviour."""
        target = tmp_path / "some_file.py"
        target.write_text("no_root_value = 1\n")

        spec = {"lang": "python", "file_path": "some_file.py"}
        findings = [{"line": 1, "pattern": "any", "snippet": ""}]

        matches = _build_matches(spec, findings, abs_path=str(target))

        assert matches[0]["line_content"] == "no_root_value = 1"
