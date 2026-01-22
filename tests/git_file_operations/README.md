# Git/File Operations Integration Tests

This directory contains integration tests for CIDX git operations and file CRUD operations
exposed via MCP/REST APIs. Tests cover 27 operations (23 git + 4 file CRUD) using real
git repositories.

## Test Repository

External test repository: `git@github.com:LightspeedDMS/VivaGoals-to-pptx.git`

This repository is used for integration tests that require real remote operations
(push, pull, fetch). SSH key access is required for these tests.

## Directory Structure

```
tests/git_file_operations/
  conftest.py                    # Pytest fixtures for all tests
  git_repo_state_manager.py      # State capture/restore for idempotent tests
  test_git_repo_state_manager.py # Unit tests for state manager
  test_infrastructure.py         # Infrastructure validation tests
  README.md                      # This file
  scripts/
    run_git_file_ops_tests.sh    # CI script (SSH tests skipped)
    run_integration_tests.sh     # Full integration tests (SSH required)
```

## Running Tests

### CI Mode (No SSH Required)

For CI/CD environments without SSH access to the external repository:

```bash
./scripts/run_git_file_ops_tests.sh
```

This sets `CIDX_SKIP_SSH_TESTS=1` and skips tests marked with `@pytest.mark.requires_ssh`.

### Integration Mode (SSH Required)

For full integration testing with real remote operations:

```bash
./scripts/run_integration_tests.sh
```

Prerequisites:
- SSH key access to `git@github.com:LightspeedDMS/VivaGoals-to-pptx.git`
- SSH agent running with key loaded (`ssh-add`)

### Direct pytest Execution

```bash
# Run all tests (SSH tests may fail without access)
python3 -m pytest tests/git_file_operations/ -v

# Skip SSH-dependent tests
CIDX_SKIP_SSH_TESTS=1 python3 -m pytest tests/git_file_operations/ -v

# Run only specific marker
python3 -m pytest tests/git_file_operations/ -m "not requires_ssh" -v

# Run destructive tests only
python3 -m pytest tests/git_file_operations/ -m "destructive" -v
```

## Test Markers

Tests use pytest markers to categorize behavior:

| Marker | Description |
|--------|-------------|
| `@pytest.mark.requires_ssh` | Requires SSH access to external repository. Skipped when `CIDX_SKIP_SSH_TESTS=1` |
| `@pytest.mark.destructive` | Tests that modify repository state (require careful cleanup) |
| `@pytest.mark.slow` | Tests that take longer to run |
| `@pytest.mark.integration` | Integration tests requiring multiple services |

## Fixtures

### Repository Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `external_repo_dir` | module | Cloned external test repository (requires SSH) |
| `local_test_repo` | module | Local git repository with bare remote (no network) |
| `activated_local_repo` | function | Mocked activation returning local repo path |
| `activated_external_repo` | function | Mocked activation returning external repo path |

### State Management Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `state_manager` | function | GitRepoStateManager for local test repo |
| `captured_state` | function | Auto-captures and restores repo state |
| `external_state_manager` | function | GitRepoStateManager for external repo |
| `external_captured_state` | function | Auto-captures and restores external repo state |

### Test Data Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `test_file_content` | function | Standard test file content |
| `unique_filename` | function | Unique filename for test files |
| `unique_branch_name` | function | Unique branch name for test branches |
| `get_confirmation_token` | function | Factory for confirmation tokens |

### Application Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `mock_user` | module | Mock user with power_user role |
| `test_app` | module | FastAPI app with auth bypass |
| `client` | module | TestClient for HTTP requests |

## Idempotent Tests

All tests are designed to be idempotent using `GitRepoStateManager`:

1. State is captured before each test
2. Test performs operations (create files, commit, push, etc.)
3. State is restored after test (cleanup created files, reset HEAD, etc.)

This ensures tests can be run repeatedly without accumulating side effects.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CIDX_SKIP_SSH_TESTS` | Set to `1`, `true`, or `yes` to skip SSH-dependent tests |

## Test Categories

The tests cover these operation categories:

- **F1: File CRUD** - create_file, edit_file, delete_file, get_file_content
- **F2: Git Status/Inspection** - git_status, git_diff, git_log
- **F3: Git Staging/Commit** - git_stage, git_unstage, git_commit
- **F4: Git Remote Operations** - git_push, git_pull, git_fetch
- **F5: Git Recovery** - git_reset, git_clean, git_merge_abort, git_checkout_file
- **F6: Git Branch Management** - git_branch_list, git_branch_create, git_branch_switch, git_branch_delete

## Adding New Tests

1. Use appropriate fixtures from `conftest.py`
2. Mark tests with relevant markers (`requires_ssh`, `destructive`, etc.)
3. Use `captured_state` fixture for automatic state restoration
4. Follow the existing test patterns for consistency

Example:

```python
@pytest.mark.requires_ssh
@pytest.mark.destructive
def test_push_to_remote(
    external_captured_state,
    activated_external_repo,
    client
):
    """Test pushing commits to remote repository."""
    # Test code here - state will be restored automatically
    pass
```
