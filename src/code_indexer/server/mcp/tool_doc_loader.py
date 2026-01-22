"""
Tool Documentation Loader - Loads MCP tool documentation from external markdown files.

Story #14: Externalize MCP Tool Documentation to Markdown Files

This module provides:
- ToolDocLoader: Loads and caches tool docs from .md files
- ToolDoc: Dataclass for parsed tool documentation
- ToolDocNotFoundError: Raised when a tool doc is missing
- FrontmatterValidationError: Raised for invalid frontmatter
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml


class ToolDocNotFoundError(Exception):
    """Raised when a tool documentation file is not found."""

    pass


class FrontmatterValidationError(Exception):
    """Raised when frontmatter validation fails."""

    pass


@dataclass
class ToolDoc:
    """Parsed tool documentation from a markdown file."""

    name: str
    category: str
    required_permission: str
    tl_dr: str
    description: str
    quick_reference: bool = False
    parameters: Optional[Dict[str, str]] = None


class ToolDocLoader:
    """Loads and caches tool documentation from markdown files."""

    VALID_CATEGORIES = {
        "search",
        "git",
        "scip",
        "files",
        "admin",
        "repos",
        "ssh",
        "guides",
        "cicd",
    }

    def __init__(self, docs_dir: Path):
        """Initialize loader with path to tool_docs directory."""
        self.docs_dir = docs_dir
        self._cache: Dict[str, ToolDoc] = {}
        self._loaded = False

    def load_all_docs(self) -> Dict[str, ToolDoc]:
        """Load all .md files from tool_docs directory and cache them."""
        if self._loaded:
            return self._cache

        if not self.docs_dir.exists():
            raise FileNotFoundError(f"Tool docs directory not found: {self.docs_dir}")

        for category_dir in self.docs_dir.iterdir():
            if not category_dir.is_dir():
                continue
            for md_file in category_dir.glob("*.md"):
                tool_doc = self._parse_md_file(md_file)
                self._cache[tool_doc.name] = tool_doc

        self._loaded = True
        return self._cache

    def _parse_md_file(self, md_file: Path) -> ToolDoc:
        """Parse a markdown file with YAML frontmatter."""
        content = md_file.read_text(encoding="utf-8")

        if not content.startswith("---"):
            raise FrontmatterValidationError(f"Missing frontmatter in {md_file}")

        parts = content.split("---", 2)
        if len(parts) < 3:
            raise FrontmatterValidationError(f"Invalid frontmatter format in {md_file}")

        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            raise FrontmatterValidationError(f"Invalid YAML in {md_file}: {e}")

        if frontmatter is None:
            raise FrontmatterValidationError(f"Empty frontmatter in {md_file}")

        required_fields = ["name", "category", "required_permission", "tl_dr"]
        for field in required_fields:
            if field not in frontmatter:
                raise FrontmatterValidationError(
                    f"Missing required field '{field}' in {md_file}"
                )

        body = parts[2].lstrip("\n")

        return ToolDoc(
            name=frontmatter["name"],
            category=frontmatter["category"],
            required_permission=frontmatter["required_permission"],
            tl_dr=frontmatter["tl_dr"],
            description=body,
            quick_reference=frontmatter.get("quick_reference", False),
            parameters=frontmatter.get("parameters"),
        )

    def get_description(self, tool_name: str) -> str:
        """Get the description for a tool. Raises ToolDocNotFoundError if missing."""
        if tool_name not in self._cache:
            raise ToolDocNotFoundError(f"No documentation found for tool: {tool_name}")
        return self._cache[tool_name].description

    def get_permission(self, tool_name: str) -> str:
        """Get the required permission for a tool."""
        if tool_name not in self._cache:
            raise ToolDocNotFoundError(f"No documentation found for tool: {tool_name}")
        return self._cache[tool_name].required_permission

    def get_param_description(self, tool_name: str, param_name: str) -> Optional[str]:
        """Get description for a specific parameter. Returns None if not found."""
        if tool_name not in self._cache:
            raise ToolDocNotFoundError(f"No documentation found for tool: {tool_name}")

        tool_doc = self._cache[tool_name]
        if tool_doc.parameters is None:
            return None
        return tool_doc.parameters.get(param_name)

    def validate_against_registry(self, tool_registry: Dict[str, Any]) -> List[str]:
        """Validate that all tools in registry have documentation. Returns missing tools."""
        missing = []
        for tool_name in tool_registry:
            if tool_name not in self._cache:
                missing.append(tool_name)
        return missing

    def generate_quick_reference(self) -> str:
        """Generate quick reference from tools with quick_reference: true."""
        # Group tools by category
        by_category: Dict[str, List[ToolDoc]] = {}
        for tool_doc in self._cache.values():
            if tool_doc.quick_reference:
                if tool_doc.category not in by_category:
                    by_category[tool_doc.category] = []
                by_category[tool_doc.category].append(tool_doc)

        if not by_category:
            return "No tools marked for quick reference."

        # Build output grouped by category
        lines = []
        for category in sorted(by_category.keys()):
            lines.append(f"\n{category.upper()}:")
            for tool in sorted(by_category[category], key=lambda t: t.name):
                lines.append(f"  {tool.name} - {tool.tl_dr}")

        return "\n".join(lines)
