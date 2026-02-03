"""Help CLI commands for code-indexer - Story #749.

Provides enhanced help commands showing mode-aware command availability,
command matrix, and feature discovery guidance.
"""

import click
from rich.console import Console
from rich.table import Table

from .disabled_commands import (
    COMMAND_COMPATIBILITY,
    get_available_commands_for_mode,
    detect_current_mode,
)

console = Console()


@click.group("help")
def help_group():
    """Help and documentation commands.

    Provides detailed information about CIDX commands, their availability
    in different modes, and feature discovery guidance.
    """
    pass


@help_group.command("commands")
def help_commands():
    """List all commands with mode availability.

    Shows a complete list of CIDX commands grouped by functionality,
    with indicators showing which modes each command supports.
    """
    current_mode = detect_current_mode()
    available = get_available_commands_for_mode(current_mode)

    console.print(f"\n[bold]CIDX Commands[/bold] (Current mode: {current_mode})\n")

    # Group commands by category
    categories = {
        "Core Operations": ["query", "index", "init", "status", "clean"],
        "SCIP Code Intelligence": ["scip"],
        "Git Operations": ["git"],
        "File Management": ["files"],
        "CI/CD Monitoring": ["cicd"],
        "Administration": ["admin", "auth"],
        "Server Management": ["server", "start", "stop", "watch", "watch-stop"],
        "Configuration": ["config", "fix-config", "teach-ai"],
        "Help": ["help"],
    }

    for category, commands in categories.items():
        # Filter to commands that exist in compatibility matrix
        existing = [c for c in commands if c in COMMAND_COMPATIBILITY]
        if not existing:
            continue

        console.print(f"[bold cyan]{category}:[/bold cyan]")
        for cmd in existing:
            compat = COMMAND_COMPATIBILITY.get(cmd, {})
            local_ok = compat.get("local", False)
            remote_ok = compat.get("remote", False)

            # Build mode indicator
            if local_ok and remote_ok:
                mode_str = "[green][ALL][/green]"
            elif local_ok:
                mode_str = "[blue][LOCAL][/blue]"
            elif remote_ok:
                mode_str = "[yellow][REMOTE][/yellow]"
            else:
                mode_str = "[dim][INIT][/dim]"

            # Mark if available in current mode
            if cmd in available:
                avail = "[green]v[/green]"
            else:
                avail = "[dim]x[/dim]"

            console.print(f"  {avail} {mode_str:20} {cmd}")
        console.print()

    console.print("[dim]Legend: v=available  x=unavailable in current mode[/dim]")
    console.print(
        "[dim]        [ALL]=Both modes  [LOCAL]=Local only  [REMOTE]=Remote only[/dim]"
    )


@help_group.command("matrix")
def help_matrix():
    """Show command availability matrix.

    Displays a table showing which commands are available in each
    operational mode (local, remote, proxy).
    """
    table = Table(title="CIDX Command Availability Matrix")

    table.add_column("Command", style="cyan")
    table.add_column("Local", justify="center")
    table.add_column("Remote", justify="center")
    table.add_column("Proxy", justify="center")

    # Sort commands alphabetically
    for cmd in sorted(COMMAND_COMPATIBILITY.keys()):
        compat = COMMAND_COMPATIBILITY[cmd]

        local = "[green]Y[/green]" if compat.get("local", False) else "[dim]-[/dim]"
        remote = "[green]Y[/green]" if compat.get("remote", False) else "[dim]-[/dim]"
        proxy = "[green]Y[/green]" if compat.get("proxy", False) else "[dim]-[/dim]"

        table.add_row(cmd, local, remote, proxy)

    console.print(table)
    console.print("\n[dim]Y = Available  - = Not available[/dim]")


@help_group.command("features")
def help_features():
    """Show feature overview and capabilities.

    Provides an overview of CIDX features grouped by functionality,
    with guidance on how to use each feature.
    """
    console.print("\n[bold]CIDX Feature Overview[/bold]\n")

    features = [
        {
            "name": "Semantic Search",
            "description": "AI-powered code search using vector embeddings",
            "command": 'cidx query "your search"',
            "modes": "local, remote",
        },
        {
            "name": "Full-Text Search",
            "description": "Fast text-based search with regex support",
            "command": 'cidx query "pattern" --fts',
            "modes": "local, remote",
        },
        {
            "name": "SCIP Code Intelligence",
            "description": "Call graphs, definitions, references, dependencies",
            "command": "cidx scip definition SYMBOL",
            "modes": "local, remote",
        },
        {
            "name": "Git History Search",
            "description": "Search through commit history and diffs",
            "command": 'cidx query "bug" --time-range-all',
            "modes": "local (with temporal index)",
        },
        {
            "name": "Git Operations",
            "description": "Remote repository git operations",
            "command": "cidx git status -r REPO",
            "modes": "remote only",
        },
        {
            "name": "File Management",
            "description": "Create, edit, delete files on remote repos",
            "command": "cidx files create PATH -r REPO",
            "modes": "remote only",
        },
        {
            "name": "CI/CD Monitoring",
            "description": "Monitor GitHub Actions and GitLab CI",
            "command": "cidx cicd github list OWNER/REPO",
            "modes": "remote only",
        },
    ]

    for feature in features:
        console.print(f"[bold cyan]{feature['name']}[/bold cyan]")
        console.print(f"  {feature['description']}")
        console.print(f"  [dim]Example:[/dim] {feature['command']}")
        console.print(f"  [dim]Available:[/dim] {feature['modes']}")
        console.print()

    console.print("[bold]Getting Started:[/bold]")
    console.print('  Local mode:  cidx init && cidx index && cidx query "search"')
    console.print('  Remote mode: cidx init --remote URL && cidx query "search"')
