"""AstSearchEngine: lazy-loaded tree-sitter AST parsing engine.

The tree_sitter and tree_sitter_languages imports are deferred to
AstSearchEngine.__init__() to preserve CLI startup time (~1.3s baseline).
This module may be imported freely without triggering those heavy imports.

Usage:
    from code_indexer.xray.ast_engine import AstSearchEngine
    engine = AstSearchEngine()          # imports tree_sitter here
    node = engine.parse(source, "python")
    lang = engine.detect_language(Path("foo.py"))
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Union, cast

from code_indexer.xray.errors import XRayExtrasNotInstalled
from code_indexer.xray.languages import (
    EXTENSION_MAP,
    SUPPORTED_LANGUAGES,
    _hcl_available,
)

if TYPE_CHECKING:
    # These imports are for type checkers only — never executed at runtime
    # at module level, preserving the lazy-load invariant.
    from tree_sitter import Parser as _TsParser  # noqa: F401
    from code_indexer.xray.xray_node import XRayNode


def find_enclosing_node(root: "XRayNode", byte_offset: int) -> "XRayNode":
    """Walk the AST tree to find the deepest node containing byte_offset.

    Iteratively descends into the first child whose byte range contains
    byte_offset.  Returns root if no narrower enclosing node exists (e.g.
    byte_offset is past the end of the file or in whitespace between nodes).

    Args:
        root: The root XRayNode of the parse tree.
        byte_offset: Byte position to locate (0-indexed).

    Returns:
        The deepest XRayNode whose [start_byte, end_byte) contains
        byte_offset, or root when no such child exists.
    """
    current = root
    while True:
        found_child = False
        for child in current.children:
            if child.start_byte <= byte_offset < child.end_byte:
                current = child
                found_child = True
                break
        if not found_child:
            return current


class AstSearchEngine:
    """Lazy-loaded AST parsing engine backed by tree-sitter.

    tree_sitter and tree_sitter_languages are imported only inside __init__(),
    ensuring that merely importing this module does not trigger those imports.
    """

    def __init__(self) -> None:
        """Initialise the engine, importing tree-sitter at this point.

        Raises:
            XRayExtrasNotInstalled: if tree_sitter_languages is not installed.
        """
        try:
            import tree_sitter_languages as _tsl  # noqa: F401
        except ImportError:
            raise XRayExtrasNotInstalled("tree_sitter_languages")

        try:
            import tree_sitter as _ts  # noqa: F401
        except ImportError:
            raise XRayExtrasNotInstalled("tree_sitter")

        # Store references so parse() can use them without re-importing
        self._tsl = _tsl
        self._ts = _ts

        # Per-language parser cache: canonical_name -> tree_sitter.Parser
        self._parsers: Dict[str, "_TsParser"] = {}

        # Instance-level registries — start from module-level constants then
        # conditionally extend with optional HCL/Terraform support.
        self._supported_languages: Dict[str, str] = dict(SUPPORTED_LANGUAGES)
        self._extension_map: Dict[str, str] = dict(EXTENSION_MAP)

        if _hcl_available():
            self._supported_languages["terraform"] = "hcl"
            self._extension_map[".tf"] = "terraform"

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def supported_languages(self) -> Dict[str, str]:
        """Mapping from canonical language name to grammar name."""
        return dict(self._supported_languages)

    @property
    def extension_map(self) -> Dict[str, str]:
        """Mapping from file extension (lowercase, with dot) to canonical name."""
        return dict(self._extension_map)

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def detect_language(self, path: Union[str, Path]) -> Optional[str]:
        """Return the canonical language name for a file path, or None.

        Extension lookup is case-insensitive (Path.suffix is lowered).
        """
        ext = Path(path).suffix.lower()
        return cast(Optional[str], self._extension_map.get(ext))

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse(
        self,
        source: Union[str, bytes],
        language: str,
    ) -> "XRayNode":
        """Parse source code and return the root XRayNode.

        Args:
            source: Source code as str or bytes. If str, encoded to UTF-8
                    before handing to tree-sitter. Bytes are passed as-is.
            language: Canonical language name (e.g. "python", "java").

        Returns:
            XRayNode wrapping the root of the parse tree.

        Raises:
            ValueError: if language is not in SUPPORTED_LANGUAGES.
        """
        from code_indexer.xray.xray_node import XRayNode

        if language not in self._supported_languages:
            raise ValueError(
                f"unsupported language: {language!r}. "
                f"Supported: {sorted(self._supported_languages)}"
            )

        parser = self._get_parser(language)

        if isinstance(source, str):
            source_bytes = source.encode("utf-8", errors="replace")
        else:
            source_bytes = source

        tree = parser.parse(source_bytes)
        return XRayNode(tree.root_node)

    def parse_file(self, path: Union[str, Path]) -> "XRayNode":
        """Read a file from disk, detect its language, and parse it.

        Args:
            path: Path to the source file.

        Returns:
            XRayNode wrapping the root of the parse tree.

        Raises:
            ValueError: if the file extension is not recognised.
        """
        file_path = Path(path)
        language = self.detect_language(file_path)
        if language is None:
            raise ValueError(
                f"Cannot detect language for {file_path!r}: "
                f"unknown extension {file_path.suffix!r}"
            )
        source = file_path.read_bytes()
        return self.parse(source, language)

    # ------------------------------------------------------------------
    # Testability / cache introspection
    # ------------------------------------------------------------------

    def _parser_cache_size(self) -> int:
        """Return the number of cached parsers (one per language used so far)."""
        return len(self._parsers)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_parser(self, language: str) -> "_TsParser":
        """Return a cached parser for the given canonical language name.

        Creates and caches the parser on first access.
        """
        if language not in self._parsers:
            grammar_name = self._supported_languages[language]
            ts_language = self._tsl.get_language(grammar_name)
            parser = self._ts.Parser()
            parser.set_language(ts_language)
            self._parsers[language] = parser
        return self._parsers[language]
