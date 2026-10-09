"""Story #1488: server-side `cidx index` spawn sites stamp the explicit
CHUNKS_DB new-collection layout via a single shared helper.

The server states the chunk-storage layout explicitly rather than relying
on the CLI/daemon default (which is SHARDED_JSON). Every server-context
`cidx index` command list routes through append_server_layout_args so a
future spawn site cannot silently regress to the CLI default.

This SAME helper also stamps `--ignore-resume-state`
(see index_command_layout.py's module docstring) -- the two exact-list
assertions below were updated accordingly when that flag was added.

This SAME helper also stamps `--server-managed-provider-settings`: a
repository-authored `config.json` must never choose an embedding-provider
endpoint or a daemon-mode override for server-spawned indexing -- only
server-managed provider settings are used.
"""

from code_indexer.server.utils.index_command_layout import append_server_layout_args


class TestAppendServerLayoutArgs:
    def test_appends_chunks_db_layout_flag(self) -> None:
        result = append_server_layout_args(["cidx", "index"])

        assert result == [
            "cidx",
            "index",
            "--new-collection-layout=chunks_db",
            "--ignore-resume-state",
            "--server-managed-provider-settings",
        ]

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
            "--ignore-resume-state",
            "--server-managed-provider-settings",
        ]

    def test_does_not_mutate_input_list(self) -> None:
        original = ["cidx", "index", "--clear"]
        result = append_server_layout_args(original)

        assert original == ["cidx", "index", "--clear"]
        assert result is not original


class TestContentBackendStamp:
    """The server's content-backend selection reaches every spawned child.

    The child cannot read the server config and must not take the choice from
    the repository's (tenant-writable) config, so the helper is the only way
    it can arrive -- and because every spawn site already routes through the
    helper, none of them can forget it.
    """

    _BASE = [
        "cidx",
        "index",
        "--new-collection-layout=chunks_db",
        "--ignore-resume-state",
        "--server-managed-provider-settings",
    ]

    def test_local_stamps_nothing(self) -> None:
        """The default command line is byte-for-byte what it was."""
        assert (
            append_server_layout_args(["cidx", "index"], content_backend="local")
            == self._BASE
        )

    def test_delegated_stamps_the_flag_last(self) -> None:
        assert append_server_layout_args(
            ["cidx", "index"], content_backend="delegated"
        ) == [*self._BASE, "--content-backend=delegated"]

    def test_unset_reads_the_server_config(self) -> None:
        """Production callers pass nothing; the selection must still arrive."""
        from types import SimpleNamespace
        from unittest.mock import patch

        service = SimpleNamespace(
            get_config=lambda: SimpleNamespace(content_backend="delegated")
        )
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=service,
        ):
            result = append_server_layout_args(["cidx", "index"])

        assert result[-1] == "--content-backend=delegated"

    def test_unset_with_a_local_server_config_stamps_nothing(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        service = SimpleNamespace(
            get_config=lambda: SimpleNamespace(content_backend="local")
        )
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=service,
        ):
            assert append_server_layout_args(["cidx", "index"]) == self._BASE


class TestGitBackendStamp:
    """The git selection travels to the child the same way.

    On a pod that does not hold the repository the child must ask git-mcp for
    HEAD's commit and branch; without the flag it records "unknown", and
    refresh then re-indexes every cycle.
    """

    _BASE = [
        "cidx",
        "index",
        "--new-collection-layout=chunks_db",
        "--ignore-resume-state",
        "--server-managed-provider-settings",
    ]

    def test_local_stamps_nothing(self) -> None:
        assert (
            append_server_layout_args(
                ["cidx", "index"], content_backend="local", git_backend="local"
            )
            == self._BASE
        )

    def test_delegated_git_stamps_its_flag(self) -> None:
        assert append_server_layout_args(
            ["cidx", "index"], content_backend="local", git_backend="delegated"
        ) == [*self._BASE, "--git-backend=delegated"]

    def test_both_delegated_stamp_content_then_git(self) -> None:
        assert append_server_layout_args(
            ["cidx", "index"], content_backend="delegated", git_backend="delegated"
        ) == [*self._BASE, "--content-backend=delegated", "--git-backend=delegated"]

    def test_unset_reads_the_server_config(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        service = SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                content_backend="delegated", git_backend="delegated"
            )
        )
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=service,
        ):
            result = append_server_layout_args(["cidx", "index"])

        assert result[-2:] == ["--content-backend=delegated", "--git-backend=delegated"]
