"""Shared helpers and constants for incremental indexing filter tests."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


# ---------------------------------------------------------------------------
# Extension constants
# ---------------------------------------------------------------------------

# Binary extensions from the production bug report
PRODUCTION_BINARY_EXTENSIONS = [
    "jar",
    "psd",
    "zip",
    "xcf",
    "exe",
    "png",
    "gz",
    "dll",
    "jpg",
    "pdf",
    "gif",
    "tif",
    "dylib",
    "bin",
    "blend",
    "blend1",
    "ttf",
    "ico",
    "sfx",
    "war",
    "so",
    "class",
    "bmp",
    "dic",
    "db",
    "ser",
    "keystore",
]

# Source extensions that MUST be accepted
VALID_SOURCE_EXTENSIONS = ["java", "kt", "kts", "py", "js", "ts", "cs", "go", "rs"]


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def make_binary_content() -> bytes:
    """Return a small chunk of actual binary content (has null bytes, non-UTF8)."""
    return b"\x00\x01\x02\x03\x04\x05\xff\xfe\xfd\xfc\xfb\xfa" * 8


def make_source_content(language: str = "java") -> str:
    """Return simple valid source code for a given language."""
    snippets = {
        "java": "public class Main {\n    public static void main(String[] args) {}\n}\n",
        "kt": 'fun main() { println("hello") }\n',
        "py": "def main():\n    pass\n",
        "js": "function main() {}\n",
        "ts": "export function main(): void {}\n",
    }
    return snippets.get(language, f"// {language} source\n")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def create_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# Test repo\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def get_current_commit(repo_path: Path) -> str:
    """Return the current HEAD commit hash."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_files(repo_path: Path, files: dict, message: str) -> str:
    """Write files to repo, stage, commit, and return new commit hash.

    Args:
        repo_path: Root of git repo
        files: {relative_path: content_bytes_or_str}
        message: Commit message

    Returns:
        New commit hash
    """
    for rel_path, content in files.items():
        full_path = repo_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full_path.write_bytes(content)
        else:
            full_path.write_text(content, encoding="utf-8")

    subprocess.run(
        ["git", "-C", str(repo_path), "add", "--all"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    return get_current_commit(repo_path)


# ---------------------------------------------------------------------------
# SmartIndexer factory
# ---------------------------------------------------------------------------


def build_smart_indexer(repo_path: Path, metadata_path: Path) -> SmartIndexer:
    """Build a SmartIndexer with real Config and mocked embedding/vector backends."""
    config = Config(codebase_dir=repo_path)

    mock_provider = Mock()
    mock_provider.get_provider_name.return_value = "test"
    mock_provider.get_current_model.return_value = "test-model"
    mock_provider.get_embedding.return_value = [0.1] * 1024

    mock_store = Mock()
    mock_store.resolve_collection_name.return_value = "test_collection"
    mock_store.collection_exists.return_value = True
    mock_store.count_points.return_value = 0
    mock_store.scroll_points.return_value = ([], None)
    mock_store.upsert_points_batched.return_value = True

    return SmartIndexer(config, mock_provider, mock_store, metadata_path)


def init_repo_with_indexer(tmp_path: Path) -> tuple:
    """Create a git repo and return (indexer, metadata_path).

    The .code-indexer directory is created inside tmp_path.
    """
    create_git_repo(tmp_path)
    metadata = tmp_path / ".code-indexer" / "metadata.json"
    metadata.parent.mkdir(exist_ok=True)
    metadata.write_text("{}")
    indexer = build_smart_indexer(tmp_path, metadata)
    return indexer, metadata
