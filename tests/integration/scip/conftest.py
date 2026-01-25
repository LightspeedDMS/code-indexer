"""
Shared fixtures for SCIP MCP/REST parity tests - Story #42.

Provides common fixtures used across parity test files.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock

from code_indexer.server.auth.user_manager import User, UserRole


# Mock return value data - extracted for reusability and readability
MOCK_DEFINITION_RESULTS = [
    {
        "symbol": "UserService",
        "project": "test-repo",
        "file_path": "src/services/user.py",
        "line": 45,
        "column": 6,
        "kind": "definition",
        "relationship": None,
        "context": "class UserService:",
    }
]

MOCK_REFERENCES_RESULTS = [
    {
        "symbol": "authenticate",
        "project": "test-repo",
        "file_path": "src/auth/handler.py",
        "line": 15,
        "column": 10,
        "kind": "reference",
        "relationship": "call",
        "context": "user.authenticate()",
    },
    {
        "symbol": "authenticate",
        "project": "test-repo",
        "file_path": "src/api/routes.py",
        "line": 30,
        "column": 4,
        "kind": "reference",
        "relationship": "call",
        "context": "service.authenticate(token)",
    },
]

MOCK_DEPENDENCIES_RESULTS = [
    {
        "symbol": "Database",
        "project": "test-repo",
        "file_path": "src/db/connection.py",
        "line": 10,
        "column": 0,
        "kind": "dependency",
        "relationship": "import",
        "context": "from db import Database",
    }
]

MOCK_DEPENDENTS_RESULTS = [
    {
        "symbol": "OrderService",
        "project": "test-repo",
        "file_path": "src/services/order.py",
        "line": 20,
        "column": 4,
        "kind": "dependent",
        "relationship": "call",
        "context": "payment_processor.process()",
    }
]

MOCK_IMPACT_RESULTS = {
    "target_symbol": "PaymentProcessor",
    "depth_analyzed": 3,
    "total_affected": 5,
    "truncated": False,
    "affected_symbols": [
        {
            "symbol": "OrderService",
            "file_path": "src/services/order.py",
            "line": 20,
            "column": 4,
            "depth": 1,
            "relationship": "call",
            "chain": [],
        }
    ],
    "affected_files": [
        {
            "path": "src/services/order.py",
            "project": "test-repo",
            "affected_symbol_count": 1,
            "min_depth": 1,
            "max_depth": 1,
        }
    ],
}

MOCK_CALLCHAIN_RESULTS = [
    {
        "path": ["handleRequest", "validateInput", "sanitize"],
        "length": 3,
        "has_cycle": False,
    }
]

MOCK_CONTEXT_RESULTS = {
    "target_symbol": "UserService",
    "summary": "Read these 2 file(s)",
    "files": [
        {
            "path": "src/services/user.py",
            "project": "test-repo",
            "relevance_score": 1.0,
            "symbols": [
                {
                    "name": "UserService",
                    "kind": "class",
                    "relationship": "definition",
                    "line": 10,
                    "column": 0,
                    "relevance": 1.0,
                }
            ],
            "read_priority": 1,
        }
    ],
    "total_files": 2,
    "total_symbols": 5,
    "avg_relevance": 0.85,
}


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    return User(
        username="testuser",
        email="test@example.com",
        full_name="Test User",
        role=UserRole.NORMAL_USER,
        password_hash="hashed_password",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_scip_query_service():
    """Create a mock SCIPQueryService with pre-configured return values."""
    from code_indexer.server.services.scip_query_service import SCIPQueryService

    mock_service = Mock(spec=SCIPQueryService)

    mock_service.find_definition.return_value = MOCK_DEFINITION_RESULTS
    mock_service.find_references.return_value = MOCK_REFERENCES_RESULTS
    mock_service.get_dependencies.return_value = MOCK_DEPENDENCIES_RESULTS
    mock_service.get_dependents.return_value = MOCK_DEPENDENTS_RESULTS
    mock_service.analyze_impact.return_value = MOCK_IMPACT_RESULTS
    mock_service.trace_callchain.return_value = MOCK_CALLCHAIN_RESULTS
    mock_service.get_context.return_value = MOCK_CONTEXT_RESULTS

    return mock_service
