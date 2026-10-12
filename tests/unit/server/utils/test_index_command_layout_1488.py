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

import pytest

from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.utils.index_command_layout import append_server_layout_args

_ATTRIBUTION = StatsAttribution(
    golden_repo_alias="example-repo-global",
    job_id="job-1",
    node_id="node-a",
    purpose="index",
)
#: Story #2118: one `--opt=value` token carrying the attribution JSON.
_STATS_ARG = f"--stats-attribution={_ATTRIBUTION.to_json()}"


class TestAppendServerLayoutArgs:
    def test_appends_chunks_db_layout_flag(self) -> None:
        result = append_server_layout_args(["cidx", "index"], attribution=_ATTRIBUTION)

        assert result == [
            "cidx",
            "index",
            "--new-collection-layout=chunks_db",
            "--ignore-resume-state",
            "--server-managed-provider-settings",
            _STATS_ARG,
        ]

    def test_preserves_prior_tokens_and_order(self) -> None:
        result = append_server_layout_args(
            ["cidx", "index", "--fts", "--reconcile", "--progress-json"],
            attribution=_ATTRIBUTION,
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
            _STATS_ARG,
        ]

    def test_does_not_mutate_input_list(self) -> None:
        original = ["cidx", "index", "--clear"]
        result = append_server_layout_args(original, attribution=_ATTRIBUTION)

        assert original == ["cidx", "index", "--clear"]
        assert result is not original

    def test_stamped_attribution_round_trips(self) -> None:
        result = append_server_layout_args(["cidx", "index"], attribution=_ATTRIBUTION)

        prefix = "--stats-attribution="
        [stamped] = [token for token in result if token.startswith(prefix)]
        assert StatsAttribution.from_json(stamped[len(prefix) :]) == _ATTRIBUTION

    def test_requires_attribution(self) -> None:
        with pytest.raises(TypeError):
            append_server_layout_args(["cidx", "index"])  # type: ignore[call-arg]
