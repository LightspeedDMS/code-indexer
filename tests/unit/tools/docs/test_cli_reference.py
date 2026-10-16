"""Tests for the CLI reference generator (Story #2082)."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import click
import pytest

from tools.docs import cli_reference as gen
from tools.docs._runner import REPO_ROOT

_HEADING = re.compile(r"^## `([^`]+)`$", re.MULTILINE)
_LINK = re.compile(r"\]\(([A-Za-z0-9_-]+\.md)?#([a-z0-9_-]+)\)")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Verbose | loud")
def tool() -> None:
    """Top tool.

    \b
    Line kept
      indented
    \f
    Hidden tail
    """


@tool.command()
@click.argument("query")
@click.argument("paths", nargs=-1)
@click.option("--limit", type=int, default=10, help="Max")
@click.option("--mode", type=click.Choice(["a", "b"]), default="a")
@click.option("--depth", type=click.IntRange(1, 5))
@click.option("--tag", multiple=True)
@click.option("--name", required=True)
@click.option("--desc", default="")
@click.option("--daemon/--no-daemon", default=True)
@click.option("--secret", hidden=True)
def search(**_: object) -> None:
    """Search things. Example: ```code``` here."""


@tool.group()
def admin() -> None:
    """Admin."""


@admin.command("users")
def users() -> None:
    pass


@tool.command("internal-probe", hidden=True)
def internal_probe() -> None:
    """Hidden top-level command."""


@admin.command("internal-reset", hidden=True)
def internal_reset() -> None:
    """Hidden subcommand."""


def test_hidden_commands_are_not_documented() -> None:
    pages = gen.render(root=tool, prog_name="tool")
    for text in pages.values():
        assert "internal-probe" not in text
        assert "internal-reset" not in text
    assert "(4 in all)" in pages["README.md"]


def _github_anchor(heading: str) -> str:
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")


def _section(page: str, path: str) -> str:
    start = page.index(f"## `{path}`\n")
    nxt = page.find("\n## `", start + 1)
    return page[start : nxt if nxt != -1 else len(page)]


@pytest.fixture(scope="module")
def synthetic() -> Dict[str, str]:
    return gen.render(root=tool, prog_name="tool")


@pytest.fixture(scope="module")
def real() -> Dict[str, str]:
    return gen.render()


def test_synthetic_page_layout(synthetic: Dict[str, str]) -> None:
    assert set(synthetic) == {"README.md", "admin.md", "commands.md"}
    assert _HEADING.findall(synthetic["README.md"]) == ["tool"]
    assert _HEADING.findall(synthetic["admin.md"]) == ["tool admin", "tool admin users"]
    assert _HEADING.findall(synthetic["commands.md"]) == ["tool search"]


def test_index_links_each_top_level_command_to_its_page(
    synthetic: Dict[str, str],
) -> None:
    root = _section(synthetic["README.md"], "tool")
    assert "| [`admin`](admin.md#tool-admin) | Admin. |" in root
    assert "| [`search`](commands.md#tool-search) |" in root


def test_group_page_contents_and_back_link(synthetic: Dict[str, str]) -> None:
    page = synthetic["admin.md"]
    assert (
        "- [`tool admin`](#tool-admin)\n  - [`tool admin users`](#tool-admin-users)\n"
    ) in page
    assert "[CLI Reference](README.md)" in page
    assert "| [`users`](#tool-admin-users) |" in _section(page, "tool admin")
    assert "[CLI Reference](README.md)" in synthetic["commands.md"]


def test_usage_lines(synthetic: Dict[str, str]) -> None:
    assert "Usage: `tool [OPTIONS] COMMAND [ARGS]...`" in synthetic["README.md"]
    assert "Usage: `tool search [OPTIONS] QUERY [PATHS]...`" in synthetic["commands.md"]
    assert "Usage: `tool admin users [OPTIONS]`" in synthetic["admin.md"]


def test_help_text_cleaned_and_fenced(synthetic: Dict[str, str]) -> None:
    root = _section(synthetic["README.md"], "tool")
    assert "```text\nTop tool.\n\nLine kept\n  indented\n```" in root
    for page in synthetic.values():
        assert "\b" not in page
        assert "Hidden tail" not in page


def test_help_containing_backtick_fence_uses_longer_fence(
    synthetic: Dict[str, str],
) -> None:
    section = _section(synthetic["commands.md"], "tool search")
    assert "````text\nSearch things. Example: ```code``` here.\n````" in section


def test_option_rows(synthetic: Dict[str, str]) -> None:
    section = _section(synthetic["commands.md"], "tool search")
    expected: List[str] = [
        "| `--limit` | integer | `10` | no | Max |",
        "| `--mode` | choice: `a`, `b` | `a` | no |  |",
        "| `--depth` | integer range (`>=1`, `<=5`) |  | no |  |",
        "| `--tag` | text (repeatable) |  | no |  |",
        "| `--name` | text |  | yes |  |",
        '| `--desc` | text | `""` | no |  |',
        "| `--daemon` / `--no-daemon` | flag | `true` | no |  |",
    ]
    for row in expected:
        assert row in section, row
    assert "--secret" not in section
    assert (
        "| `--verbose`, `-v` | flag |  | no | Verbose \\| loud |"
        in synthetic["README.md"]
    )


def test_argument_rows(synthetic: Dict[str, str]) -> None:
    section = _section(synthetic["commands.md"], "tool search")
    assert "| `QUERY` | text | yes |" in section
    assert "| `PATHS` | text (any number) | no |" in section


def test_command_without_help_or_params(synthetic: Dict[str, str]) -> None:
    section = _section(synthetic["admin.md"], "tool admin users")
    assert "```" not in section
    assert "| Option |" not in section


def test_rare_parameter_shapes() -> None:
    @click.command(deprecated=True)
    @click.option("--when", default=lambda: "now")
    @click.option("--pair", type=int, nargs=2)
    @click.option("--kinds", multiple=True, default=("a", "b"))
    def old(**_: object) -> None:
        """Old command."""

    pages = gen.render(root=old, prog_name="old")
    assert set(pages) == {"README.md"}
    text = pages["README.md"]
    assert "\nDeprecated.\n" in text
    assert "| `--when` | text | (computed at runtime) | no |  |" in text
    assert "| `--pair` | integer (2 values) |  | no |  |" in text
    assert "| `--kinds` | text (repeatable) | `a`, `b` | no |  |" in text


def test_group_named_like_a_reserved_page_fails_loudly() -> None:
    @click.group()
    def top() -> None:
        """Top."""

    @top.group("commands")
    def clash() -> None:
        """Clash."""

    with pytest.raises(ValueError, match="commands"):
        gen.render(root=top, prog_name="top")


def _walk(cmd: click.Command, ctx: click.Context, out: List[str]) -> None:
    out.append(ctx.command_path)
    if isinstance(cmd, click.Group):
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            assert sub is not None
            if not sub.hidden:
                _walk(sub, click.Context(sub, info_name=name, parent=ctx), out)


def test_real_cli_every_command_once(real: Dict[str, str]) -> None:
    from code_indexer.cli import cli

    paths: List[str] = []
    _walk(cli, click.Context(cli, info_name="cidx"), paths)
    headings = [h for page in real.values() for h in _HEADING.findall(page)]
    assert sorted(headings) == sorted(paths)
    root_ctx = click.Context(cli, info_name="cidx")
    groups = {
        f"{name}.md"
        for name in cli.list_commands(root_ctx)
        if isinstance(cli.get_command(root_ctx, name), click.Group)
    }
    assert set(real) == {"README.md", "commands.md"} | groups
    root = _section(real["README.md"], "cidx")
    assert "`--version`" in root
    assert "`--verbose`, `-v`" in root
    assert "| `--fts` |" in _section(real["commands.md"], "cidx query")


def test_real_cli_every_anchor_link_resolves(real: Dict[str, str]) -> None:
    anchors = {
        page: {_github_anchor(h) for h in _HEADING.findall(text)}
        for page, text in real.items()
    }
    links = 0
    for page, text in real.items():
        for target_page, anchor in _LINK.findall(text):
            links += 1
            assert anchor in anchors[target_page or page], (page, target_page, anchor)
    assert links >= 2 * len(anchors)


def test_every_page_starts_with_generated_header(real: Dict[str, str]) -> None:
    for text in real.values():
        assert text.splitlines()[0] == gen.generated_header(gen.COMMAND)
    assert gen.COMMAND == "python3 -m tools.docs.cli_reference"


def test_rendering_real_cli_does_not_import_tantivy() -> None:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    probe = (
        "import sys\n"
        "from tools.docs import cli_reference\n"
        "cli_reference.render()\n"
        "print('tantivy' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_render_is_deterministic(real: Dict[str, str]) -> None:
    assert gen.render() == real


def test_check_fails_on_stale_and_passes_after_generation(tmp_path: Path) -> None:
    target = tmp_path / "cli"
    target.mkdir()
    (target / "README.md").write_text("# old hand-written reference\n", "utf-8")
    assert gen.main(["--check"], target_dir=target) == 1
    assert gen.main([], target_dir=target) == 0
    assert gen.main(["--check"], target_dir=target) == 0
    for name, text in gen.render().items():
        assert (target / name).read_text(encoding="utf-8") == text
