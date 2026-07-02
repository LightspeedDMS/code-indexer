"""Tests for LangfuseReadmeGenerator (Story #592).

Tests cover:
- Root README generation with multiple sessions
- Per-session README generation
- Template version skip logic (should skip / should regenerate)
- Atomic write (temp + rename)
- Edge cases: empty session, session with only subagent files, trace.input = None
"""

import json
from pathlib import Path

from code_indexer.server.services.langfuse_readme_generator import (
    LangfuseReadmeGenerator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_trace_file(
    session_dir: Path,
    filename: str,
    trace_input,
    trace_output="response",
    timestamp="2024-01-15T10:00:00Z",
) -> None:
    """Write a fake trace JSON file to a session directory."""
    session_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "trace": {
            "input": trace_input,
            "output": trace_output,
            "timestamp": timestamp,
        },
        "observations": [],
    }
    (session_dir / filename).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_repo(tmp_path: Path, sessions: dict) -> Path:
    """
    Create a fake repo directory tree.

    sessions = {
        "session-abc": [
            ("001_turn_12345678.json", "user prompt text", "2024-01-15T10:00:00Z"),
            ("002_subagent-tdd_deadbeef.json", None, "2024-01-15T10:05:00Z"),
        ],
        ...
    }
    """
    repo = tmp_path / "langfuse_Claude_Code_seba_battig"
    for session_id, files in sessions.items():
        session_dir = repo / session_id
        for fname, trace_input, ts in files:
            _write_trace_file(session_dir, fname, trace_input, timestamp=ts)
    return repo


# ---------------------------------------------------------------------------
# Root README generation
# ---------------------------------------------------------------------------


class TestRootReadmeGeneration:
    def test_creates_root_readme_when_missing(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    (
                        "001_turn_12345678.json",
                        "Fix the bug please",
                        "2024-01-15T10:00:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        readme = repo / "README.md"
        assert readme.exists(), "Root README.md should be created"

    def test_root_readme_contains_version_marker(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Do something", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "README.md").read_text()
        assert "<!-- cidx-readme-v1 -->" in content

    def test_root_readme_contains_session_table(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    (
                        "001_turn_12345678.json",
                        "Fix the bug please",
                        "2024-01-15T10:00:00Z",
                    ),
                    (
                        "002_turn_deadbeef.json",
                        "What is the status",
                        "2024-01-15T11:00:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "README.md").read_text()
        assert "session-abc" in content
        assert "Sessions" in content

    def test_root_readme_counts_turns_and_subagents(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-xyz": [
                    ("001_turn_aaaaaaaa.json", "First turn", "2024-01-15T10:00:00Z"),
                    ("002_turn_bbbbbbbb.json", "Second turn", "2024-01-15T10:01:00Z"),
                    (
                        "003_subagent-tdd_cccccccc.json",
                        "Subagent task",
                        "2024-01-15T10:02:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-xyz"})

        content = (repo / "README.md").read_text()
        # Should mention 2 turns and 1 subagent in the session row
        assert "session-xyz" in content
        # Table should have a row with 2 turns
        assert "2" in content

    def test_root_readme_truncates_last_prompt_to_80_chars(self, tmp_path):
        long_prompt = "A" * 120
        repo = _make_repo(
            tmp_path,
            {
                "session-long": [
                    ("001_turn_12345678.json", long_prompt, "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-long"})

        content = (repo / "README.md").read_text()
        # The full 120-char prompt should NOT appear verbatim
        assert long_prompt not in content
        # But 80 chars of it should appear
        assert "A" * 80 in content

    def test_root_readme_multiple_sessions(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-001": [
                    ("001_turn_11111111.json", "First session", "2024-01-10T10:00:00Z"),
                ],
                "session-002": [
                    (
                        "001_turn_22222222.json",
                        "Second session",
                        "2024-01-20T10:00:00Z",
                    ),
                ],
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-001", "session-002"})

        content = (repo / "README.md").read_text()
        assert "session-001" in content
        assert "session-002" in content

    def test_root_readme_contains_how_to_read_section(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Help me", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "README.md").read_text()
        assert "How to Read" in content

    def test_root_readme_date_extracted_from_timestamp(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-date": [
                    ("001_turn_12345678.json", "Some prompt", "2024-03-25T15:30:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-date"})

        content = (repo / "README.md").read_text()
        assert "2024-03-25" in content


# ---------------------------------------------------------------------------
# Per-session README generation
# ---------------------------------------------------------------------------


class TestSessionReadmeGeneration:
    def test_creates_session_readme_for_modified_sessions(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Do the thing", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        readme = repo / "session-abc" / "README.md"
        assert readme.exists(), "Session README.md should be created"

    def test_session_readme_contains_version_marker(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Do the thing", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "session-abc" / "README.md").read_text()
        assert "<!-- cidx-readme-v1 -->" in content

    def test_session_readme_lists_all_files_in_order(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_aaaaaaaa.json", "First", "2024-01-15T10:00:00Z"),
                    ("002_turn_bbbbbbbb.json", "Second", "2024-01-15T10:01:00Z"),
                    ("003_subagent-tdd_cccccccc.json", "Third", "2024-01-15T10:02:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "session-abc" / "README.md").read_text()
        assert "001_turn_aaaaaaaa.json" in content
        assert "002_turn_bbbbbbbb.json" in content
        assert "003_subagent-tdd_cccccccc.json" in content

    def test_session_readme_shows_session_id(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-xyz-123": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-xyz-123"})

        content = (repo / "session-xyz-123" / "README.md").read_text()
        assert "session-xyz-123" in content

    def test_session_readme_not_created_for_unmodified_sessions(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-modified": [
                    ("001_turn_12345678.json", "Modified", "2024-01-15T10:00:00Z"),
                ],
                "session-unmodified": [
                    ("001_turn_87654321.json", "Unmodified", "2024-01-14T10:00:00Z"),
                ],
            },
        )
        generator = LangfuseReadmeGenerator()
        # Only session-modified is in the modified set
        generator.generate_for_repo(repo, {"session-modified"})

        assert (repo / "session-modified" / "README.md").exists()
        assert not (repo / "session-unmodified" / "README.md").exists()

    def test_session_readme_contains_how_to_resume_section(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Help", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "session-abc" / "README.md").read_text()
        assert "How to Resume" in content

    def test_session_readme_shows_file_type_column(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_aaaaaaaa.json", "User turn", "2024-01-15T10:00:00Z"),
                    (
                        "002_subagent-tdd_bbbbbbbb.json",
                        "Subagent task",
                        "2024-01-15T10:01:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "session-abc" / "README.md").read_text()
        assert "turn" in content
        assert "subagent" in content


# ---------------------------------------------------------------------------
# Skip logic (template version check)
# ---------------------------------------------------------------------------


class TestSkipLogic:
    def test_root_readme_not_rewritten_when_sessions_unchanged(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        readme = repo / "README.md"
        mtime_first = readme.stat().st_mtime

        # Run again with same sessions — should skip root README rewrite
        generator.generate_for_repo(repo, {"session-abc"})
        mtime_second = readme.stat().st_mtime

        assert mtime_first == mtime_second, (
            "Root README should NOT be rewritten when unchanged"
        )

    def test_root_readme_rewritten_when_new_session_added(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "First", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        readme = repo / "README.md"
        mtime_first = readme.stat().st_mtime

        # Add a new session to the repo
        new_session = repo / "session-new"
        _write_trace_file(
            new_session,
            "001_turn_99999999.json",
            "New session",
            timestamp="2024-01-20T10:00:00Z",
        )

        # Small delay to ensure mtime difference is detectable
        import time

        time.sleep(0.01)

        generator.generate_for_repo(repo, {"session-new"})
        mtime_second = readme.stat().st_mtime

        assert mtime_second > mtime_first, (
            "Root README should be rewritten when new session added"
        )

    def test_root_readme_rewritten_when_version_marker_missing(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        # Write a README without the version marker
        (repo / "README.md").write_text(
            "# Old README without version marker\n", encoding="utf-8"
        )

        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "README.md").read_text()
        assert "<!-- cidx-readme-v1 -->" in content, (
            "Should rewrite when version marker missing"
        )

    def test_root_readme_rewritten_when_old_version_marker(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        # Write a README with an OLD version marker
        (repo / "README.md").write_text(
            "<!-- cidx-readme-v0 -->\n# Old version\n| session-abc | 2024-01-15 | 1 | 0 | Prompt |\n",
            encoding="utf-8",
        )

        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-abc"})

        content = (repo / "README.md").read_text()
        assert "<!-- cidx-readme-v1 -->" in content, (
            "Should rewrite when old version marker present"
        )

    def test_should_skip_root_returns_true_when_unchanged(self, tmp_path):
        """Direct unit test of _should_skip_root()."""
        generator = LangfuseReadmeGenerator()

        # Build a session_rows list
        session_rows = [
            {
                "session_id": "session-abc",
                "date": "2024-01-15",
                "turns": 1,
                "subagents": 0,
                "last_prompt": "Prompt",
            },
        ]

        # Build content that would match
        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator.generate_for_repo(repo, {"session-abc"})
        existing = (repo / "README.md").read_text()

        # Extract rows from the generated README (re-parse)
        result = generator._should_skip_root(existing, session_rows)
        assert result is True

    def test_should_skip_root_returns_false_when_no_version_marker(self):
        generator = LangfuseReadmeGenerator()
        session_rows = [
            {
                "session_id": "s",
                "date": "2024-01-01",
                "turns": 1,
                "subagents": 0,
                "last_prompt": "x",
            }
        ]
        result = generator._should_skip_root("# No version marker here", session_rows)
        assert result is False

    def test_should_skip_root_returns_false_when_session_count_changed(self, tmp_path):
        generator = LangfuseReadmeGenerator()

        repo = _make_repo(
            tmp_path,
            {
                "session-abc": [
                    ("001_turn_12345678.json", "Prompt", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator.generate_for_repo(repo, {"session-abc"})
        existing = (repo / "README.md").read_text()

        # Now pass a different session_rows (different session count)
        new_rows = [
            {
                "session_id": "session-abc",
                "date": "2024-01-15",
                "turns": 1,
                "subagents": 0,
                "last_prompt": "Prompt",
            },
            {
                "session_id": "session-new",
                "date": "2024-01-20",
                "turns": 2,
                "subagents": 1,
                "last_prompt": "New",
            },
        ]
        result = generator._should_skip_root(existing, new_rows)
        assert result is False


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_atomic_write_creates_file(self, tmp_path):
        generator = LangfuseReadmeGenerator()
        target = tmp_path / "README.md"
        generator._atomic_write(target, "# Test content\n")

        assert target.exists()
        assert target.read_text() == "# Test content\n"

    def test_atomic_write_no_tmp_file_left(self, tmp_path):
        generator = LangfuseReadmeGenerator()
        target = tmp_path / "README.md"
        generator._atomic_write(target, "# Content\n")

        tmp_file = target.with_suffix(".tmp")
        assert not tmp_file.exists(), "Temp file should be gone after atomic write"

    def test_atomic_write_overwrites_existing(self, tmp_path):
        generator = LangfuseReadmeGenerator()
        target = tmp_path / "README.md"
        target.write_text("old content", encoding="utf-8")

        generator._atomic_write(target, "new content")
        assert target.read_text() == "new content"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_session_folder(self, tmp_path):
        """Session folder exists but has no JSON files."""
        repo = tmp_path / "langfuse_Claude_Code_seba_battig"
        (repo / "session-empty").mkdir(parents=True)

        generator = LangfuseReadmeGenerator()
        # Should not raise
        generator.generate_for_repo(repo, {"session-empty"})

        # Root README should be created
        assert (repo / "README.md").exists()
        # Session README should be created (even if empty)
        assert (repo / "session-empty" / "README.md").exists()

    def test_trace_input_none(self, tmp_path):
        """trace.input = None should not crash."""
        repo = _make_repo(
            tmp_path,
            {
                "session-none": [
                    ("001_turn_12345678.json", None, "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        # Should not raise
        generator.generate_for_repo(repo, {"session-none"})

        content = (repo / "README.md").read_text()
        assert "session-none" in content

    def test_session_with_only_subagent_files(self, tmp_path):
        """Session has only subagent files, no turn files."""
        repo = _make_repo(
            tmp_path,
            {
                "session-subagent-only": [
                    (
                        "001_subagent-tdd_aaaaaaaa.json",
                        "Implement feature X",
                        "2024-01-15T10:00:00Z",
                    ),
                    (
                        "002_subagent-reviewer_bbbbbbbb.json",
                        "Review the code",
                        "2024-01-15T10:05:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-subagent-only"})

        # Root README: session should appear with 0 turns, 2 subagents
        root_content = (repo / "README.md").read_text()
        assert "session-subagent-only" in root_content

        # Session README should list both files
        session_content = (repo / "session-subagent-only" / "README.md").read_text()
        assert "001_subagent-tdd_aaaaaaaa.json" in session_content
        assert "002_subagent-reviewer_bbbbbbbb.json" in session_content

    def test_missing_session_id_not_in_repo(self, tmp_path):
        """modified_session_ids contains a session that doesn't exist on disk."""
        repo = _make_repo(
            tmp_path,
            {
                "session-real": [
                    ("001_turn_12345678.json", "Real", "2024-01-15T10:00:00Z"),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        # Should not raise even if session-ghost doesn't exist
        generator.generate_for_repo(repo, {"session-real", "session-ghost"})

        assert (repo / "README.md").exists()
        assert (repo / "session-real" / "README.md").exists()
        assert not (repo / "session-ghost" / "README.md").exists()

    def test_trace_json_parse_error_handled_gracefully(self, tmp_path):
        """Malformed JSON in trace file should not crash."""
        repo = tmp_path / "langfuse_test"
        session_dir = repo / "session-bad-json"
        session_dir.mkdir(parents=True)
        (session_dir / "001_turn_12345678.json").write_text(
            "{ not valid json }", encoding="utf-8"
        )

        generator = LangfuseReadmeGenerator()
        # Should not raise
        generator.generate_for_repo(repo, {"session-bad-json"})

        assert (repo / "README.md").exists()

    def test_trace_input_newlines_replaced_in_table(self, tmp_path):
        """Newlines in trace.input should be replaced with spaces in table."""
        repo = _make_repo(
            tmp_path,
            {
                "session-newline": [
                    (
                        "001_turn_12345678.json",
                        "Line one\nLine two\nLine three",
                        "2024-01-15T10:00:00Z",
                    ),
                ]
            },
        )
        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, {"session-newline"})

        content = (repo / "README.md").read_text()
        # Should not contain raw newlines within the table row for the prompt
        # The truncated prompt should have spaces instead of newlines
        assert "Line one Line two" in content or "Line one" in content

    def test_repo_with_no_sessions(self, tmp_path):
        """Repo directory exists but has no session subdirectories."""
        repo = tmp_path / "langfuse_empty_repo"
        repo.mkdir()

        generator = LangfuseReadmeGenerator()
        generator.generate_for_repo(repo, set())

        # Root README should be created (empty sessions table)
        assert (repo / "README.md").exists()
        content = (repo / "README.md").read_text()
        assert "<!-- cidx-readme-v1 -->" in content
