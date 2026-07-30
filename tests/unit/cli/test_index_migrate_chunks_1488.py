"""Story #1488: CLI wiring of `cidx index --migrate-chunks-to-sqlite`.

Verifies the option is registered on the real ``index`` command (help text)
-- the orchestration/exclusivity behavior itself is covered by
``tests/unit/services/test_chunk_migration_cli_1488.py``.
"""

from __future__ import annotations

from click.testing import CliRunner

from code_indexer.cli import cli


def test_index_help_lists_migrate_chunks_option() -> None:
    result = CliRunner().invoke(cli, ["index", "--help"])
    assert result.exit_code == 0
    assert "--migrate-chunks-to-sqlite" in result.output
