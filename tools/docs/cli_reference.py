"""Generate ``docs/reference/cli/`` from the click command tree.

Output: ``README.md`` (the root command, its global options and an index of
top-level commands), one page per top-level command group, and
``commands.md`` for the top-level commands that have no subcommands.

The tree is ``code_indexer.cli:cli``, the group that both the ``cidx`` and
``code-indexer`` entry points run for ``--help``. Importing it needs no
network, credentials or server, and must not pull in tantivy (CLI startup
budget); this module does not change any CLI import.

Usage (from the repository root)::

    PYTHONPATH=src python3 -m tools.docs.cli_reference [--check]
"""

import inspect
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import click

from tools.docs._runner import REPO_ROOT, generated_header, md_cell, run

COMMAND = "python3 -m tools.docs.cli_reference"
TARGET_DIR = REPO_ROOT / "docs" / "reference" / "cli"
INDEX_PAGE = "README.md"
STANDALONE_PAGE = "commands.md"

_BACKSPACE_MARKERS = ("\b", "\\b")  # click's "do not rewrap" paragraph marker
_ANCHOR_DROP = re.compile(r"[^a-z0-9 _-]")

Entry = Tuple[click.Command, click.Context, int]


def _anchor(path: str) -> str:
    """GitHub heading anchor for a heading whose text is ``path`` in backticks."""
    return _ANCHOR_DROP.sub("", path.lower()).replace(" ", "-")


def _visible_subcommands(
    group: click.Group, ctx: click.Context
) -> List[Tuple[str, click.Command]]:
    """Subcommands sorted by name; ``hidden=True`` commands are left out."""
    visible = []
    for name in sorted(group.list_commands(ctx)):
        sub = group.get_command(ctx, name)
        if sub is None:
            raise ValueError(
                f"{ctx.command_path} lists {name!r} but has no such command"
            )
        if not sub.hidden:
            visible.append((name, sub))
    return visible


def _walk(cmd: click.Command, ctx: click.Context, depth: int = 0) -> Iterator[Entry]:
    """Depth-first over the visible tree, subcommands sorted by name."""
    yield cmd, ctx, depth
    if isinstance(cmd, click.Group):
        for name, sub in _visible_subcommands(cmd, ctx):
            yield from _walk(
                sub, click.Context(sub, info_name=name, parent=ctx), depth + 1
            )


def _help_text(cmd: click.Command) -> str:
    raw = (cmd.help or "").split("\f", 1)[0]  # click hides text after \f
    lines = [
        line.rstrip()
        for line in inspect.cleandoc(raw).splitlines()
        if line.strip() not in _BACKSPACE_MARKERS
    ]
    return "\n".join(lines).strip("\n")


def _fenced(text: str) -> List[str]:
    fence = "```"
    while fence in text:
        fence += "`"
    return [f"{fence}text", text, fence]


def _type_text(param: click.Parameter) -> str:
    if isinstance(param, click.Option) and param.is_flag:
        return "flag"
    ptype = param.type
    if isinstance(ptype, click.Choice):
        text = "choice: " + ", ".join(f"`{choice}`" for choice in ptype.choices)
    elif isinstance(ptype, (click.IntRange, click.FloatRange)):
        bounds = []
        if ptype.min is not None:
            bounds.append(f"`{'>' if ptype.min_open else '>='}{ptype.min}`")
        if ptype.max is not None:
            bounds.append(f"`{'<' if ptype.max_open else '<='}{ptype.max}`")
        text = f"{ptype.name} ({', '.join(bounds)})"
    else:
        text = ptype.name
    if param.multiple:
        text += " (repeatable)"
    if param.nargs == -1:
        text += " (any number)"
    elif param.nargs > 1:
        text += f" ({param.nargs} values)"
    return text


def _default_text(param: click.Parameter) -> str:
    default = param.default
    if callable(default):
        return "(computed at runtime)"
    if default is None or default == () or default == []:
        return ""
    if isinstance(param, click.Option) and param.is_flag and default is False:
        return ""
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if isinstance(default, (list, tuple)):
        return ", ".join(f"`{value}`" for value in default)
    if default == "":
        return '`""`'
    return f"`{default}`"


def _option_names(option: click.Option) -> str:
    names = ", ".join(f"`{opt}`" for opt in option.opts)
    if option.secondary_opts:
        names += " / " + ", ".join(f"`{opt}`" for opt in option.secondary_opts)
    return names


def _param_tables(cmd: click.Command) -> List[str]:
    arguments = [p for p in cmd.params if isinstance(p, click.Argument)]
    options = [p for p in cmd.params if isinstance(p, click.Option) and not p.hidden]
    lines: List[str] = []
    if arguments:
        lines += [
            "",
            "| Argument | Type | Required |",
            "|----------|------|----------|",
        ]
        for arg in arguments:
            lines.append(
                f"| `{arg.human_readable_name}` | {md_cell(_type_text(arg))} "
                f"| {'yes' if arg.required else 'no'} |"
            )
    if options:
        lines += [
            "",
            "| Option | Type | Default | Required | Description |",
            "|--------|------|---------|----------|-------------|",
        ]
        for opt in options:
            lines.append(
                f"| {_option_names(opt)} | {md_cell(_type_text(opt))} "
                f"| {_default_text(opt)} | {'yes' if opt.required else 'no'} "
                f"| {md_cell(opt.help or '')} |"
            )
    return lines


class _Pages:
    """Which page each command path lives on, and links between them."""

    def __init__(self, entries: List[Entry]) -> None:
        self.page_of: Dict[str, str] = {}
        top_page = INDEX_PAGE
        for cmd, ctx, depth in entries:
            if depth == 1:
                top_page = STANDALONE_PAGE
                if isinstance(cmd, click.Group):
                    top_page = f"{ctx.info_name}.md"
                    if top_page in (INDEX_PAGE, STANDALONE_PAGE):
                        raise ValueError(
                            f"Group {ctx.command_path!r} would overwrite the "
                            f"reserved page {top_page}"
                        )
            self.page_of[ctx.command_path] = INDEX_PAGE if depth == 0 else top_page

    def link(self, path: str, current: str) -> str:
        page = self.page_of[path]
        prefix = "" if page == current else page
        return f"{prefix}#{_anchor(path)}"


def _subcommand_table(
    group: click.Group, ctx: click.Context, pages: _Pages, current: str
) -> List[str]:
    lines = ["", "| Command | Description |", "|---------|-------------|"]
    for name, sub in _visible_subcommands(group, ctx):
        link = pages.link(f"{ctx.command_path} {name}", current)
        lines.append(
            f"| [`{name}`]({link}) | {md_cell(sub.get_short_help_str(limit=300))} |"
        )
    return lines


def _command_section(
    cmd: click.Command, ctx: click.Context, pages: _Pages, current: str
) -> List[str]:
    usage = " ".join([ctx.command_path] + cmd.collect_usage_pieces(ctx))
    lines = ["", f"## `{ctx.command_path}`", "", f"Usage: `{usage}`"]
    if cmd.deprecated:
        lines += ["", "Deprecated."]
    help_text = _help_text(cmd)
    if help_text:
        lines += [""] + _fenced(help_text)
    lines += _param_tables(cmd)
    if isinstance(cmd, click.Group):
        lines += _subcommand_table(cmd, ctx, pages, current)
    return lines


def _sub_page(
    page: str, title: str, intro: str, entries: List[Entry], pages: _Pages
) -> str:
    lines = [generated_header(COMMAND), "", f"# {title}", "", intro, ""]
    base = min(depth for _, _, depth in entries)
    for _, ctx, depth in entries:
        lines.append(
            f"{'  ' * (depth - base)}- [`{ctx.command_path}`]"
            f"({pages.link(ctx.command_path, page)})"
        )
    for cmd, ctx, _ in entries:
        lines += _command_section(cmd, ctx, pages, page)
    return "\n".join(lines) + "\n"


def render(
    root: Optional[click.Command] = None, prog_name: str = "cidx"
) -> Dict[str, str]:
    """Return ``{page name: Markdown}`` for the CLI reference."""
    if root is None:
        from code_indexer.cli import cli

        root = cli
    root_ctx = click.Context(root, info_name=prog_name)
    entries = list(_walk(root, root_ctx))
    pages = _Pages(entries)
    back = "All commands: [CLI Reference](README.md)."

    index = [
        generated_header(COMMAND),
        "",
        "# CLI Reference",
        "",
        f"Every `{prog_name}` command and subcommand ({len(entries)} in all), "
        "generated from the click command tree (`cli` in "
        "`src/code_indexer/cli.py`). `cidx` and `code-indexer` are the same "
        "program. Every command also accepts `--help`, which prints the help "
        "text shown here. This page covers the root command and its global "
        "options; the table at the end links each top-level command to its page.",
    ]
    index += _command_section(root, root_ctx, pages, INDEX_PAGE)
    rendered = {INDEX_PAGE: "\n".join(index) + "\n"}

    by_page: Dict[str, List[Entry]] = {}
    for entry in entries[1:]:
        by_page.setdefault(pages.page_of[entry[1].command_path], []).append(entry)
    for page, page_entries in sorted(by_page.items()):
        if page == STANDALONE_PAGE:
            title = "Standalone commands"
            intro = f"Top-level `{prog_name}` commands without subcommands. {back}"
        else:
            path = page_entries[0][1].command_path
            title = f"{path} commands"
            intro = f"The `{path}` command group. {back}"
        rendered[page] = _sub_page(page, title, intro, page_entries, pages)
    return rendered


def main(argv: Optional[Sequence[str]] = None, target_dir: Path = TARGET_DIR) -> int:
    return run(argv, target_dir=target_dir, render=render, command=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
