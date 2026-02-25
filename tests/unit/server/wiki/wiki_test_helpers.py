"""Shared test helpers for wiki unit tests."""
import json
from pathlib import Path


def make_aliases_dir(golden_repos_dir: str, alias: str, target_path: str) -> None:
    """Write a {alias}-global.json alias file inside golden_repos_dir/aliases/."""
    aliases_dir = Path(golden_repos_dir) / "aliases"
    aliases_dir.mkdir(parents=True, exist_ok=True)
    alias_file = aliases_dir / f"{alias}-global.json"
    alias_data = {
        "target_path": target_path,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_refresh": "2026-01-01T00:00:00+00:00",
        "repo_name": alias,
    }
    alias_file.write_text(json.dumps(alias_data))
