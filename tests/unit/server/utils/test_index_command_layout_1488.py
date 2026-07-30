"""Story #1488: server-side `cidx index` spawn sites stamp the explicit
CHUNKS_DB new-collection layout via a single shared helper.

The server states the chunk-storage layout explicitly rather than relying
on the CLI/daemon default (which is SHARDED_JSON). Every server-context
`cidx index` command list routes through append_server_layout_args so a
future spawn site cannot silently regress to the CLI default.
"""

from code_indexer.server.utils.index_command_layout import append_server_layout_args


class TestAppendServerLayoutArgs:
    def test_appends_chunks_db_layout_flag(self) -> None:
        result = append_server_layout_args(["cidx", "index"])

        assert result == ["cidx", "index", "--new-collection-layout=chunks_db"]

    def test_preserves_prior_tokens_and_order(self) -> None:
        result = append_server_layout_args(
            ["cidx", "index", "--fts", "--reconcile", "--progress-json"]
        )

        assert result == [
            "cidx",
            "index",
            "--fts",
            "--reconcile",
            "--progress-json",
            "--new-collection-layout=chunks_db",
        ]

    def test_does_not_mutate_input_list(self) -> None:
        original = ["cidx", "index", "--clear"]
        result = append_server_layout_args(original)

        assert original == ["cidx", "index", "--clear"]
        assert result is not original
