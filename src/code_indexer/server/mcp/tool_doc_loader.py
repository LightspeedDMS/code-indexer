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
class CategoryMeta:
    """Metadata for a tool category."""

    name: str
    description: str


@dataclass
class ToolDoc:
    """Parsed tool documentation from a markdown file."""

    name: str
    category: str
    required_permission: str
    tl_dr: str
    description: str
    parameters: Optional[Dict[str, str]] = None
    inputSchema: Optional[Dict[str, Any]] = None
    outputSchema: Optional[Dict[str, Any]] = None
    requires_config: Optional[str] = None  # Story #185: Conditional tool visibility


# Module-level singleton for ToolDocLoader to avoid per-request disk I/O
# (Story #222 code review Finding 1: ~650ms latency regression from per-call instantiation)
_singleton_loader: "Optional[ToolDocLoader]" = None


def _get_tool_doc_loader() -> "ToolDocLoader":
    """Return the module-level ToolDocLoader singleton, creating it on first call.

    Tool docs are static files that only change on deployment, not at runtime.
    Caching avoids parsing 127 YAML files from disk on every quick_reference() call.
    """
    global _singleton_loader
    if _singleton_loader is None:
        docs_dir = Path(__file__).parent / "tool_docs"
        _singleton_loader = ToolDocLoader(docs_dir)
        _singleton_loader.load_all_docs()
    return _singleton_loader


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
        "tracing",
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

    def _load_category_meta(self, category_dir: Path) -> Optional[CategoryMeta]:
        """Load _category.yaml from a category directory.

        Args:
            category_dir: Path to the category directory

        Returns:
            CategoryMeta if _category.yaml exists, None otherwise.
            Returns fallback CategoryMeta with directory name if YAML parsing fails.
        """
        meta_file = category_dir / "_category.yaml"
        if not meta_file.exists():
            return None

        try:
            content = meta_file.read_text(encoding="utf-8")
        except OSError:
            # Treat read errors as missing file
            return None

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError:
            # On malformed YAML, return fallback with directory name
            return CategoryMeta(
                name=category_dir.name,
                description="",
            )

        if not isinstance(data, dict):
            data = {}

        return CategoryMeta(
            name=data.get("name", category_dir.name),
            description=data.get("description", ""),
        )

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
            parameters=frontmatter.get("parameters"),
            inputSchema=frontmatter.get("inputSchema"),
            outputSchema=frontmatter.get("outputSchema"),
            requires_config=frontmatter.get("requires_config"),  # Story #185
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

    def build_tool_registry(self) -> Dict[str, Dict[str, Any]]:
        """Build TOOL_REGISTRY from loaded tool docs.

        Returns a dictionary matching the TOOL_REGISTRY format used in tools.py.
        Only includes tools that have an inputSchema defined (excludes guides).

        Returns:
            Dict mapping tool names to their definitions with name, description,
            inputSchema, and required_permission.
        """
        if not self._loaded:
            self.load_all_docs()

        registry: Dict[str, Dict[str, Any]] = {}
        for name, doc in self._cache.items():
            if doc.inputSchema is None:
                continue  # Skip tools without inputSchema (like guides)
            tool_def = {
                "name": name,
                "description": doc.description,
                "inputSchema": doc.inputSchema,
                "required_permission": doc.required_permission,
            }
            # Include outputSchema if present (for documentation purposes)
            if doc.outputSchema is not None:
                tool_def["outputSchema"] = doc.outputSchema
            # Story #185: Include requires_config for conditional visibility
            if doc.requires_config is not None:
                tool_def["requires_config"] = doc.requires_config
            registry[name] = tool_def
        return registry

    def get_tools_by_category(self) -> Dict[str, List[Dict[str, str]]]:
        """Get all tools grouped by category with tl_dr descriptions.

        Returns tools organized by their category, with each tool represented
        as a dict containing 'name' and 'tl_dr' fields. Only includes tools
        that have an inputSchema (excludes documentation-only entries).

        Returns:
            Dict mapping category names to lists of tool info dicts.
        """
        if not self._loaded:
            self.load_all_docs()

        by_category: Dict[str, List[Dict[str, str]]] = {}
        for doc in self._cache.values():
            if doc.inputSchema is None:
                continue  # Skip non-tool docs (guides without schema)
            if doc.category not in by_category:
                by_category[doc.category] = []
            by_category[doc.category].append({"name": doc.name, "tl_dr": doc.tl_dr})
        return by_category

    def get_category_overview(self) -> List[Dict[str, Any]]:
        """Get overview of all categories with descriptions and key tools.

        Returns a list of category info dicts sorted alphabetically by name.
        Only includes categories that have at least one tool with inputSchema.

        Returns:
            List of dicts, each containing:
            - name: Category name
            - description: Category description from _category.yaml (or empty string)
            - key_tools: Tools with quick_reference: true first, otherwise first 3 alphabetically
            - tool_count: Total number of tools in category
        """
        if not self._loaded:
            self.load_all_docs()

        # Group tools by category (only tools with inputSchema)
        tools_by_category: Dict[str, List[ToolDoc]] = {}
        for doc in self._cache.values():
            if doc.inputSchema is None:
                continue  # Skip guides without inputSchema
            if doc.category not in tools_by_category:
                tools_by_category[doc.category] = []
            tools_by_category[doc.category].append(doc)

        # Build category overview
        overview: List[Dict[str, Any]] = []
        for category_dir in self.docs_dir.iterdir():
            if not category_dir.is_dir():
                continue

            category_name = category_dir.name
            if category_name not in tools_by_category:
                continue  # Skip categories without tools

            # Load category metadata
            meta = self._load_category_meta(category_dir)
            description = meta.description if meta else ""

            # Get key tools: first 3 alphabetically
            tools = tools_by_category[category_name]
            key_tools = sorted([t.name for t in tools])[:3]

            overview.append(
                {
                    "name": category_name,
                    "description": description,
                    "key_tools": key_tools,
                    "tool_count": len(tools),
                }
            )

        # Sort by category name
        return sorted(overview, key=lambda x: x["name"])
