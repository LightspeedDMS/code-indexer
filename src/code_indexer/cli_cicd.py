"""CI/CD CLI commands for code-indexer - Story #746.

Provides CI/CD monitoring commands for remote mode (CLI-REST parity).
"""

import sys
import click
from typing import Optional
from rich.console import Console
from rich.table import Table

from .disabled_commands import require_mode

console = Console()


def _load_remote_config_for_cicd() -> dict:
    """Load remote configuration for CI/CD commands."""
    from .cli_utils.remote_command_base import _load_remote_config

    try:
        return _load_remote_config()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except ValueError as e:
        raise click.ClickException(str(e))


def _handle_cicd_error(e: Exception, json_output: bool) -> None:
    """Handle CI/CD command errors with appropriate output format."""
    from .cli_utils import format_json_error
    from .cli_utils.remote_command_base import handle_remote_error

    if json_output:
        console.print(format_json_error(str(e), type(e).__name__))
    else:
        console.print(f"[red]Error: {handle_remote_error(e, verbose=False)}[/red]")
    sys.exit(1)


def _format_status_color(status: str) -> str:
    """Get color for CI/CD status display."""
    status_lower = status.lower()
    if status_lower in ("success", "completed", "passed"):
        return "green"
    elif status_lower in ("failure", "failed", "error"):
        return "red"
    elif status_lower in ("running", "pending", "in_progress", "queued"):
        return "yellow"
    elif status_lower in ("cancelled", "canceled", "skipped"):
        return "dim"
    return "white"


@click.group("cicd")
@require_mode("remote")
def cicd_group():
    """CI/CD pipeline monitoring (remote mode only).

    Monitor GitHub Actions and GitLab CI pipelines from the CLI.
    """
    pass


# GitHub Actions command group
@cicd_group.group("github")
def github_group():
    """GitHub Actions workflow monitoring.

    Commands to list, inspect, and control GitHub Actions workflow runs.
    """
    pass


@github_group.command("list")
@click.argument("repository")
@click.option("--status", "-s", help="Filter by run status")
@click.option("--branch", "-b", help="Filter by branch name")
@click.option("--limit", "-n", default=10, type=int, help="Maximum runs to return")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_list(
    repository: str,
    status: Optional[str],
    branch: Optional[str],
    limit: int,
    json_output: bool,
):
    """List GitHub Actions workflow runs for OWNER/REPO."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    # Parse owner/repo format
    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(
            client.github_list_runs(owner, repo, status, branch, limit)
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            runs = result.get("runs", [])
            if not runs:
                console.print("[dim]No workflow runs found[/dim]")
                return

            table = Table(title=f"GitHub Actions Runs - {repository}")
            table.add_column("ID", style="cyan")
            table.add_column("Status", justify="center")
            table.add_column("Branch", style="blue")
            table.add_column("Started", style="dim")
            table.add_column("Duration", style="dim")

            for run in runs:
                run_id = str(run.get("id", ""))
                run_status = run.get("status", "unknown")
                run_branch = run.get("head_branch", run.get("branch", ""))
                started = (
                    run.get("created_at", run.get("started_at", ""))[:16]
                    if run.get("created_at") or run.get("started_at")
                    else ""
                )
                duration = run.get("duration", "")

                status_color = _format_status_color(run_status)
                table.add_row(
                    run_id,
                    f"[{status_color}]{run_status}[/{status_color}]",
                    run_branch,
                    started,
                    str(duration),
                )

            console.print(table)

    except Exception as e:
        _handle_cicd_error(e, json_output)


@github_group.command("show")
@click.argument("repository")
@click.argument("run_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_show(repository: str, run_id: int, json_output: bool):
    """Show details of a GitHub Actions workflow run."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.github_get_run(owner, repo, run_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            run_status = result.get("status", "unknown")
            status_color = _format_status_color(run_status)

            console.print(
                f"[bold]Run #{run_id}[/bold] - [{status_color}]{run_status}[/{status_color}]"
            )
            console.print(f"[dim]Repository:[/dim] {repository}")
            console.print(
                f"[dim]Branch:[/dim] {result.get('head_branch', result.get('branch', 'N/A'))}"
            )
            console.print(
                f"[dim]Workflow:[/dim] {result.get('name', result.get('workflow_name', 'N/A'))}"
            )
            console.print(
                f"[dim]Started:[/dim] {result.get('created_at', result.get('started_at', 'N/A'))}"
            )
            console.print(f"[dim]Conclusion:[/dim] {result.get('conclusion', 'N/A')}")

            jobs = result.get("jobs", [])
            if jobs:
                console.print("\n[bold]Jobs:[/bold]")
                for job in jobs:
                    job_status = job.get("status", "unknown")
                    job_color = _format_status_color(job_status)
                    console.print(
                        f"  [{job_color}]{job_status}[/{job_color}] {job.get('name', 'unnamed')}"
                    )

    except Exception as e:
        _handle_cicd_error(e, json_output)


@github_group.command("logs")
@click.argument("repository")
@click.argument("run_id", type=int)
@click.option("--query", "-q", help="Search pattern for logs")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_logs(repository: str, run_id: int, query: Optional[str], json_output: bool):
    """Search logs for a GitHub Actions workflow run."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.github_search_logs(owner, repo, run_id, query))

        if json_output:
            console.print(format_json_success(result))
        else:
            matches = result.get("matches", result.get("logs", []))
            if isinstance(matches, str):
                console.print(matches)
            elif matches:
                for match in matches:
                    if isinstance(match, dict):
                        job = match.get("job", "")
                        step = match.get("step", "")
                        line = match.get("line", match.get("content", ""))
                        console.print(f"[dim]{job}/{step}:[/dim] {line}")
                    else:
                        console.print(str(match))
            else:
                console.print("[dim]No log matches found[/dim]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


@github_group.command("job-logs")
@click.argument("repository")
@click.argument("job_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_job_logs(repository: str, job_id: int, json_output: bool):
    """Get complete logs for a specific GitHub Actions job."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.github_get_job_logs(owner, repo, job_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            logs = result.get("logs", result.get("content", ""))
            if logs:
                console.print(logs)
            else:
                console.print("[dim]No logs available[/dim]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


@github_group.command("retry")
@click.argument("repository")
@click.argument("run_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_retry(repository: str, run_id: int, json_output: bool):
    """Retry a failed GitHub Actions workflow run."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.github_retry_run(owner, repo, run_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Run #{run_id} has been queued for retry[/green]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


@github_group.command("cancel")
@click.argument("repository")
@click.argument("run_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def github_cancel(repository: str, run_id: int, json_output: bool):
    """Cancel a running or queued GitHub Actions workflow run."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    if "/" not in repository:
        if json_output:
            from .cli_utils import format_json_error

            console.print(
                format_json_error(
                    "Repository must be in OWNER/REPO format", "ValueError"
                )
            )
        else:
            console.print("[red]Error: Repository must be in OWNER/REPO format[/red]")
        sys.exit(1)

    owner, repo = repository.split("/", 1)

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.github_cancel_run(owner, repo, run_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[yellow]Run #{run_id} has been cancelled[/yellow]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


# GitLab CI command group
@cicd_group.group("gitlab")
def gitlab_group():
    """GitLab CI pipeline monitoring.

    Commands to list, inspect, and control GitLab CI pipelines.
    """
    pass


@gitlab_group.command("list")
@click.argument("project_id")
@click.option("--status", "-s", help="Filter by pipeline status")
@click.option("--ref", "-r", help="Filter by branch/tag name")
@click.option("--limit", "-n", default=10, type=int, help="Maximum pipelines to return")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_list(
    project_id: str,
    status: Optional[str],
    ref: Optional[str],
    limit: int,
    json_output: bool,
):
    """List GitLab CI pipelines for PROJECT_ID."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(
            client.gitlab_list_pipelines(project_id, status, ref, limit)
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            pipelines = result.get("pipelines", [])
            if not pipelines:
                console.print("[dim]No pipelines found[/dim]")
                return

            table = Table(title=f"GitLab CI Pipelines - {project_id}")
            table.add_column("ID", style="cyan")
            table.add_column("Status", justify="center")
            table.add_column("Ref", style="blue")
            table.add_column("Started", style="dim")
            table.add_column("Duration", style="dim")

            for pipeline in pipelines:
                pid = str(pipeline.get("id", ""))
                pstatus = pipeline.get("status", "unknown")
                pref = pipeline.get("ref", "")
                started = (
                    pipeline.get("created_at", pipeline.get("started_at", ""))[:16]
                    if pipeline.get("created_at") or pipeline.get("started_at")
                    else ""
                )
                duration = pipeline.get("duration", "")

                status_color = _format_status_color(pstatus)
                table.add_row(
                    pid,
                    f"[{status_color}]{pstatus}[/{status_color}]",
                    pref,
                    started,
                    str(duration),
                )

            console.print(table)

    except Exception as e:
        _handle_cicd_error(e, json_output)


@gitlab_group.command("show")
@click.argument("project_id")
@click.argument("pipeline_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_show(project_id: str, pipeline_id: int, json_output: bool):
    """Show details of a GitLab CI pipeline."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.gitlab_get_pipeline(project_id, pipeline_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            pstatus = result.get("status", "unknown")
            status_color = _format_status_color(pstatus)

            console.print(
                f"[bold]Pipeline #{pipeline_id}[/bold] - [{status_color}]{pstatus}[/{status_color}]"
            )
            console.print(f"[dim]Project:[/dim] {project_id}")
            console.print(f"[dim]Ref:[/dim] {result.get('ref', 'N/A')}")
            console.print(
                f"[dim]Started:[/dim] {result.get('created_at', result.get('started_at', 'N/A'))}"
            )
            console.print(f"[dim]Duration:[/dim] {result.get('duration', 'N/A')}s")

            jobs = result.get("jobs", [])
            if jobs:
                console.print("\n[bold]Jobs:[/bold]")
                for job in jobs:
                    jstatus = job.get("status", "unknown")
                    jcolor = _format_status_color(jstatus)
                    console.print(
                        f"  [{jcolor}]{jstatus}[/{jcolor}] {job.get('name', 'unnamed')}"
                    )

    except Exception as e:
        _handle_cicd_error(e, json_output)


@gitlab_group.command("logs")
@click.argument("project_id")
@click.argument("pipeline_id", type=int)
@click.option("--query", "-q", help="Search pattern for logs")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_logs(
    project_id: str, pipeline_id: int, query: Optional[str], json_output: bool
):
    """Search logs for a GitLab CI pipeline."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.gitlab_search_logs(project_id, pipeline_id, query))

        if json_output:
            console.print(format_json_success(result))
        else:
            matches = result.get("matches", result.get("logs", []))
            if isinstance(matches, str):
                console.print(matches)
            elif matches:
                for match in matches:
                    if isinstance(match, dict):
                        job = match.get("job", "")
                        line = match.get("line", match.get("content", ""))
                        console.print(f"[dim]{job}:[/dim] {line}")
                    else:
                        console.print(str(match))
            else:
                console.print("[dim]No log matches found[/dim]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


@gitlab_group.command("job-logs")
@click.argument("project_id")
@click.argument("job_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_job_logs(project_id: str, job_id: int, json_output: bool):
    """Get complete logs for a specific GitLab CI job."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.gitlab_get_job_logs(project_id, job_id))

        if json_output:
            console.print(format_json_success(result))
        else:
            logs = result.get("logs", result.get("content", ""))
            if logs:
                console.print(logs)
            else:
                console.print("[dim]No logs available[/dim]")

    except Exception as e:
        _handle_cicd_error(e, json_output)


@gitlab_group.command("retry")
@click.argument("project_id")
@click.argument("pipeline_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_retry(project_id: str, pipeline_id: int, json_output: bool):
    """Retry a failed GitLab CI pipeline."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        asyncio.run(client.gitlab_retry_pipeline(project_id, pipeline_id))

        if json_output:
            console.print(
                format_json_success({"success": True, "pipeline_id": pipeline_id})
            )
        else:
            console.print(
                f"[green]Pipeline #{pipeline_id} has been queued for retry[/green]"
            )

    except Exception as e:
        _handle_cicd_error(e, json_output)


@gitlab_group.command("cancel")
@click.argument("project_id")
@click.argument("pipeline_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def gitlab_cancel(project_id: str, pipeline_id: int, json_output: bool):
    """Cancel a running or pending GitLab CI pipeline."""
    import asyncio
    from .api_clients.cicd_client import CICDAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_cicd()
        client = CICDAPIClient(config["server_url"], config["credentials"])
        asyncio.run(client.gitlab_cancel_pipeline(project_id, pipeline_id))

        if json_output:
            console.print(
                format_json_success({"success": True, "pipeline_id": pipeline_id})
            )
        else:
            console.print(
                f"[yellow]Pipeline #{pipeline_id} has been cancelled[/yellow]"
            )

    except Exception as e:
        _handle_cicd_error(e, json_output)
