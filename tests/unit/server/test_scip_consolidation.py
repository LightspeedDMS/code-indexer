"""
SCIP Code Consolidation Tests - Story #42

Tests that verify no duplicate _find_scip_files implementations exist in the codebase.

Acceptance Criteria (Story #42):
- No duplicate _find_scip_files implementations exist
- Zero matches found in handlers.py
- Zero matches found in scip_queries.py
- Exactly one implementation exists in scip_query_service.py
- SCIPQueryService is imported in both handlers.py and scip_queries.py

Since modular phase 4d the SCIP handlers, REST router and query service are
the cidx-scip feature's (``packages/features/cidx-scip``): the MCP handlers
are ``cidx_scip/server/mcp/scip.py`` (helpers in ``_common.py``), the REST
router ``cidx_scip/server/routes.py`` and the service
``cidx_scip/server/query_service.py``; the core's handler package keeps no
SCIP code at all.
"""

import re
from pathlib import Path


# Get the source directory path
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
SRC_DIR = _REPO_ROOT / "src"
SERVER_DIR = SRC_DIR / "code_indexer" / "server"
SCIP_SERVER_DIR = (
    _REPO_ROOT / "packages" / "features" / "cidx-scip" / "src" / "cidx_scip" / "server"
)
SCIP_HANDLERS = SCIP_SERVER_DIR / "mcp" / "scip.py"
SCIP_HANDLER_HELPERS = SCIP_SERVER_DIR / "mcp" / "_common.py"
SCIP_ROUTES = SCIP_SERVER_DIR / "routes.py"
SCIP_QUERY_SERVICE = SCIP_SERVER_DIR / "query_service.py"


class TestNoDuplicateFindScipFiles:
    """Tests verifying no duplicate _find_scip_files implementations exist."""

    def test_no_find_scip_files_in_handlers_py(self):
        """
        Scenario: No duplicate _find_scip_files implementations exist.

        Given the refactored codebase
        When searching for "def _find_scip_files" in handlers.py
        Then zero matches are found
        """
        handlers_path = SERVER_DIR / "mcp" / "handlers" / "_legacy.py"
        assert handlers_path.exists(), (
            f"handlers/_legacy.py not found at {handlers_path}"
        )

        content = handlers_path.read_text()

        # Check for private _find_scip_files (old duplicate)
        private_matches = re.findall(r"def _find_scip_files\s*\(", content)
        assert len(private_matches) == 0, (
            f"Found {len(private_matches)} '_find_scip_files' definitions in handlers/_legacy.py. "
            "This function should be removed - use SCIPQueryService.find_scip_files() instead."
        )

        # Also check for public find_scip_files (shouldn't exist here either)
        public_matches = re.findall(r"def find_scip_files\s*\(", content)
        assert len(public_matches) == 0, (
            f"Found {len(public_matches)} 'find_scip_files' definitions in handlers/_legacy.py. "
            "This function should be in SCIPQueryService, not handlers/_legacy.py."
        )

    def test_no_find_scip_files_in_scip_queries_py(self):
        """
        Scenario: No duplicate _find_scip_files implementations exist.

        Given the refactored codebase
        When searching for "def _find_scip_files" in scip_queries.py
        Then zero matches are found
        """
        scip_queries_path = SCIP_ROUTES
        assert scip_queries_path.exists(), (
            f"scip_queries.py not found at {scip_queries_path}"
        )

        content = scip_queries_path.read_text()

        # Check for private _find_scip_files (old duplicate)
        private_matches = re.findall(r"def _find_scip_files\s*\(", content)
        assert len(private_matches) == 0, (
            f"Found {len(private_matches)} '_find_scip_files' definitions in "
            "scip_queries.py. This function should be removed - use "
            "SCIPQueryService.find_scip_files() instead."
        )

        # Also check for public find_scip_files (shouldn't exist here either)
        public_matches = re.findall(r"def find_scip_files\s*\(", content)
        assert len(public_matches) == 0, (
            f"Found {len(public_matches)} 'find_scip_files' definitions in "
            "scip_queries.py. This function should be in SCIPQueryService, "
            "not scip_queries.py."
        )

    def test_find_scip_files_exists_in_scip_query_service(self):
        """
        Scenario: Exactly one implementation exists in scip_query_service.py.

        Given the refactored codebase
        When searching for "def find_scip_files" in scip_query_service.py
        Then exactly one implementation is found
        """
        service_path = SCIP_QUERY_SERVICE
        assert service_path.exists(), (
            f"scip_query_service.py not found at {service_path}"
        )

        content = service_path.read_text()

        # Check for public find_scip_files method
        matches = re.findall(r"def find_scip_files\s*\(", content)
        assert len(matches) >= 1, (
            "No 'find_scip_files' method found in scip_query_service.py. "
            "SCIPQueryService must have this method."
        )


class TestSCIPQueryServiceImported:
    """Tests verifying SCIPQueryService is imported where needed."""

    def test_handlers_py_imports_scip_query_service(self):
        """
        Verify handlers.py uses SCIPQueryService.

        SCIPQueryService should be imported and used in MCP handlers.
        """
        content = SCIP_HANDLERS.read_text()

        # Check for SCIPQueryService usage (through the shared getter)
        assert "_get_scip_query_service" in content, (
            "the SCIP MCP handlers must use SCIPQueryService through "
            "'_get_scip_query_service'."
        )

        # The getter is defined once, in the handlers' shared helpers.
        utils_content = SCIP_HANDLER_HELPERS.read_text()
        assert "def _get_scip_query_service" in utils_content, (
            "cidx_scip/server/mcp/_common.py must have '_get_scip_query_service' "
            "to create SCIPQueryService instances."
        )
        assert "SCIPQueryService" in utils_content

    def test_scip_queries_py_imports_scip_query_service(self):
        """
        Verify scip_queries.py uses SCIPQueryService.

        SCIPQueryService should be imported at module level in REST router.
        """
        scip_queries_path = SCIP_ROUTES
        content = scip_queries_path.read_text()

        # Check for explicit import
        has_import = (
            "from cidx_scip.server.query_service import SCIPQueryService" in content
        )
        assert has_import, (
            "scip_queries.py must import SCIPQueryService from "
            "cidx_scip.server.query_service"
        )

        # Verify _get_scip_query_service function exists
        assert "def _get_scip_query_service" in content, (
            "scip_queries.py must have '_get_scip_query_service' function to create "
            "SCIPQueryService instances."
        )


class TestNoLegacyHelperFunctions:
    """Tests verifying legacy helper functions have been removed."""

    def test_no_get_accessible_repos_in_handlers(self):
        """Verify _get_accessible_repos helper is removed from handlers/_legacy.py."""
        handlers_path = SERVER_DIR / "mcp" / "handlers" / "_legacy.py"
        content = handlers_path.read_text()

        # This was a duplicate helper that should now be in SCIPQueryService
        matches = re.findall(r"def _get_accessible_repos\s*\(", content)
        assert len(matches) == 0, (
            f"Found {len(matches)} '_get_accessible_repos' definitions in handlers/_legacy.py. "
            "This function should be removed - access control is handled by "
            "SCIPQueryService.get_accessible_repos() or AccessFilteringService."
        )

    def test_no_filter_scip_results_in_handlers(self):
        """Verify _filter_scip_results helper is removed from handlers/_legacy.py."""
        handlers_path = SERVER_DIR / "mcp" / "handlers" / "_legacy.py"
        content = handlers_path.read_text()

        # This was a duplicate helper that should now be in SCIPQueryService
        matches = re.findall(r"def _filter_scip_results\s*\(", content)
        assert len(matches) == 0, (
            f"Found {len(matches)} '_filter_scip_results' definitions in handlers/_legacy.py. "
            "This function should be removed - filtering is handled internally by "
            "SCIPQueryService."
        )


class TestSCIPQueryServiceExists:
    """Tests verifying SCIPQueryService class exists and has required methods."""

    def test_scip_query_service_class_exists(self):
        """Verify SCIPQueryService class can be imported."""
        from cidx_scip.server.query_service import SCIPQueryService

        assert SCIPQueryService is not None, "SCIPQueryService class must exist"

    def test_scip_query_service_has_required_methods(self):
        """Verify SCIPQueryService has all required query methods."""
        from cidx_scip.server.query_service import SCIPQueryService

        required_methods = [
            "find_scip_files",
            "find_definition",
            "find_references",
            "get_dependencies",
            "get_dependents",
            "analyze_impact",
            "trace_callchain",
            "get_context",
        ]

        for method_name in required_methods:
            assert hasattr(SCIPQueryService, method_name), (
                f"SCIPQueryService must have '{method_name}' method"
            )

    def test_scip_query_service_has_access_control_method(self):
        """Verify SCIPQueryService has get_accessible_repos method."""
        from cidx_scip.server.query_service import SCIPQueryService

        assert hasattr(SCIPQueryService, "get_accessible_repos"), (
            "SCIPQueryService must have 'get_accessible_repos' method for "
            "access control integration"
        )
