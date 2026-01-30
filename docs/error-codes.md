# Error Code System

## Overview

The CIDX server uses a centralized error code system for consistent, traceable logging. Each error code uniquely identifies a specific error condition, enabling accurate issue tracking, deduplication, and actionable monitoring.

## Format Specification

Error codes follow the format: `{SUBSYSTEM}-{CATEGORY}-{NUMBER}`

- **SUBSYSTEM**: 2-6 uppercase letters identifying the functional area
- **CATEGORY**: 2-8 uppercase letters identifying the specific component/operation
- **NUMBER**: Exactly 3 digits (001-999) for unique identification

Examples:
- `AUTH-OIDC-001` - OIDC authentication error #1
- `MCP-TOOL-042` - MCP tool execution error #42
- `GIT-CLONE-999` - Git clone operation error #999

## Subsystem Prefixes

| Prefix | Subsystem | Description |
|--------|-----------|-------------|
| AUTH | Authentication | Authentication and authorization |
| GIT | Git Operations | Git cloning, syncing, pulling, pushing |
| MCP | MCP Protocol | MCP protocol handlers and tools |
| CACHE | Caching | Caching operations |
| REPO | Repository | Repository management |
| QUERY | Query | Semantic and FTS query operations |
| VALID | Validation | Health checks and validation |
| DEPLOY | Deployment | Auto-update and deployment |
| SCIP | SCIP | SCIP code intelligence |
| TELEM | Telemetry | Metrics and telemetry |
| STORE | Storage | Vector and data storage |
| SVC | Services | Service operations |
| WEB | Web | Web routes and HTTP handlers |
| APP | Application | Application lifecycle |

## Usage

### Basic Logging

```python
from code_indexer.server.error_codes import ERROR_REGISTRY
from code_indexer.server.logging_utils import format_error_log, get_log_extra

# Log with error code
logger.error(
    format_error_log("AUTH-OIDC-001", "Failed to connect to OIDC provider", issuer=issuer_url),
    extra=get_log_extra("AUTH-OIDC-001")
)
```

### Log Format

Error log messages follow this pattern:
```
[{ERROR_CODE}] operation context: details key1=value1 key2=value2
```

Example output:
```
[AUTH-OIDC-001] Failed to connect to OIDC provider issuer=https://example.com
```

### Correlation IDs

Correlation IDs are automatically included in the `extra` dict when available:

```python
extra = get_log_extra("AUTH-OIDC-001")
# Returns: {"error_code": "AUTH-OIDC-001", "correlation_id": "req-12345"}
```

### Sensitive Data

Use `sanitize_for_logging()` to redact sensitive information:

```python
from code_indexer.server.logging_utils import sanitize_for_logging

user_data = {"username": "admin", "password": "secret"}
logger.error(
    format_error_log("AUTH-LOGIN-001", "Login failed", **sanitize_for_logging(user_data)),
    extra=get_log_extra("AUTH-LOGIN-001")
)
# Logs: [AUTH-LOGIN-001] Login failed username=admin password=***REDACTED***
```

## Error Registry

All error codes are defined in `src/code_indexer/server/error_codes.py`:

```python
ERROR_REGISTRY = {
    "AUTH-HYBRID-001": ErrorDefinition(
        code="AUTH-HYBRID-001",
        description="User manager not initialized during hybrid authentication",
        severity=Severity.ERROR,
        action="Check application initialization - user_manager should be initialized"
    ),
    # ... more error codes
}
```

### Looking Up Error Definitions

```python
from code_indexer.server.error_codes import get_error_definition

error_def = get_error_definition("AUTH-HYBRID-001")
if error_def:
    print(f"Code: {error_def.code}")
    print(f"Description: {error_def.description}")
    print(f"Severity: {error_def.severity.value}")
    print(f"Action: {error_def.action}")
```

## Migration Guide

### Step 1: Define Error Code

Add entry to `ERROR_REGISTRY` in `src/code_indexer/server/error_codes.py`:

```python
"AUTH-OIDC-001": ErrorDefinition(
    code="AUTH-OIDC-001",
    description="OIDC discovery endpoint unreachable",
    severity=Severity.ERROR,
    action="Check OIDC provider connectivity and configuration"
),
```

### Step 2: Update Import Statements

Add imports to the file containing the log statement:

```python
from code_indexer.server.logging_utils import format_error_log, get_log_extra
```

### Step 3: Migrate Log Statement

**Before:**
```python
logger.error(f"Failed to connect to OIDC provider: {issuer}")
```

**After:**
```python
logger.error(
    format_error_log("AUTH-OIDC-001", "Failed to connect to OIDC provider", issuer=issuer),
    extra=get_log_extra("AUTH-OIDC-001")
)
```

### Step 4: Verify Format

Run tests to ensure the migration doesn't break functionality:

```bash
pytest tests/unit/server/test_error_codes.py -v
pytest tests/unit/server/test_logging_utils.py -v
pytest tests/e2e/test_error_code_logging.py -v
```

## Proof-of-Concept Implementation

A complete proof-of-concept migration has been implemented in:
- **File**: `src/code_indexer/server/auth/dependencies.py`
- **Error Codes**: AUTH-HYBRID-001, AUTH-HYBRID-002, AUTH-HYBRID-003
- **Tests**: All unit and E2E tests passing

This demonstrates the pattern and serves as a reference for future migrations.

## Current Status

**Proof-of-Concept Complete**: 3/813 log statements migrated (0.37%)

- Error code infrastructure: Complete
- Logging utilities: Complete
- Test coverage: 100% of implemented features
- Documentation: Complete
- Remaining work: 810 log statements across 95 files

## Self-Monitoring Integration

Error codes are designed for integration with self-monitoring systems:

```python
from code_indexer.server.error_codes import ERROR_REGISTRY

# Deduplication uses error_code as primary fingerprint
for code, definition in ERROR_REGISTRY.items():
    print(f"{code}: {definition.description}")
```

## Verification

Check for untagged log statements:

```bash
# This should return 0 matches after full migration
grep -rE "logger\.(warning|error|critical)\(" src/code_indexer/server/ | grep -v "\[.*-.*-[0-9]"
```

Current count: 810 untagged statements remaining.
