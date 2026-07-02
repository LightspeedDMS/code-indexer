"""Prompt templates for golden repository description generation and lifecycle detection."""

import re
from pathlib import Path

_VALID_NAME = re.compile(r"^[a-zA-Z0-9_]+$")
_PROMPTS_DIR = Path(__file__).parent


def get_prompt(name: str) -> str:
    """
    Load a prompt template from its .md file.

    Args:
        name: Prompt name without .md extension (e.g., "repo_description_create").
              Must be a non-empty string containing only letters, digits, and underscores.

    Returns:
        str: Complete prompt text, verbatim file content (no frontmatter stripping)

    Raises:
        ValueError: If name is not a string, is empty, or contains invalid characters
        FileNotFoundError: If no .md file exists for the given name
    """
    if not isinstance(name, str):
        raise ValueError(f"Prompt name must be a str, got {type(name).__name__!r}")
    if not _VALID_NAME.match(name):
        raise ValueError(
            f"Invalid prompt name {name!r}: only letters, digits, and underscores allowed"
        )
    prompt_path = _PROMPTS_DIR / f"{name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")
