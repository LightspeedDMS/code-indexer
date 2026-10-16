"""CLI help strings are plain text.

Invariant: no command's or option's help string contains emoji. The CLI
reference under docs/reference/cli/ is generated from these strings, and
project Markdown carries no emoji or decorative characters.
"""

from __future__ import annotations

import re
from typing import Iterator, Tuple

import click

from code_indexer.cli import cli

_EMOJI_RE = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️]")


def _help_strings(command: click.Command) -> str:
    parts = [command.help or "", command.short_help or ""]
    parts += [getattr(param, "help", None) or "" for param in command.params]
    return "\n".join(parts)


def _walk(command: click.Command, parent: click.Context) -> Iterator[Tuple[str, str]]:
    ctx = click.Context(command, info_name=command.name, parent=parent)
    yield ctx.command_path, _help_strings(command)
    if isinstance(command, click.Group):
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, ctx)


def test_every_command_help_string_is_plain_text() -> None:
    root = click.Context(cli, info_name="cidx")
    offenders = {
        path: _EMOJI_RE.findall(text)
        for path, text in _walk(cli, root)
        if _EMOJI_RE.search(text)
    }

    assert offenders == {}
