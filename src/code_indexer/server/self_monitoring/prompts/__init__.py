"""Prompt templates for self-monitoring log analysis."""

from pathlib import Path


def get_default_prompt() -> str:
    """
    Load default analysis prompt from markdown file.

    Returns:
        str: Complete prompt template with {last_scan_log_id} and {dedup_context} placeholders
    """
    prompt_path = Path(__file__).parent / "default_analysis_prompt.md"
    return prompt_path.read_text(encoding="utf-8")
