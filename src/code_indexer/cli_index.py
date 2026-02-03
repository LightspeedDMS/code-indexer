"""Remote Index Management CLI commands for code-indexer - Story #656.

Provides remote index management commands for remote mode (CLI-REST-MCP parity).
"""

import sys
import click
from typing import Optional, List
from rich.console import Console
from rich.table import Table

from .disabled_commands import require_mode

console = Console()


def _load_remote_config_for_index() -> dict:
    """Load remote configuration for index commands.

    Returns:
        Configuration dict with server_url and credentials

    Raises:
        click.ClickException: If config file is missing or malformed
    """
    from .cli_utils.remote_command_base import _load_remote_config

    try:
        return _load_remote_config()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except ValueError as e:
        raise click.ClickException(str(e))


def _handle_index_error(e: Exception, json_output: bool) -> None:
    """Handle index command errors with appropriate output format."""
    from .cli_utils import format_json_error
    from .cli_utils.remote_command_base import handle_remote_error

    if json_output:
        console.print(format_json_error(str(e), type(e).__name__))
    else:
        console.print(f"[red]Error: {handle_remote_error(e, verbose=False)}[/red]")
    sys.exit(1)


# Valid index types
VALID_INDEX_TYPES = ["semantic", "fts", "temporal", "scip"]


class IndexTypeParamType(click.ParamType):
    """Custom parameter type for validating index types."""

    name = "index_type"

    def convert(self, value, param, ctx):
        if isinstance(value, str):
            types = [t.strip() for t in value.split(",")]
            for t in types:
                if t not in VALID_INDEX_TYPES:
                    self.fail(
                        f"Invalid index type '{t}'. Valid types: {', '.join(VALID_INDEX_TYPES)}",
                        param,
                        ctx,
                    )
            return types
        return value


INDEX_TYPE = IndexTypeParamType()


@click.group("remote-index")
@require_mode("remote")
def index_remote_group():
    """Remote index management commands (remote mode only).

    Manage indexing operations on the CIDX server including triggering
    re-indexing, checking index status, and adding new index types.
    """
    pass


@index_remote_group.command("trigger")
@click.argument("repository")
@click.option("--clear", is_flag=True, help="Clear existing indexes before re-indexing")
@click.option(
    "--types",
    type=INDEX_TYPE,
    help=f"Index types to build (comma-separated): {', '.join(VALID_INDEX_TYPES)}",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def index_trigger(
    repository: str,
    clear: bool,
    types: Optional[List[str]],
    json_output: bool,
):
    """Trigger indexing for a repository.

    Starts a background job to index or re-index the specified REPOSITORY.
    The job ID is returned and can be used to track progress.

    Examples:
        cidx remote-index trigger my-repo
        cidx remote-index trigger my-repo --clear
        cidx remote-index trigger my-repo --types semantic,fts
        cidx remote-index trigger my-repo --types scip --json
    """
    import asyncio
    from .api_clients.index_client import IndexAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_index()
        client = IndexAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(
            client.trigger(
                repository=repository,
                clear=clear,
                index_types=types,
            )
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            job_id = result.get("job_id", "unknown")
            status = result.get("status", "queued")
            console.print(f"[green]Indexing triggered for '{repository}'[/green]")
            console.print(f"[dim]Job ID:[/dim] {job_id}")
            console.print(f"[dim]Status:[/dim] {status}")
            if result.get("index_types"):
                console.print(f"[dim]Types:[/dim] {', '.join(result['index_types'])}")
            if clear:
                console.print("[yellow]Existing indexes will be cleared[/yellow]")

    except Exception as e:
        _handle_index_error(e, json_output)


@index_remote_group.command("status")
@click.argument("repository")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def index_status(repository: str, json_output: bool):
    """Get index status for a repository.

    Shows the status of all index types for the specified REPOSITORY,
    including completion status, file counts, and last update times.

    Examples:
        cidx remote-index status my-repo
        cidx remote-index status my-repo --json
    """
    import asyncio
    from .api_clients.index_client import IndexAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_index()
        client = IndexAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.status(repository))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[bold]Index Status for '{repository}'[/bold]\n")

            indexes = result.get("indexes", {})
            if not indexes:
                console.print("[dim]No index information available[/dim]")
                return

            table = Table()
            table.add_column("Index Type", style="cyan")
            table.add_column("Status", style="green")
            table.add_column("Details", style="dim")

            for index_type, info in indexes.items():
                status = info.get("status", "unknown")
                details = []

                if info.get("files_indexed"):
                    details.append(f"{info['files_indexed']} files")
                if info.get("projects_indexed"):
                    details.append(f"{info['projects_indexed']} projects")
                if info.get("progress"):
                    details.append(f"{info['progress']}% complete")
                if info.get("last_updated"):
                    details.append(f"updated {info['last_updated'][:10]}")

                # Color code status
                if status == "complete":
                    status_display = f"[green]{status}[/green]"
                elif status == "in_progress":
                    status_display = f"[yellow]{status}[/yellow]"
                elif status == "failed":
                    status_display = f"[red]{status}[/red]"
                else:
                    status_display = status

                table.add_row(
                    index_type,
                    status_display,
                    ", ".join(details) if details else "-",
                )

            console.print(table)

    except Exception as e:
        _handle_index_error(e, json_output)


@index_remote_group.command("add-type")
@click.argument("repository")
@click.argument("type", type=click.Choice(VALID_INDEX_TYPES))
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def index_add_type(repository: str, type: str, json_output: bool):
    """Add an index type to a repository.

    Enables and triggers building of the specified index TYPE for
    the REPOSITORY. Valid types: semantic, fts, temporal, scip.

    Examples:
        cidx remote-index add-type my-repo temporal
        cidx remote-index add-type my-repo scip --json
    """
    import asyncio
    from .api_clients.index_client import IndexAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_index()
        client = IndexAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.add_type(repository, type))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Index type '{type}' added to '{repository}'[/green]")
            if result.get("job_id"):
                console.print(f"[dim]Job ID:[/dim] {result['job_id']}")

    except Exception as e:
        _handle_index_error(e, json_output)
