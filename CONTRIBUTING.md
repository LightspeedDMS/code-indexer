# Contributing to CIDX

Thank you for considering contributing to CIDX! This guide will help you set up your development environment and understand our development workflow.

## Development Setup

### Prerequisites

- Python 3.9 or higher
- Git
- VoyageAI API key (for testing semantic search features)

### Initial Setup

1. **Fork and clone the repository**

   ```bash
   git clone https://github.com/YOUR_USERNAME/code-indexer.git
   cd code-indexer
   ```

2. **Initialize submodules**

   ```bash
   git submodule update --init --recursive
   ```

   This pulls required dependencies:
   - `third_party/hnswlib` - HNSW vector index library
   - `test-fixtures/multimodal-mock-repo` - E2E test fixtures (if present)

3. **Install development dependencies**

   ```bash
   python3 -m pip install -e ".[dev]" --break-system-packages
   ```

   This installs CIDX in editable mode with all development dependencies including:
   - pytest (testing framework)
   - mypy (type checking)
   - ruff (linting and formatting)
   - pre-commit (git hooks)

4. **Install pre-commit hooks** (CRITICAL)

   ```bash
   pre-commit install
   ```

   This installs git hooks that automatically check your code before each commit. **All contributors must install these hooks** to ensure code quality.

### Pre-commit Hooks

All commits are automatically validated for:

- **Linting**: Ruff checks for code quality issues and auto-fixes many of them
- **Formatting**: Ruff-format ensures consistent code style
- **Type Checking**: Mypy validates type annotations on `src/` code
- **Standard Checks**: Trailing whitespace, EOF newlines, YAML syntax, etc.

**What happens when you commit:**

```bash
git add my_changes.py
git commit -m "Add feature"
# Pre-commit hooks run automatically
# If checks fail, files are auto-fixed when possible
# Re-stage and commit again:
git add my_changes.py
git commit -m "Add feature"
```

**Manual pre-commit execution:**

```bash
# Run on all files (useful after pulling changes)
pre-commit run --all-files

# Run on staged files only
pre-commit run
```

## Architecture Overview

CIDX v8.0+ uses a container-free, filesystem-based architecture:

### Operational Modes

1. **CLI Mode** (Direct, Local)
   - Direct command-line tool for local semantic code search
   - Vectors stored in `.code-indexer/index/` as JSON files
   - No daemon, no server, no network required

2. **Daemon Mode** (Local, Cached)
   - Local RPyC-based background service for faster queries
   - In-memory HNSW/FTS index caching
   - Unix socket communication (`.code-indexer/daemon.sock`)

### Key Components

- **VoyageAI** - Only supported embedding provider (voyage-3, voyage-3-large, voyage-code-3)
- **FilesystemVectorStore** - Container-free vector storage
- **HNSW** - Graph-based approximate nearest neighbor search
- **Tantivy** - Full-text search (FTS) with regex support

## Code Quality Standards

### Perfect Linting

CIDX maintains **zero linting errors**:

- Ruff: 0 errors
- Mypy: 0 errors (on `src/` code)
- Ruff-format: All files formatted consistently

Run linting manually with `./lint.sh`:

```bash
# Check and auto-fix linting issues
./lint.sh

# Or manually:
ruff check src/ tests/
ruff format src/ tests/
mypy src/
```

### Type Annotations

- All functions in `src/` should have type annotations
- Use `from typing import` for type hints
- Use `cast()` when mypy needs help inferring types
- Tests (`tests/`) don't require full type annotations

### Code Style

- Follow PEP 8 (enforced by ruff)
- Use descriptive variable names
- Keep functions focused and small
- Document complex logic with comments

## Testing

### Testing Hierarchy (CRITICAL)

Follow this workflow during development:

```
1. Targeted unit tests (FAST - seconds)
   |
   v
2. Manual testing (verify feature works)
   |
   v
3. fast-automation.sh (FINAL GATE - must pass before done)
```

**NEVER run fast-automation.sh after every small change.** That wastes time.

### During Development - Targeted Tests Only

Run specific tests related to your changes:

```bash
# Change base_client.py -> run related tests
pytest tests/unit/api_clients/test_base_*.py -v --tb=short

# Change handlers.py -> run handler tests
pytest tests/unit/server/mcp/test_handlers*.py -v --tb=short

# Run specific test function
pytest tests/unit/test_something.py::test_function_name -v

# Run tests matching pattern
pytest tests/ -k "test_scip" -v
```

Targeted tests give fast feedback (seconds, not minutes).

### Final Validation - fast-automation.sh

Run **only after ALL changes are complete**:

```bash
# Full test suite (~6-7 minutes, 865+ tests)
./fast-automation.sh
```

**Performance Requirements:**
- Must complete in under 10 minutes
- If exceeded, investigate with `pytest --durations=20`
- Move inherently slow tests (>30s) to full-automation.sh
- Mark slow tests with `@pytest.mark.slow`

### Test Suites

| Suite | Tests | Time | When to Use |
|-------|-------|------|-------------|
| Targeted pytest | varies | seconds | During development |
| fast-automation.sh | 865+ | ~6-7 min | Final validation before commit |
| server-fast-automation.sh | varies | varies | Server-specific changes |
| full-automation.sh | all | 10+ min | Complete validation (ask user to run) |

### Writing Tests

- Use pytest for all tests
- Follow existing test patterns in the codebase
- Test files go in `tests/unit/`, `tests/integration/`, or `tests/e2e/`
- Aim for >85% code coverage for new features
- Use real implementations where possible, minimize mocking

### Test Organization

```
tests/
├── unit/           # Fast unit tests, no external dependencies
├── integration/    # Tests requiring multiple components
├── e2e/            # End-to-end workflow tests
│   ├── server/     # Server E2E tests
│   └── multimodal/ # Multimodal image vectorization tests
└── conftest.py     # Shared fixtures
```

## Development Workflow

### Making Changes

1. **Create a feature branch**

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write code following quality standards
   - Add/update tests as needed
   - Update documentation if needed

3. **Run targeted tests during development**

   ```bash
   pytest tests/unit/path/to/relevant_tests.py -v --tb=short
   ```

4. **Run final validation**

   ```bash
   ./fast-automation.sh
   ```

5. **Commit your changes**

   ```bash
   git add .
   git commit -m "feat: description of change"
   # Pre-commit hooks run automatically
   ```

6. **Push to your fork**

   ```bash
   git push origin feature/your-feature-name
   ```

7. **Open a Pull Request**
   - Describe what you changed and why
   - Reference any related issues
   - Ensure CI checks pass

### Commit Messages

Use clear, descriptive commit messages:

```
feat: add semantic search caching
fix: resolve SCIP index corruption on Windows
docs: update installation guide for Python 3.12
refactor: simplify query parameter parsing
test: add coverage for temporal search edge cases
```

**Prefixes:**
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Test additions/changes
- `chore:` - Build/tooling changes

## Pull Request Process

1. **Ensure all checks pass**
   - Pre-commit hooks: pass
   - Tests: pass (fast-automation.sh)
   - Type checking: pass (mypy)
   - Linting: pass (ruff)

2. **Update documentation**
   - Update README.md if adding user-facing features
   - Add docstrings to new functions/classes
   - Update relevant guides in `docs/`

3. **Keep PRs focused**
   - One feature/fix per PR
   - Split large changes into smaller PRs
   - Avoid mixing refactoring with feature work

4. **Respond to feedback**
   - Address reviewer comments
   - Push additional commits to the same branch
   - Request re-review when ready

## Code Review Guidelines

When reviewing PRs:

- Check code quality and adherence to standards
- Verify tests cover new functionality
- Ensure documentation is updated
- Test locally if needed
- Be constructive and respectful

## Project Structure

```
code-indexer/
├── src/code_indexer/           # Main source code
│   ├── __init__.py             # Version definition
│   ├── cli.py                  # CLI entry point
│   ├── daemon/                 # Daemon mode implementation
│   ├── indexing/               # Indexing pipeline
│   ├── scip/                   # SCIP code intelligence
│   ├── server/                 # Multi-user server
│   │   ├── mcp/                # MCP protocol handlers
│   │   ├── multi/              # Multi-repo search
│   │   └── routers/            # REST API routers
│   ├── services/               # Core services (VoyageAI, etc.)
│   └── storage/                # Vector storage (FilesystemVectorStore)
├── tests/                      # Test suite
│   ├── unit/                   # Unit tests
│   ├── integration/            # Integration tests
│   └── e2e/                    # End-to-end tests
├── docs/                       # Documentation
├── third_party/                # Git submodules
│   └── hnswlib/                # HNSW library
├── test-fixtures/              # Test fixture submodules
│   └── multimodal-mock-repo/   # Multimodal E2E test fixtures
├── fast-automation.sh          # Fast test suite
├── full-automation.sh          # Complete test suite
├── lint.sh                     # Linting script
└── CLAUDE.md                   # Development guidelines
```

## Key Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Comprehensive development guidelines and rules |
| `README.md` | User-facing documentation |
| `CHANGELOG.md` | Version history and release notes |
| `pyproject.toml` | Project configuration and dependencies |

## Version Bumping

When bumping version, update ALL of these files:

1. `src/code_indexer/__init__.py` - Primary source of truth
2. `README.md` - Version badge
3. `CHANGELOG.md` - New version entry
4. `docs/architecture.md` - Version references
5. `docs/query-guide.md` - Version references

## Getting Help

- **Questions**: Open a [GitHub Discussion](https://github.com/LightspeedDMS/code-indexer/discussions)
- **Bugs**: Report via [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- **Features**: Suggest via [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- **Development Guidelines**: See `CLAUDE.md` for comprehensive rules

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

**Thank you for contributing to CIDX!**
