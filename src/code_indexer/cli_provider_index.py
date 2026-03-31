"""CLI commands for provider-specific index management (Story #490)."""

import click
from rich.console import Console
from rich.table import Table


@click.group("provider-index")
def provider_index_group():
    """Manage provider-specific semantic indexes."""
    pass


@provider_index_group.command("list-providers")
def list_providers():
    """List configured embedding providers with valid API keys."""
    from .services.embedding_factory import EmbeddingProviderFactory
    from .config import ConfigManager

    console = Console()
    config = ConfigManager().load()

    configured = EmbeddingProviderFactory.get_configured_providers(config)
    provider_info = EmbeddingProviderFactory.get_provider_info()

    if not configured:
        console.print(
            "[yellow]No embedding providers configured with valid API keys[/yellow]"
        )
        return

    table = Table(title="Configured Embedding Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("API Key Env", style="dim")

    for name in configured:
        info = provider_info.get(name, {})
        table.add_row(
            name,
            info.get("default_model", "unknown"),
            info.get("api_key_env", ""),
        )

    console.print(table)


@provider_index_group.command("status")
@click.option("--repo", required=True, help="Repository path or alias")
def status(repo: str):
    """Show per-provider index status for a repository."""
    from pathlib import Path
    from .config import ConfigManager

    console = Console()
    config = ConfigManager().load()

    # Require an existing directory path; aliases are not resolved silently
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        raise click.ClickException(
            f"Repository path not found or not a directory: {repo_path}"
        )

    from code_indexer.server.services.provider_index_service import ProviderIndexService

    service = ProviderIndexService(config=config)
    provider_status = service.get_provider_index_status(str(repo_path), repo)

    if not provider_status:
        console.print("[yellow]No providers configured[/yellow]")
        return

    table = Table(title=f"Provider Index Status: {repo}")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Vectors", justify="right")
    table.add_column("Model", style="green")
    table.add_column("Last Indexed", style="dim")

    for pname, pstatus in provider_status.items():
        exists = pstatus.get("exists", False)
        status_str = "[green]indexed[/green]" if exists else "[dim]not indexed[/dim]"
        vectors = str(pstatus.get("vector_count", 0)) if exists else "-"
        model = pstatus.get("model", "")
        last_indexed = pstatus.get("last_indexed", "-") or "-"

        table.add_row(pname, status_str, vectors, model, last_indexed)

    console.print(table)


@provider_index_group.command("add")
@click.option("--provider", required=True, help="Embedding provider name")
@click.option("--repo", required=True, help="Repository path")
def add(provider: str, repo: str):
    """Add a provider's semantic index to a repository."""
    import subprocess
    from pathlib import Path
    from .config import ConfigManager

    console = Console()
    repo_path = Path(repo).resolve()

    if not repo_path.is_dir():
        raise click.ClickException(
            f"Repository path not found or not a directory: {repo_path}"
        )

    config = ConfigManager().load()

    from code_indexer.server.services.provider_index_service import ProviderIndexService

    service = ProviderIndexService(config=config)
    error = service.validate_provider(provider)
    if error:
        raise click.ClickException(error)

    console.print(f"Building {provider} index for {repo_path.name}...")

    try:
        result = subprocess.run(
            ["cidx", "index", "--provider", provider],
            cwd=str(repo_path),
            capture_output=False,
        )
    except FileNotFoundError:
        raise click.ClickException("cidx not found; install it or add it to PATH")

    if result.returncode == 0:
        console.print(f"[green]Successfully built {provider} index[/green]")
    else:
        raise click.ClickException(
            f"Failed to build {provider} index (exit code {result.returncode})"
        )


@provider_index_group.command("recreate")
@click.option("--provider", required=True, help="Embedding provider name")
@click.option("--repo", required=True, help="Repository path")
def recreate(provider: str, repo: str):
    """Recreate a provider's semantic index from scratch."""
    import subprocess
    from pathlib import Path
    from .config import ConfigManager

    console = Console()
    repo_path = Path(repo).resolve()

    if not repo_path.is_dir():
        raise click.ClickException(
            f"Repository path not found or not a directory: {repo_path}"
        )

    config = ConfigManager().load()

    from code_indexer.server.services.provider_index_service import ProviderIndexService

    service = ProviderIndexService(config=config)
    error = service.validate_provider(provider)
    if error:
        raise click.ClickException(error)

    console.print(
        f"Rebuilding {provider} index for {repo_path.name} (clear + rebuild)..."
    )

    try:
        result = subprocess.run(
            ["cidx", "index", "--provider", provider, "--clear"],
            cwd=str(repo_path),
            capture_output=False,
        )
    except FileNotFoundError:
        raise click.ClickException("cidx not found; install it or add it to PATH")

    if result.returncode == 0:
        console.print(f"[green]Successfully rebuilt {provider} index[/green]")
    else:
        raise click.ClickException(
            f"Failed to rebuild {provider} index (exit code {result.returncode})"
        )


@provider_index_group.command("remove")
@click.option("--provider", required=True, help="Embedding provider name")
@click.option("--repo", required=True, help="Repository path")
def remove(provider: str, repo: str):
    """Remove a provider's semantic index collection."""
    from pathlib import Path
    from .config import ConfigManager

    console = Console()
    repo_path = Path(repo).resolve()

    if not repo_path.is_dir():
        raise click.ClickException(
            f"Repository path not found or not a directory: {repo_path}"
        )

    config = ConfigManager().load()

    from code_indexer.server.services.provider_index_service import ProviderIndexService

    service = ProviderIndexService(config=config)

    error = service.validate_provider(provider)
    if error:
        raise click.ClickException(error)

    result = service.remove_provider_index(str(repo_path), provider)

    if result["removed"]:
        console.print(f"[green]{result['message']}[/green]")
    else:
        console.print(f"[yellow]{result['message']}[/yellow]")
