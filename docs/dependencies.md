# Dependency Management

All dependencies are defined in `pyproject.toml`, which is the single source of truth for this project.

## For Users

Install the package and its core dependencies:

```bash
pip install git+https://github.com/LightspeedDMS/code-indexer.git
```

### Optional extras

The following optional extras are available:

```bash
# Cohere embedding provider support (alternative to VoyageAI)
pip install "git+https://github.com/LightspeedDMS/code-indexer.git[cohere]"

# PostgreSQL cluster mode (required for multi-node deployments)
pip install "git+https://github.com/LightspeedDMS/code-indexer.git[cluster]"

# Both optional providers
pip install "git+https://github.com/LightspeedDMS/code-indexer.git[cohere,cluster]"
```

## For Developers

```bash
# Clone and install with all development dependencies
git clone https://github.com/LightspeedDMS/code-indexer.git
cd code-indexer
pip install -e ".[dev]"

# Full development setup including all optional extras
pip install -e ".[dev,cohere,cluster]"
```

## Files

- `pyproject.toml` - Project configuration and all dependency definitions (primary source of truth)

Note: `requirements.txt` and `requirements-dev.txt` are not used by this project. All
dependencies are declared in `pyproject.toml` under `[project.dependencies]` and
`[project.optional-dependencies]`.

## Notable dependencies

- `hnswlib` is installed from a custom fork that exposes the `check_integrity()` method absent
  from the PyPI version. This requires `gcc`/`g++` at install time. See
  `docs/hnswlib-custom-build.md` for details.
- `tree-sitter>=0.21,<0.22` and `tree-sitter-languages==1.10.2` are pinned core dependencies
  required for X-Ray AST-aware code search (included since v10.2.1).
