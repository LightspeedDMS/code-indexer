"""SSH Key Management CLI commands for code-indexer - Story #656.

Provides SSH key management commands for remote mode (CLI-REST-MCP parity).
"""

import sys
import click
from typing import Optional
from rich.console import Console
from rich.table import Table

from .disabled_commands import require_mode

console = Console()


def _load_remote_config_for_keys() -> dict:
    """Load remote configuration for keys commands.

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


def _handle_keys_error(e: Exception, json_output: bool) -> None:
    """Handle keys command errors with appropriate output format."""
    from .cli_utils import format_json_error
    from .cli_utils.remote_command_base import handle_remote_error

    if json_output:
        console.print(format_json_error(str(e), type(e).__name__))
    else:
        console.print(f"[red]Error: {handle_remote_error(e, verbose=False)}[/red]")
    sys.exit(1)


@click.group("keys")
@require_mode("remote")
def keys_group():
    """SSH key management commands (remote mode only).

    Manage SSH keys for accessing private repositories on the CIDX server.
    Keys can be created, listed, deleted, and assigned to specific hostnames.
    """
    pass


@keys_group.command("create")
@click.argument("name")
@click.option("--email", "-e", required=True, help="Email address for the key")
@click.option(
    "--key-type",
    "-t",
    type=click.Choice(["ed25519", "rsa"]),
    default="ed25519",
    help="Key type (default: ed25519)",
)
@click.option("--description", "-d", help="Optional description for the key")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def keys_create(
    name: str,
    email: str,
    key_type: str,
    description: Optional[str],
    json_output: bool,
):
    """Create a new SSH key.

    Creates a new SSH key pair with the specified NAME. The public key
    will be stored on the server and can be retrieved with show-public.

    Examples:
        cidx keys create github-key --email user@example.com
        cidx keys create deploy-key --email ci@company.com --key-type rsa
    """
    import asyncio
    from .api_clients.ssh_client import SSHAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_keys()
        client = SSHAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(
            client.create_key(
                name=name,
                email=email,
                key_type=key_type,
                description=description,
            )
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]SSH key '{name}' created successfully[/green]")
            console.print(f"[dim]Type:[/dim] {result.get('key_type', key_type)}")
            console.print(f"[dim]Fingerprint:[/dim] {result.get('fingerprint', 'N/A')}")
            if result.get("public_key"):
                console.print("\n[bold]Public Key:[/bold]")
                console.print(result["public_key"])

    except Exception as e:
        _handle_keys_error(e, json_output)


@keys_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def keys_list(json_output: bool):
    """List all SSH keys.

    Shows all SSH keys stored on the CIDX server with their
    type, fingerprint, and creation date.

    Examples:
        cidx keys list
        cidx keys list --json
    """
    import asyncio
    from .api_clients.ssh_client import SSHAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_keys()
        client = SSHAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.list_keys())

        if json_output:
            console.print(format_json_success(result))
        else:
            keys = result.get("keys", [])
            if not keys:
                console.print("[dim]No SSH keys found[/dim]")
                return

            table = Table(title="SSH Keys")
            table.add_column("Name", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Fingerprint", style="dim")
            table.add_column("Created", style="dim")

            for key in keys:
                table.add_row(
                    key.get("name", ""),
                    key.get("key_type", ""),
                    (
                        key.get("fingerprint", "")[:20] + "..."
                        if key.get("fingerprint")
                        else ""
                    ),
                    key.get("created_at", "")[:10] if key.get("created_at") else "",
                )

            console.print(table)

    except Exception as e:
        _handle_keys_error(e, json_output)


@keys_group.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def keys_delete(name: str, yes: bool, json_output: bool):
    """Delete an SSH key.

    Permanently deletes the SSH key with the specified NAME.
    This action cannot be undone.

    Examples:
        cidx keys delete old-key --yes
        cidx keys delete unused-key
    """
    import asyncio
    from .api_clients.ssh_client import SSHAPIClient
    from .cli_utils import format_json_success

    # Require confirmation unless --yes is provided
    if not yes and not json_output:
        if not click.confirm(f"Delete SSH key '{name}'? This cannot be undone"):
            console.print("[yellow]Aborted[/yellow]")
            return

    try:
        config = _load_remote_config_for_keys()
        client = SSHAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.delete_key(name))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]SSH key '{name}' deleted successfully[/green]")

    except Exception as e:
        _handle_keys_error(e, json_output)


@keys_group.command("show-public")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def keys_show_public(name: str, json_output: bool):
    """Show the public key for an SSH key.

    Displays the public key in OpenSSH format, which can be added
    to Git hosting services (GitHub, GitLab, etc.).

    Examples:
        cidx keys show-public github-key
        cidx keys show-public deploy-key --json
    """
    import asyncio
    from .api_clients.ssh_client import SSHAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_keys()
        client = SSHAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.show_public_key(name))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[bold]Public key for '{name}':[/bold]")
            console.print(result.get("public_key", ""))

    except Exception as e:
        _handle_keys_error(e, json_output)


@keys_group.command("assign")
@click.argument("name")
@click.argument("hostname")
@click.option("--force", "-f", is_flag=True, help="Replace existing assignment")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def keys_assign(name: str, hostname: str, force: bool, json_output: bool):
    """Assign an SSH key to a hostname.

    Associates the SSH key NAME with the specified HOSTNAME.
    The key will be used when accessing repositories on that host.

    Examples:
        cidx keys assign github-key github.com
        cidx keys assign gitlab-key gitlab.company.com --force
    """
    import asyncio
    from .api_clients.ssh_client import SSHAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_keys()
        client = SSHAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.assign_key(name, hostname, force=force))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]SSH key '{name}' assigned to '{hostname}'[/green]")
            if result.get("replaced_existing"):
                console.print("[dim]Replaced existing key assignment[/dim]")

    except Exception as e:
        _handle_keys_error(e, json_output)
