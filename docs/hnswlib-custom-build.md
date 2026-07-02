# Custom hnswlib Build

## Overview

Code-indexer uses a **custom build of hnswlib** from the `third_party/hnswlib` git submodule instead of the PyPI package. This custom build includes the `check_integrity()` method required for HNSW index integrity validation.

## Why Custom Build?

The PyPI version of hnswlib (v0.8.0) does not expose the `check_integrity()` method in Python bindings. This method is essential for:

- Validating HNSW index integrity before queries
- Detecting corrupted index files
- Providing better error messages when indexes are broken
- Supporting future integrity-checking features

## Installation

### Prerequisites

1. Git submodule must be initialized
2. Python 3.9 or higher
3. C++ compiler (for building hnswlib native extension)

### Steps

```bash
# 1. Clone code-indexer (if not already cloned)
git clone https://github.com/LightspeedDMS/code-indexer.git
cd code-indexer

# 2. Initialize hnswlib submodule
git submodule update --init

# 3. Install in development mode (builds hnswlib from submodule)
pip install -e .
```

The custom `setup.py` automatically:
- Checks that the submodule is initialized
- Builds hnswlib from `third_party/hnswlib`
- Installs the custom build with `check_integrity()` method

### Verification

To verify the custom build is installed correctly:

```python
from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib

try:
    verify_custom_hnswlib()
    print("Custom hnswlib build verified successfully!")
except (ImportError, AttributeError) as e:
    print(f"Verification failed: {e}")
```

## Build Configuration

### pyproject.toml

The `dependencies` list in `pyproject.toml` **does not include** `hnswlib>=0.8.0`. Instead, there's a comment:

```toml
dependencies = [
    # ... other dependencies ...
    # hnswlib is built from third_party/hnswlib submodule (Story #54)
    # DO NOT use PyPI hnswlib - it lacks check_integrity() method
    # Install: git submodule update --init && pip install -e .
]
```

### setup.py

The `setup.py` defines custom build commands:

- `CustomDevelopCommand`: Builds hnswlib from submodule during `pip install -e .`
- `CustomInstallCommand`: Builds hnswlib from submodule during `pip install .`

Both commands:
1. Verify submodule exists and is initialized
2. Run hnswlib's `setup.py` from `third_party/hnswlib`
3. Install the custom build

## Submodule Details

### Location

`third_party/hnswlib/`

### Custom Commit

The submodule points to commit `8972063` which includes:

```
feat: Expose checkIntegrity() method to Python bindings
```

This commit adds the `check_integrity()` method to the Python bindings that is not present in the upstream PyPI release.

### Verifying Submodule

```bash
cd third_party/hnswlib
git log -1 --oneline
# Should show: 8972063 feat: Expose checkIntegrity() method to Python bindings
```

## Troubleshooting

### Error: "hnswlib is not installed"

**Cause**: Submodule not initialized or build failed.

**Solution**:
```bash
git submodule update --init
pip uninstall hnswlib  # Remove any PyPI version
pip install -e .       # Rebuild from submodule
```

### Error: "hnswlib.Index does not have check_integrity() method"

**Cause**: Using PyPI hnswlib instead of custom build.

**Solution**:
```bash
pip uninstall hnswlib
git submodule update --init
pip install -e .
```

### Error: "third_party/hnswlib submodule not found"

**Cause**: Submodule not initialized.

**Solution**:
```bash
git submodule update --init
```

### Verification Fails in Tests

If integration tests fail with "Submodule not on custom commit":

```bash
cd third_party/hnswlib
git fetch origin
git checkout 8972063  # The custom commit
cd ../..
git add third_party/hnswlib
```

## Development Workflow

### Adding check_integrity() Calls

When adding new code that validates HNSW indexes:

```python
import hnswlib

# Create or load index
index = hnswlib.Index(space='l2', dim=128)
index.load_index('path/to/index.bin')

# Validate integrity before using
if not index.check_integrity():
    raise RuntimeError("HNSW index is corrupted")

# Safe to use index
results = index.knn_query(query_vector, k=10)
```

### Testing

Unit tests for the verification utility:
```bash
pytest tests/unit/utils/test_hnswlib_verification.py -v
```

Integration tests for submodule build:
```bash
pytest tests/integration/test_hnswlib_submodule_build.py -v
```

## CI/CD Considerations

### GitHub Actions

In CI workflows, ensure submodule initialization:

```yaml
- name: Checkout code with submodules
  uses: actions/checkout@v3
  with:
    submodules: recursive

- name: Install dependencies
  run: pip install -e .
```

### Docker Builds

In Dockerfiles:

```dockerfile
# Clone with submodules
RUN git clone --recurse-submodules https://github.com/LightspeedDMS/code-indexer.git

# Or initialize after clone
WORKDIR /app
RUN git submodule update --init

# Install (builds hnswlib from submodule)
RUN pip install -e .
```

## Migration from PyPI hnswlib

If upgrading from a version that used PyPI hnswlib:

1. Uninstall PyPI version:
   ```bash
   pip uninstall hnswlib
   ```

2. Initialize submodule:
   ```bash
   git submodule update --init
   ```

3. Reinstall code-indexer:
   ```bash
   pip install -e .
   ```

4. Verify custom build:
   ```python
   from code_indexer.utils.hnswlib_verification import verify_custom_hnswlib
   verify_custom_hnswlib()
   ```

## References

- Story #54: Replace PyPI hnswlib with custom build
- hnswlib GitHub: https://github.com/nmslib/hnswlib
- Custom commit: 8972063 (checkIntegrity method)
