"""Feature add-on CLI commands are found lazily (modular phase 4a), for
every installed feature (phase 4c)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Sequence

import click
import pytest
from cidx_sdk import BaseFeature, CliCommandContribution, FeatureManifest

from code_indexer import cli_features
from code_indexer.server.features.loader import FeatureCandidate


@pytest.fixture(autouse=True)
def fresh_commands() -> Iterator[None]:
    cli_features.reset_feature_commands()
    yield
    cli_features.reset_feature_commands()


def _installed(
    monkeypatch: pytest.MonkeyPatch, candidates: Dict[str, FeatureCandidate]
) -> None:
    from code_indexer.server.features import loader

    monkeypatch.setattr(loader, "discover_installed_features", lambda: candidates)


def _candidate(name: str, feature: Any) -> FeatureCandidate:
    return FeatureCandidate(name=name, load=lambda: lambda: feature, source="inline")


class _Hello(BaseFeature):
    def cli_commands(self) -> Sequence[CliCommandContribution]:
        @click.command(name="hello-cli")
        def hello() -> None:
            click.echo("hello")

        return (CliCommandContribution(name="hello-cli", command=hello),)


def _hello(requires_sdk: str = ">=0.1,<1") -> _Hello:
    return _Hello(
        FeatureManifest(name="hello", version="1.0.0", requires_sdk=requires_sdk)
    )


def test_no_installed_feature_means_no_feature_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch, {})
    assert cli_features.feature_command("anything") is None
    assert cli_features.listed_feature_commands([]) == []


def test_installed_feature_commands_need_no_server_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 4c: the CLI serves every INSTALLED feature's commands, with no
    server config.json at all (a CLI-only user keeps `cidx xray`)."""
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    _installed(monkeypatch, {"hello": _candidate("hello", _hello())})
    assert cli_features.feature_command("hello-cli") is not None
    assert cli_features.listed_feature_commands([]) == ["hello-cli"]


def test_refused_feature_fails_loud_on_lookup_but_not_in_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One refused add-on hides no other add-on's commands; an unknown name
    while one is refused fails loud (the command may be the refused one's)."""
    _installed(
        monkeypatch,
        {
            "hello": _candidate("hello", _hello()),
            "future": _candidate(
                "future",
                _Hello(
                    FeatureManifest(
                        name="future", version="1.0.0", requires_sdk=">=99.0"
                    )
                ),
            ),
        },
    )
    assert cli_features.feature_command("hello-cli") is not None
    with pytest.raises(click.ClickException, match="future"):
        cli_features.feature_command("anything")
    cli_features.reset_feature_commands()
    assert cli_features.listed_feature_commands([]) == ["hello-cli"]
    assert "future" in capsys.readouterr().err


def test_builtin_commands_never_import_the_feature_lookup() -> None:
    """The CLI import budget: resolving a built-in command loads nothing of
    the feature machinery."""
    probe = (
        "import sys, click\n"
        "from code_indexer.cli import cli\n"
        "ctx = click.Context(cli)\n"
        "assert cli.get_command(ctx, 'query') is not None\n"
        "assert 'code_indexer.cli_features' not in sys.modules\n"
        "assert 'code_indexer.server.features.loader' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_cheap_group_name_is_the_sdks() -> None:
    from cidx_sdk import FEATURES_ENTRY_POINT_GROUP

    assert cli_features.FEATURES_GROUP == FEATURES_ENTRY_POINT_GROUP
