"""Repository file browsing helper functions for CLI."""

from pathlib import Path
import click
from code_indexer.api_clients.repos_client import ReposAPIClient


def _lookup_alias(client: ReposAPIClient, user_alias: str) -> str:
    """Look up user_alias in the list of activated repositories.

    Calls the sync ReposAPIClient directly. The caller is responsible for
    constructing the client and for calling client.close() afterwards.

    Args:
        client: Constructed ReposAPIClient (must not be None).
        user_alias: Alias to look up (must be a non-empty string).

    Returns:
        user_alias if found.

    Raises:
        ValueError: If user_alias is empty/None, or if the alias is not found.
    """
    if not user_alias:
        raise ValueError("user_alias must be a non-empty string")
    repos = client.list_activated_repositories()
    for repo in repos:
        if repo.alias == user_alias:
            return user_alias
    raise ValueError(f"Repository '{user_alias}' not found")


def get_repo_id_from_alias(user_alias: str, project_root: Path) -> str:
    """Get repository ID from user alias.

    Args:
        user_alias: Alias to look up (non-empty).
        project_root: Project root path (must not be None).

    Raises:
        ValueError: If project_root is None or alias not found.
    """
    if project_root is None:
        raise ValueError("project_root must not be None")
    client = ReposAPIClient(server_url="", credentials={}, project_root=project_root)
    try:
        return _lookup_alias(client, user_alias)
    finally:
        client.close()


def get_repo_id_from_alias_sync(
    server_url: str, credentials: dict, user_alias: str, project_root: Path
) -> str:
    """Get repository ID from user alias (synchronous version for CLI commands).

    Args:
        server_url: Server URL (non-empty).
        credentials: Credentials dict.
        user_alias: Alias to look up (non-empty).
        project_root: Project root path (must not be None).

    Raises:
        ValueError: If server_url is empty, project_root is None, or alias not found.
    """
    if not server_url:
        raise ValueError("server_url must be a non-empty string")
    if project_root is None:
        raise ValueError("project_root must not be None")
    client = ReposAPIClient(
        server_url=server_url, credentials=credentials, project_root=project_root
    )
    try:
        return _lookup_alias(client, user_alias)
    finally:
        client.close()


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def display_file_tree(files: list, base_path: str = ""):
    """Display files in tree format with icons."""
    if not files:
        click.echo("(empty directory)")
        return

    # Separate directories and files
    # Note: API returns files without 'type' field, only directories have is_directory=True
    dirs = [f for f in files if f.get("type") == "directory" or f.get("is_directory")]
    regular_files = [
        f
        for f in files
        if f.get("type") == "file" or (not f.get("is_directory", False))
    ]

    # Sort alphabetically - handle both 'name' and 'path' fields
    dirs.sort(key=lambda x: x.get("path", x.get("name", "")))
    regular_files.sort(key=lambda x: x.get("path", x.get("name", "")))

    # Display header
    if base_path:
        click.echo(f"\nDirectory: {base_path}\n")

    # Display directories first
    for d in dirs:
        # Use 'path' if available (API response), fallback to 'name' (legacy)
        name = d.get("path", d.get("name", ""))
        click.echo(f"📁 {name}/")

    # Display files
    for f in regular_files:
        # Use 'path' if available (API response), fallback to 'name' (legacy)
        name = f.get("path", f.get("name", ""))
        # Use 'size_bytes' if available (API response), fallback to 'size' (legacy)
        size = f.get("size_bytes", f.get("size", 0))
        size_str = format_file_size(size)
        click.echo(f"📄 {name} ({size_str})")

    # Summary
    total = len(dirs) + len(regular_files)
    click.echo(f"\n{total} items ({len(dirs)} directories, {len(regular_files)} files)")


def display_with_line_numbers(content: str):
    """Display content with line numbers."""
    if not content:
        click.echo("(empty file)")
        return

    lines = content.split("\n")
    max_digits = len(str(len(lines)))

    for i, line in enumerate(lines, 1):
        line_num = str(i).rjust(max_digits)
        click.echo(f"{line_num} │ {line}")


def apply_syntax_highlighting(content: str, file_path: str) -> str:
    """Apply syntax highlighting based on file extension (optional)."""
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_for_filename
        from pygments.formatters import TerminalFormatter

        lexer = get_lexer_for_filename(file_path)
        result: str = str(highlight(content, lexer, TerminalFormatter()))
        return result
    except ImportError:
        # pygments not available, return plain content
        return content
    except Exception:
        # Any other error (unknown file type, etc.)
        return content
