"""Git CLI commands for code-indexer - Story #737.

Provides git workflow commands for remote mode (CLI-REST-MCP parity).
"""

import sys
import click
from typing import Optional
from rich.console import Console

from .disabled_commands import require_mode

console = Console()


def _load_remote_config_for_git() -> dict:
    """Load remote configuration for git commands.

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


def _handle_git_error(e: Exception, json_output: bool) -> None:
    """Handle git command errors with appropriate output format."""
    from .cli_utils import format_json_error
    from .cli_utils.remote_command_base import handle_remote_error

    if json_output:
        console.print(format_json_error(str(e), type(e).__name__))
    else:
        console.print(f"[red]Error: {handle_remote_error(e, verbose=False)}[/red]")
    sys.exit(1)


@click.group("git")
@require_mode("remote")
def git_group():
    """Git history search and branch operations (remote mode only).

    Provides access to git history, commits, branches, and blame information
    for repositories indexed on the CIDX server.
    """
    pass


@git_group.command("status")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_status(repository: str, json_output: bool):
    """Show working tree status."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.status(repository))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[bold]Repository:[/bold] {repository}")
            staged = result.get("staged", [])
            unstaged = result.get("unstaged", [])
            untracked = result.get("untracked", [])

            if staged:
                console.print("\n[green]Staged changes:[/green]")
                for f in staged:
                    console.print(f"  {f}")
            if unstaged:
                console.print("\n[yellow]Unstaged changes:[/yellow]")
                for f in unstaged:
                    console.print(f"  {f}")
            if untracked:
                console.print("\n[dim]Untracked files:[/dim]")
                for f in untracked:
                    console.print(f"  {f}")
            if not staged and not unstaged and not untracked:
                console.print("[green]Working tree clean[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("commit")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--message", "-m", required=True, help="Commit message")
@click.option("--author-name", help="Author name (optional)")
@click.option("--author-email", help="Author email (optional)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_commit(
    repository: str,
    message: str,
    author_name: Optional[str],
    author_email: Optional[str],
    json_output: bool,
):
    """Create a commit with staged changes."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(
            client.commit(repository, message, author_name, author_email)
        )

        if json_output:
            console.print(format_json_success(result))
        else:
            commit_hash = result.get("hash", result.get("commit_hash", "unknown"))
            console.print(f"[green]Commit created:[/green] {commit_hash[:8]}")
            console.print(f"[dim]Message:[/dim] {message}")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("reset")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option(
    "--mode",
    type=click.Choice(["soft", "mixed", "hard"]),
    default="mixed",
    help="Reset mode (soft/mixed/hard)",
)
@click.option("--commit", "commit_ref", help="Target commit (default: HEAD)")
@click.option(
    "--confirm",
    is_flag=True,
    help="Confirm destructive operation (required for --mode hard)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_reset(
    repository: str,
    mode: str,
    commit_ref: Optional[str],
    confirm: bool,
    json_output: bool,
):
    """Reset working tree to a commit.

    WARNING: --mode hard is destructive and requires --confirm flag.
    """
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success, format_json_error

    # Require confirmation for hard reset
    if mode == "hard" and not confirm:
        msg = "Hard reset is destructive. Use --confirm to proceed."
        if json_output:
            console.print(format_json_error(msg, "ConfirmationRequired"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.reset(repository, mode, commit_ref))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Reset complete[/green] (mode: {mode})")
            if commit_ref:
                console.print(f"[dim]Target: {commit_ref}[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("diff")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--path", "-p", help="Limit diff to specific path")
@click.option("--staged", is_flag=True, help="Show staged changes only")
@click.option("--commit", "-c", help="Show diff for specific commit")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_diff(
    repository: str,
    path: Optional[str],
    staged: bool,
    commit: Optional[str],
    json_output: bool,
):
    """Show changes between commits, commit and working tree, etc."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.diff(repository, path, staged, commit))

        if json_output:
            console.print(format_json_success(result))
        else:
            diff_text = result.get("diff", "")
            if diff_text:
                console.print(diff_text)
            else:
                console.print("[dim]No changes[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("log")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--limit", "-n", default=20, help="Maximum number of commits")
@click.option("--author", "-a", help="Filter by author")
@click.option("--path", "-p", help="Filter commits affecting path")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_log(
    repository: str,
    limit: int,
    author: Optional[str],
    path: Optional[str],
    json_output: bool,
):
    """Show commit history."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.log(repository, limit, author, path))

        if json_output:
            console.print(format_json_success(result))
        else:
            commits = result.get("commits", [])
            if commits:
                for c in commits:
                    hash_short = c.get("hash", "")[:8]
                    msg = c.get("message", "").split("\n")[0]
                    author_name = c.get("author", "")
                    console.print(
                        f"[yellow]{hash_short}[/yellow] {msg} [dim]({author_name})[/dim]"
                    )
            else:
                console.print("[dim]No commits found[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("show")
@click.argument("commit_hash")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--include-diff", is_flag=True, help="Include diff in output")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_show(
    commit_hash: str,
    repository: str,
    include_diff: bool,
    json_output: bool,
):
    """Show details of a specific commit."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.show_commit(repository, commit_hash, include_diff))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[yellow]Commit:[/yellow] {result.get('hash', commit_hash)}")
            console.print(f"[dim]Author:[/dim] {result.get('author', 'unknown')}")
            console.print(f"[dim]Date:[/dim] {result.get('date', 'unknown')}")
            console.print(f"\n{result.get('message', '')}")

            if include_diff and result.get("diff"):
                console.print("\n[bold]Diff:[/bold]")
                console.print(result.get("diff", ""))

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("stage")
@click.argument("files", nargs=-1, required=True)
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_stage(files: tuple, repository: str, json_output: bool):
    """Stage files for commit."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.stage(repository, list(files)))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Staged {len(files)} file(s)[/green]")
            for f in files:
                console.print(f"  {f}")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("unstage")
@click.argument("files", nargs=-1, required=True)
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_unstage(files: tuple, repository: str, json_output: bool):
    """Unstage files from the index."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.unstage(repository, list(files)))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Unstaged {len(files)} file(s)[/green]")
            for f in files:
                console.print(f"  {f}")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("push")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--remote", default="origin", help="Remote name (default: origin)")
@click.option("--branch", "-b", help="Branch to push (default: current)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_push(repository: str, remote: str, branch: Optional[str], json_output: bool):
    """Push commits to remote repository."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.push(repository, remote, branch))

        if json_output:
            console.print(format_json_success(result))
        else:
            target = f"{remote}/{branch}" if branch else remote
            console.print(f"[green]Pushed to {target}[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("pull")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--remote", default="origin", help="Remote name (default: origin)")
@click.option("--branch", "-b", help="Branch to pull (default: current)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_pull(repository: str, remote: str, branch: Optional[str], json_output: bool):
    """Pull changes from remote repository."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.pull(repository, remote, branch))

        if json_output:
            console.print(format_json_success(result))
        else:
            target = f"{remote}/{branch}" if branch else remote
            console.print(f"[green]Pulled from {target}[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("fetch")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--remote", default="origin", help="Remote name (default: origin)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_fetch(repository: str, remote: str, json_output: bool):
    """Fetch changes from remote without merging."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.fetch(repository, remote))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Fetched from {remote}[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("clean")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option(
    "--confirm", is_flag=True, help="Confirm destructive operation (required)"
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_clean(repository: str, confirm: bool, json_output: bool):
    """Remove untracked files from working tree.

    WARNING: This is destructive and requires --confirm flag.
    """
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success, format_json_error

    if not confirm:
        msg = "Clean is destructive. Use --confirm to proceed."
        if json_output:
            console.print(format_json_error(msg, "ConfirmationRequired"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.clean(repository))

        if json_output:
            console.print(format_json_success(result))
        else:
            removed = result.get("removed", [])
            if removed:
                console.print(f"[green]Removed {len(removed)} file(s)[/green]")
                for f in removed:
                    console.print(f"  {f}")
            else:
                console.print("[dim]No untracked files to remove[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("merge-abort")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_merge_abort(repository: str, json_output: bool):
    """Abort a merge in progress."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.merge_abort(repository))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print("[green]Merge aborted[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("checkout-file")
@click.argument("file")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_checkout_file(file: str, repository: str, json_output: bool):
    """Checkout a file from HEAD, discarding local changes."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.checkout_file(repository, file))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Restored {file}[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("branches")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_branches(repository: str, json_output: bool):
    """List all branches."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.branches(repository))

        if json_output:
            console.print(format_json_success(result))
        else:
            current = result.get("current", "")
            branches = result.get("branches", [])
            for branch in branches:
                if branch == current:
                    console.print(f"[green]* {branch}[/green]")
                else:
                    console.print(f"  {branch}")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("branch-create")
@click.argument("name")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--start-point", help="Starting ref (default: HEAD)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_branch_create(
    name: str,
    repository: str,
    start_point: Optional[str],
    json_output: bool,
):
    """Create a new branch."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.branch_create(repository, name, start_point))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Created branch '{name}'[/green]")
            if start_point:
                console.print(f"[dim]From: {start_point}[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("branch-switch")
@click.argument("name")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_branch_switch(name: str, repository: str, json_output: bool):
    """Switch to a branch."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.branch_switch(repository, name))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Switched to branch '{name}'[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("branch-delete")
@click.argument("name")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option(
    "--confirm", is_flag=True, help="Confirm destructive operation (required)"
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_branch_delete(name: str, repository: str, confirm: bool, json_output: bool):
    """Delete a branch.

    WARNING: This is destructive and requires --confirm flag.
    """
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success, format_json_error

    if not confirm:
        msg = "Branch deletion is destructive. Use --confirm to proceed."
        if json_output:
            console.print(format_json_error(msg, "ConfirmationRequired"))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.branch_delete(repository, name))

        if json_output:
            console.print(format_json_success(result))
        else:
            console.print(f"[green]Deleted branch '{name}'[/green]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("blame")
@click.argument("file")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_blame(file: str, repository: str, json_output: bool):
    """Show what revision and author last modified each line of a file."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.blame(repository, file))

        if json_output:
            console.print(format_json_success(result))
        else:
            lines = result.get("lines", [])
            for line_info in lines:
                line_num = line_info.get("line", "")
                commit_short = line_info.get("commit", "")[:8]
                author = line_info.get("author", "")
                content = line_info.get("content", "")
                console.print(
                    f"[yellow]{commit_short}[/yellow] ({author}) {line_num}: {content}"
                )

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("file-history")
@click.argument("file")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--limit", "-n", default=20, help="Maximum number of commits")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_file_history(file: str, repository: str, limit: int, json_output: bool):
    """Show commit history for a specific file."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.file_history(repository, file, limit))

        if json_output:
            console.print(format_json_success(result))
        else:
            commits = result.get("commits", [])
            if commits:
                console.print(f"[bold]History for {file}:[/bold]\n")
                for c in commits:
                    hash_short = c.get("hash", "")[:8]
                    msg = c.get("message", "").split("\n")[0]
                    author = c.get("author", "")
                    console.print(
                        f"[yellow]{hash_short}[/yellow] {msg} [dim]({author})[/dim]"
                    )
            else:
                console.print("[dim]No commits found[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("search-commits")
@click.argument("query")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--limit", "-n", default=20, help="Maximum number of results")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_search_commits(query: str, repository: str, limit: int, json_output: bool):
    """Search commits by message content."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.search_commits(repository, query, limit))

        if json_output:
            console.print(format_json_success(result))
        else:
            commits = result.get("commits", [])
            if commits:
                console.print(f"[bold]Search results for '{query}':[/bold]\n")
                for c in commits:
                    hash_short = c.get("hash", "")[:8]
                    msg = c.get("message", "").split("\n")[0]
                    author = c.get("author", "")
                    console.print(
                        f"[yellow]{hash_short}[/yellow] {msg} [dim]({author})[/dim]"
                    )
            else:
                console.print("[dim]No matching commits found[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("search-diffs")
@click.argument("pattern")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--limit", "-n", default=20, help="Maximum number of results")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_search_diffs(pattern: str, repository: str, limit: int, json_output: bool):
    """Search for a pattern in diff content."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.search_diffs(repository, pattern, limit))

        if json_output:
            console.print(format_json_success(result))
        else:
            matches = result.get("matches", [])
            if matches:
                console.print(f"[bold]Search results for pattern '{pattern}':[/bold]\n")
                for m in matches:
                    commit_short = m.get("commit", "")[:8]
                    file_path = m.get("file", "")
                    line = m.get("line", "")
                    console.print(
                        f"[yellow]{commit_short}[/yellow] {file_path}: {line}"
                    )
            else:
                console.print("[dim]No matches found[/dim]")

    except Exception as e:
        _handle_git_error(e, json_output)


@git_group.command("cat")
@click.argument("file")
@click.option("--repository", "-r", required=True, help="Repository alias")
@click.option("--revision", help="Git revision (default: HEAD)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def git_cat(file: str, repository: str, revision: Optional[str], json_output: bool):
    """Show file content at a specific revision."""
    import asyncio
    from .api_clients.git_client import GitAPIClient
    from .cli_utils import format_json_success

    try:
        config = _load_remote_config_for_git()
        client = GitAPIClient(config["server_url"], config["credentials"])
        result = asyncio.run(client.cat_file(repository, file, revision))

        if json_output:
            console.print(format_json_success(result))
        else:
            content = result.get("content", "")
            console.print(content)

    except Exception as e:
        _handle_git_error(e, json_output)
