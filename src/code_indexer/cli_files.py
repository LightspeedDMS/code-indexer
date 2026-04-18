"""File CLI commands for code-indexer - Story #738.

Provides file CRUD commands for remote mode (CLI-REST-MCP parity).
"""

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from .disabled_commands import require_mode

console = Console()


def _load_remote_config_for_files() -> dict:
    """Load remote configuration for file commands.

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


def _handle_files_error(e: Exception, json_output: bool) -> None:
    """Handle file command errors with appropriate output format."""
    from .cli_utils import format_json_error
    from .cli_utils.remote_command_base import handle_remote_error

    if json_output:
        console.print(format_json_error(str(e), type(e).__name__))
    else:
        console.print(f"[red]Error: {handle_remote_error(e, verbose=False)}[/red]")
    sys.exit(1)


def _resolve_file_content(
    content: Optional[str],
    from_file: Optional[str],
    json_output: bool,
) -> str:
    """Validate content source arguments and return file content as a string.

    Handles mutual-exclusion validation and local file reading.
    Arbitrary local paths are intentionally permitted: this is a CLI tool
    running as the user, reading files the user explicitly specifies.
    Path.resolve() normalises the path (removes .., symlinks) so callers
    work with a canonical absolute path.

    Args:
        content: Inline content string (None if not supplied; empty string is valid).
        from_file: Path to a local file to read (None if not supplied).
        json_output: Controls error output format.

    Returns:
        File content as a string.

    Raises:
        SystemExit: On validation failure or unreadable file.
    """
    from .cli_utils import format_json_error

    if content is None and from_file is None:
        msg = "Must provide either --content or --from-file"
        if json_output:
            console.print(format_json_error(msg, "ValidationError"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    if content is not None and from_file is not None:
        msg = "Cannot use both --content and --from-file"
        if json_output:
            console.print(format_json_error(msg, "ValidationError"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    if from_file is not None:
        resolved = Path(from_file).resolve()
        try:
            return resolved.read_text()
        except Exception as e:
            if json_output:
                console.print(format_json_error(str(e), "FileReadError"))
            else:
                console.print(f"[red]Error reading file: {e}[/red]")
            sys.exit(1)

    # content is guaranteed non-None here: both-None and both-set cases handled above
    assert content is not None
    return content


@click.group("files")
@require_mode("remote")
def files_group():
    """File operations for remote repositories (remote mode only).

    Provides file create, edit, and delete operations for repositories
    indexed on the CIDX server.
    """
    pass


@files_group.command("create")
@click.argument("path")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--content", "-c", default=None, help="File content (inline)")
@click.option(
    "--from-file",
    "-f",
    type=click.Path(exists=True),
    default=None,
    help="Read content from local file",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def files_create(
    path: str,
    repository: str,
    content: Optional[str],
    from_file: Optional[str],
    json_output: bool,
):
    """Create a new file in the repository.

    PATH is the path to create the file at in the repository.

    You must provide either --content or --from-file to specify the file content.

    Examples:

        cidx files create src/new_file.py -r myrepo --content "print('hello')"

        cidx files create config.yaml -r myrepo --from-file ./local_config.yaml
    """
    from .api_clients.file_client import FileAPIClient
    from .cli_utils import format_json_success

    file_content = _resolve_file_content(content, from_file, json_output)

    try:
        config = _load_remote_config_for_files()
        client = FileAPIClient(config["server_url"], config["credentials"])
        result = client.create_file(repository, path, file_content)

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Created:[/green] {result['file_path']}")
            console.print(f"  Hash: {result.get('content_hash', 'N/A')}")
            if "size_bytes" in result:
                console.print(f"  Size: {result['size_bytes']} bytes")

    except Exception as e:
        _handle_files_error(e, json_output)


@files_group.command("edit")
@click.argument("path")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--old", required=True, help="String to replace")
@click.option("--new", required=True, help="Replacement string")
@click.option("--content-hash", help="Expected content hash (optimistic locking)")
@click.option("--replace-all", is_flag=True, help="Replace all occurrences")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def files_edit(
    path: str,
    repository: str,
    old: str,
    new: str,
    content_hash: Optional[str],
    replace_all: bool,
    json_output: bool,
):
    """Edit a file using string replacement.

    PATH is the path to the file in the repository.

    Uses string replacement to edit the file. Specify the string to find with
    --old and the replacement with --new.

    Optionally provide --content-hash for optimistic locking to ensure the file
    hasn't been modified since you last read it.

    Examples:

        cidx files edit src/app.py -r myrepo --old "old_func" --new "new_func"

        cidx files edit config.py -r myrepo --old "DEBUG = True" --new "DEBUG = False" --replace-all

        cidx files edit app.py -r myrepo --old "v1" --new "v2" --content-hash abc123
    """
    from .api_clients.file_client import FileAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_files()
        client = FileAPIClient(config["server_url"], config["credentials"])
        result = client.edit_file(
            repository,
            path,
            old_string=old,
            new_string=new,
            content_hash=content_hash,
            replace_all=replace_all,
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            changes = result.get("changes_made", 0)
            console.print(f"[green]Edited:[/green] {result['file_path']}")
            console.print(f"  Changes made: {changes}")
            console.print(f"  New hash: {result.get('content_hash', 'N/A')}")

    except Exception as e:
        _handle_files_error(e, json_output)


@files_group.command("delete")
@click.argument("path")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--content-hash", help="Expected content hash (optimistic locking)")
@click.option("--confirm", is_flag=True, help="Confirm deletion (required for safety)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def files_delete(
    path: str,
    repository: str,
    content_hash: Optional[str],
    confirm: bool,
    json_output: bool,
):
    """Delete a file from the repository.

    PATH is the path to the file in the repository.

    WARNING: This is a destructive operation. The --confirm flag is required.

    Optionally provide --content-hash for optimistic locking to ensure the file
    hasn't been modified since you last read it.

    Examples:

        cidx files delete obsolete.py -r myrepo --confirm

        cidx files delete old_config.yaml -r myrepo --content-hash abc123 --confirm
    """
    from .api_clients.file_client import FileAPIClient
    from .cli_utils import format_json_success, format_json_error

    # Require confirmation for destructive operation
    if not confirm:
        msg = "File deletion is destructive. Use --confirm to proceed."
        if json_output:
            console.print(format_json_error(msg, "ConfirmationRequired"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    try:
        config = _load_remote_config_for_files()
        client = FileAPIClient(config["server_url"], config["credentials"])
        result = client.delete_file(repository, path, content_hash=content_hash)

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Deleted:[/green] {result['file_path']}")
            if "deleted_at" in result:
                console.print(f"  Deleted at: {result['deleted_at']}")

    except Exception as e:
        _handle_files_error(e, json_output)
