"""SCIP primitive query operations."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, TYPE_CHECKING

from .loader import SCIPLoader

if TYPE_CHECKING:
    from .backends import CallChain, DatabaseBackend


@dataclass
class QueryResult:
    """Result from a SCIP query operation."""

    symbol: str
    project: str
    file_path: str
    line: int
    column: int
    kind: str  # "definition", "reference", "dependency", "dependent"
    relationship: Optional[str] = None  # "call", "import", "extends", etc.
    context: Optional[str] = None  # Source line snippet
    depth: Optional[int] = None  # Depth level for transitive queries


def _is_definition(symbol_roles: int) -> bool:
    """
    Check if occurrence is a definition based on symbol_roles.

    Args:
        symbol_roles: SCIP symbol roles bitmask

    Returns:
        True if symbol_roles indicates a definition (bit 0 set)
    """
    return (symbol_roles & 1) == 1


def _parse_location(occurrence) -> Tuple[int, int]:
    """
    Parse line and column from occurrence range.

    Args:
        occurrence: SCIP occurrence object with range field

    Returns:
        Tuple of (line, column), defaulting to (0, 0) if range is invalid
    """
    if len(occurrence.range) >= 2:
        return occurrence.range[0], occurrence.range[1]
    return 0, 0


def _matches_symbol(occurrence_symbol: str, target_symbol: str, exact: bool) -> bool:
    """
    Check if occurrence symbol matches target symbol.

    Args:
        occurrence_symbol: Symbol from SCIP occurrence (full symbol with path)
        target_symbol: Target symbol to match (simple name like "CacheEntry")
        exact: If True, match exact symbol name; if False, match substring

    Returns:
        True if symbols match according to exact/substring rules
    """
    if exact:
        # Extract symbol name from SCIP format:
        # Class: .../ClassName#
        # Method: .../ClassName#method().
        # Attribute: .../ClassName#attribute.
        if "/" in occurrence_symbol:
            symbol_path = occurrence_symbol.split("/")[-1]

            # Normalize occurrence symbol: clean suffixes but keep structure
            normalized_occurrence = symbol_path
            if normalized_occurrence.endswith("()."):
                # Method: ClassName#method(). -> ClassName#method
                normalized_occurrence = normalized_occurrence[:-3]
            elif normalized_occurrence.endswith("."):
                # Attribute: ClassName#attr. -> ClassName#attr
                normalized_occurrence = normalized_occurrence[:-1]
            elif normalized_occurrence.endswith("#"):
                # Class: ClassName# -> ClassName
                normalized_occurrence = normalized_occurrence[:-1]

            # Normalize target symbol: clean suffixes but keep structure
            normalized_target = target_symbol
            if normalized_target.endswith("()."):
                normalized_target = normalized_target[:-3]
            elif normalized_target.endswith("()"):
                normalized_target = normalized_target[:-2]
            elif normalized_target.endswith("."):
                normalized_target = normalized_target[:-1]
            elif normalized_target.endswith("#"):
                normalized_target = normalized_target[:-1]

            # Compare normalized forms
            # If target has #, compare full "ClassName#method"
            # If target has no #, extract just class or method name from occurrence
            if "#" in normalized_target:
                # Full qualified name: "CacheEntry#__init__"
                return normalized_occurrence == normalized_target
            elif "#" in normalized_occurrence:
                # Target is simple name, occurrence is qualified
                # Extract either class name (before #) or member name (after #)
                parts = normalized_occurrence.split("#")
                if len(parts) == 2:
                    # Check both class name and member name
                    return (
                        normalized_target == parts[0] or normalized_target == parts[1]
                    )
                return normalized_target == parts[0]
            else:
                # Both are simple names
                return normalized_occurrence == normalized_target

        return occurrence_symbol == target_symbol
    return target_symbol.lower() in occurrence_symbol.lower()


def _determine_relationship(symbol_roles: int) -> str:
    """
    Determine relationship type from symbol roles bitmask.

    Args:
        symbol_roles: SCIP symbol roles bitmask

    Returns:
        Relationship type string ("import", "write", "call", or "reference")
    """
    if symbol_roles & 2:  # Import bit
        return "import"
    elif symbol_roles & 4:  # WriteAccess bit
        return "write"
    elif symbol_roles & 8:  # ReadAccess bit
        return "call"
    return "reference"


def _find_enclosing_symbol(
    ref_line: int, enclosing_range: list, symbol_def_locations: List[Tuple[str, int]]
) -> Optional[str]:
    """
    Find the specific symbol that contains a reference using proximity heuristics.

    Args:
        ref_line: Line number of the reference
        enclosing_range: SCIP enclosing_range field (may be empty)
        symbol_def_locations: List of (symbol, line) tuples sorted by line number

    Returns:
        The symbol name that contains this reference, or None if not found
    """
    # Strategy 1: Use SCIP enclosing_range if available
    if len(enclosing_range) >= 2:
        enclosing_start_line = enclosing_range[0]
        # Find the definition that matches the enclosing range start
        for symbol, def_line in symbol_def_locations:
            if def_line == enclosing_start_line:
                return symbol

    # Strategy 2: Proximity heuristic - find most recent definition before reference
    # The enclosing symbol is the last definition that occurs before the reference line
    candidate = None
    for symbol, def_line in symbol_def_locations:
        if def_line <= ref_line:
            candidate = symbol
        else:
            # Definitions are sorted by line, so once we exceed ref_line, stop
            break

    return candidate


def _find_scip_repo_root(scip_dir: Path) -> Optional[Path]:
    """
    Locate the real repo root for a SCIP output directory (Bug #1630).

    Walks UP ``scip_dir``'s own ancestor name-chain looking for the literal
    ``.code-indexer/scip`` path-segment pair that a SCIPGenerator-produced
    ``scip_dir`` must be nested under
    (``<repo_root>/.code-indexer/scip/[<relative_path>/]``). Returns the
    corresponding repo root (the parent of that ``.code-indexer``), or
    ``None`` if the pair never appears in ``scip_dir``'s own lineage.

    Deliberately NOT implemented as a generic "does this ancestor have a
    .code-indexer child" walk: a dev repo can legitimately contain OTHER,
    unrelated ``.code-indexer`` directories at intermediate ancestor
    levels -- verified live in this repo's own tree during Bug #1630's
    fix (``/tmp/.code-indexer``, an unrelated cidx project accidentally
    rooted at ``/tmp`` and therefore an ancestor of every pytest
    ``tmp_path``; and ``tests/.code-indexer``, an unrelated nested
    fixture project). A "has a .code-indexer child" check picks up those
    unrelated projects' ``config.json`` instead of the real one. Matching
    the literal ``.code-indexer/scip`` segment pair in ``scip_dir``'s OWN
    name-chain avoids that false-positive class entirely, since it never
    probes any ancestor's *children* -- only ``scip_dir``'s own ancestry.
    """
    current = scip_dir.resolve()
    for ancestor in [current] + list(current.parents):
        if ancestor.name == "scip" and ancestor.parent.name == ".code-indexer":
            return ancestor.parent.parent
    return None


class SCIPQueryEngine:
    """Engine for executing SCIP primitive queries."""

    backend: "DatabaseBackend"

    def __init__(self, scip_file: Path):
        """
        Initialize query engine with a SCIP index.

        Requires .scip.db database file to exist. Use DatabaseBackend exclusively.

        Args:
            scip_file: Path to .scip file OR .scip.db file

        Raises:
            FileNotFoundError: If .scip.db database file does not exist
        """
        # Handle both .scip and .scip.db paths
        scip_file_str = str(scip_file)
        if scip_file_str.endswith(".db"):
            # Already .scip.db path - strip .db to get .scip path
            self.scip_file = Path(scip_file_str.removesuffix(".db"))
            self.db_path = scip_file
        else:
            # .scip path provided
            self.scip_file = scip_file
            self.db_path = Path(scip_file_str + ".db")

        # Verify database exists
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Database file {self.db_path} not found. "
                f"Run 'cidx scip generate' to create the database."
            )

        # Load protobuf index only if .scip file exists (it's deleted after conversion to save space)
        self.loader = SCIPLoader()
        if self.scip_file.exists():
            self.index = self.loader.load(self.scip_file)
            project_root = self.index.metadata.project_root
        else:
            # .scip file was deleted - derive project root from db path.
            #
            # Database path structure (SCIPGenerator, Bug #1630):
            #   <repo_root>/.code-indexer/scip/[<relative_path>/]index.scip.db
            # <relative_path> is empty for the top-level project and an
            # arbitrary-depth sub-project path for a monorepo module
            # discovered by ProjectDiscovery. A fixed two-level parent walk
            # (scip_dir.parent.parent) only produces the correct root for
            # the top-level (empty relative_path) case; for any sub-project
            # it undershoots by however many levels relative_path adds.
            #
            # Fix: locate the real repo root independently of nesting depth
            # by matching the literal ".code-indexer/scip" segment pair in
            # scip_dir's own ancestry (_find_scip_repo_root), then
            # re-derive the sub-project's own directory by re-applying the
            # same relative-path suffix under the repo root instead of
            # under ".code-indexer/scip". This keeps project_root
            # consistent with how document-relative paths are actually
            # recorded -- relative to the sub-project's OWN directory, not
            # the repo root (verified against a real SCIPDatabaseBuilder-
            # built sub-project database) -- which
            # DatabaseBackend._read_context_lines() requires to resolve
            # source files on disk.
            #
            # When scip_dir does not follow that convention at all (e.g. a
            # synthetic/non-standard test fixture path), fall back to the
            # pre-#1630 heuristic rather than crash -- there is no valid
            # sub-project depth to correct for on a path that never had
            # the ".code-indexer/scip" structure in the first place.
            self.index = None
            scip_dir = self.db_path.parent

            repo_root = _find_scip_repo_root(scip_dir)
            if repo_root is None:
                project_root = str(scip_dir.parent.parent)
            else:
                scip_base_dir = repo_root / ".code-indexer" / "scip"
                relative_suffix = scip_dir.resolve().relative_to(
                    scip_base_dir.resolve()
                )
                if relative_suffix == Path("."):
                    project_root = str(repo_root)
                else:
                    project_root = str(repo_root / relative_suffix)

        from .backends import DatabaseBackend

        self.backend = DatabaseBackend(
            self.db_path,
            project_root=project_root,
            scip_file=self.scip_file if self.scip_file.exists() else None,
        )
        self.db_conn = self.backend.conn

    def find_definition(self, symbol: str, exact: bool = False) -> List[QueryResult]:
        """
        Find definition locations for a symbol.

        Uses database backend if available (300-400x faster), otherwise falls back
        to protobuf scanning.

        Args:
            symbol: Symbol name to search for (e.g., "UserService", "authenticate")
            exact: If True, match exact symbol name; if False, match substring

        Returns:
            List of QueryResult objects with definition locations
        """
        return self.backend.find_definition(symbol, exact=exact)

    def find_references(
        self, symbol: str, limit: int = 100, exact: bool = False
    ) -> List[QueryResult]:
        """
        Find all references to a symbol.

        Uses database backend if available (300-400x faster), otherwise falls back
        to protobuf scanning.

        Note: Database backend currently only supports exact=True matching behavior
        (SCIP symbol path matching). When exact=False is requested with database
        backend, falls back to protobuf scanning for substring matching.

        Args:
            symbol: Symbol name to search for
            limit: Maximum number of results to return
            exact: If True, match exact symbol name; if False, match substring

        Returns:
            List of QueryResult objects with reference locations
        """
        return self.backend.find_references(symbol, limit=limit, exact=exact)

    def get_dependencies(
        self, symbol: str, depth: int = 1, exact: bool = False
    ) -> List[QueryResult]:
        """
        Get symbols that this symbol depends on.

        Uses database backend if available (150-200x faster), otherwise falls back
        to protobuf scanning.

        Args:
            symbol: Symbol name to analyze
            depth: Depth of transitive dependencies (1 = direct only)
            exact: If True, match exact symbol name; if False, match substring

        Returns:
            List of QueryResult objects with dependency information
        """
        return self.backend.get_dependencies(symbol, depth=depth, exact=exact)

    def get_dependents(
        self, symbol: str, depth: int = 1, exact: bool = False
    ) -> List[QueryResult]:
        """
        Get symbols that depend on this symbol.

        Uses database backend if available (150-200x faster), otherwise falls back
        to protobuf scanning.

        Args:
            symbol: Symbol name to analyze
            depth: Depth of transitive dependents (1 = direct only)
            exact: If True, match exact symbol name; if False, match substring

        Returns:
            List of QueryResult objects with dependent information
        """
        return self.backend.get_dependents(symbol, depth=depth, exact=exact)

    def analyze_impact(self, symbol: str, depth: int = 3):
        """
        Analyze impact of changing symbol (transitive dependents grouped by file).

        Uses database backend if available (150x faster), otherwise falls back
        to protobuf scanning.

        Args:
            symbol: Symbol name to analyze
            depth: Maximum dependency depth (1-10, default 3)

        Returns:
            List of ImpactResult objects with file_path, symbol_count, and symbols
        """
        return self.backend.analyze_impact(symbol, depth=depth)

    def trace_call_chain(
        self,
        from_symbol: str,
        to_symbol: str,
        max_depth: int = 5,
        limit: int = 100,
        timeout_errors: Optional[List[str]] = None,
    ) -> List["CallChain"]:
        """
        Trace all call chains from entry point to target function.

        Finds all execution paths through the call graph from a starting symbol
        (e.g., API handler) to a target symbol (e.g., database function). Useful
        for understanding code flow, debugging, and security analysis.

        Args:
            from_symbol: Entry point symbol name
            to_symbol: Target function symbol name
            max_depth: Maximum path length in hops (default 5). The
                recursive-CTE query enumerates ALL distinct paths (not
                shortest-path), so the backend clamps this down to
                MAX_DEPTH_CAP (3, in scip/database/queries.py) regardless
                of what is passed here (Bug #1603).
            limit: Maximum number of paths to return (default 100)
            timeout_errors: Optional list this call appends a message to
                if the underlying query times out (Bug #1603 code review
                Priority 1) -- forwarded unchanged to the backend.

        Returns:
            List of CallChain objects sorted by length (shortest first).
            Each CallChain has:
                - path: List[str] of symbol names in execution order
                - length: int number of hops in the chain
                - has_cycle: bool indicating if path contains a cycle

        Example:
            >>> engine = SCIPQueryEngine(scip_file)
            >>> chains = engine.trace_call_chain("api_handler", "db_query", max_depth=5)
            >>> for chain in chains:
            ...     print(f"Path ({chain.length} hops): {' -> '.join(chain.path)}")
        """
        return self.backend.trace_call_chain(
            from_symbol,
            to_symbol,
            max_depth=max_depth,
            limit=limit,
            timeout_errors=timeout_errors,
        )
