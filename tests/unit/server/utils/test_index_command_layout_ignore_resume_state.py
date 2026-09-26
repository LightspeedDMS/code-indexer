"""Indexer resume-state trust and containment.

Single-seam completeness: `--ignore-resume-state` must reach every real
server-spawned `cidx index` site, including the three that construct
their commands independently (global_repos/refresh_scheduler.py's
scheduled golden-repo refresh, server/services/claude_cli_manager.py's
cidx-meta re-index, server/mcp/handlers/repos.py's provider-index add).

`append_server_layout_args` (server/utils/index_command_layout.py) is
ALREADY the single shared seam every server-context `cidx index` spawn
routes through -- enforced by the existing repo-wide AST discovery guard
`tests/unit/server/utils/test_index_command_layout_spawn_site_guard_1488.py`
(`test_every_server_context_cidx_index_spawn_is_wrapped`, parametrized over
EVERY discovered server-context `cidx index` construction, including a new
future site). Stamping `--ignore-resume-state` INSIDE this same helper --
rather than at each call site individually -- means every site that guard
already proves is wrapped automatically gets this flag too, with no
per-site duplication and no way for a future site to forget it (it would
already fail the Story #1488 guard if unwrapped, and once wrapped it gets
both flags for free).

This test proves the payload itself is correct; site-COMPLETENESS is
guaranteed by the pre-existing Story #1488 guard, which this fix does not
duplicate.
"""

from __future__ import annotations

from code_indexer.server.utils.index_command_layout import append_server_layout_args


class TestAppendServerLayoutArgsIgnoresResumeState:
    def test_appends_ignore_resume_state_flag(self) -> None:
        result = append_server_layout_args(["cidx", "index"])

        assert "--ignore-resume-state" in result, (
            "SECURITY: every server-context `cidx index` spawn must distrust "
            f"repo-authored resume state. Got: {result}"
        )

    def test_appends_both_flags_alongside_existing_layout_stamp(self) -> None:
        result = append_server_layout_args(
            ["cidx", "index", "--fts", "--progress-json"]
        )

        assert result == [
            "cidx",
            "index",
            "--fts",
            "--progress-json",
            "--new-collection-layout=chunks_db",
            "--ignore-resume-state",
            "--server-managed-provider-settings",
        ]

    def test_does_not_mutate_input_list(self) -> None:
        original = ["cidx", "index", "--clear"]
        result = append_server_layout_args(original)

        assert original == ["cidx", "index", "--clear"]
        assert result is not original
