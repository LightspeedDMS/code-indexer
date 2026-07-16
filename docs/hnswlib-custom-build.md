# Custom hnswlib Build

## Overview

Code-indexer uses a **custom build of hnswlib** from the `third_party/hnswlib` git submodule instead of the PyPI package. This custom build includes the `check_integrity()` method required for HNSW index integrity validation, and the `repair_orphans()` method (Story #1358 / Epic #1333) for deterministic repair of zero-inbound ("orphan") HNSW nodes.

## Why Custom Build?

The PyPI version of hnswlib (v0.8.0) does not expose the `check_integrity()` or `repair_orphans()` methods in Python bindings. These are essential for:

- Validating HNSW index integrity before queries
- Detecting corrupted index files
- Providing better error messages when indexes are broken
- Deterministically repairing orphan nodes (unreachable elements with zero inbound graph connections) so they stop silently missing from k-NN search results
- Supporting future integrity-checking features

## Installation

### Prerequisites

1. Git submodule must be initialized
2. Python 3.9 or higher
3. C++ compiler (for building hnswlib native extension)

### Steps

```bash
# 1. Clone code-indexer (if not already cloned)
git clone https://github.com/YOUR_USERNAME/code-indexer.git
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
from code_indexer.services.hnsw_health_service import HnswHealthService

try:
    HnswHealthService().check_integrity()
    print("Custom hnswlib build verified successfully!")
except (ImportError, AttributeError) as e:
    print(f"Verification failed: {e}")
```

## Build Configuration

### pyproject.toml (the actual install mechanism -- Story #54)

The `dependencies` list in `pyproject.toml` does **not** use PyPI `hnswlib>=0.8.0`. Instead it pins a direct PEP 508 git dependency to a specific commit on the fork:

```toml
dependencies = [
    # ... other dependencies ...
    # Custom hnswlib fork (Story #54) — PyPI version lacks check_integrity().
    # Also carries repair_orphans() (Story #1358 / Epic #1333) — deterministic
    # HNSW orphan repair. Pinned to same commit as third_party/hnswlib
    # submodule. Requires gcc/g++ at install time.
    "hnswlib @ git+https://github.com/LightspeedDMS/hnswlib.git@<commit-sha>",
]
```

This is the mechanism pip actually resolves on any `pip install .` / `pip install -e .` — the pinned commit SHA here MUST always match the `third_party/hnswlib` submodule pointer (see "Custom Commit" below); bumping one without the other leaves the change inert for real installs.

### setup.py (legacy submodule-local build path)

`setup.py` additionally defines custom commands (`CustomDevelopCommand`, `CustomInstallCommand`) that build hnswlib directly from the local `third_party/hnswlib` submodule checkout. This is useful for iterating on fork changes locally (rebuild via `cd third_party/hnswlib && pip install --force-reinstall --no-deps .`) before pushing, but the pyproject.toml git dependency above is what a normal `pip install -e .` on this project actually resolves from.

## Submodule Details

### Location

`third_party/hnswlib/`

### Custom Commit

The submodule points to commit `878cfbe5` (`878cfbe585395a8bdd95f593d071f778d2fac457`), which includes fork patches on top of upstream (descended from the earlier `57e9453`/`8972063` patches):

```
8972063 feat: Expose checkIntegrity() method to Python bindings
57e9453 feat: Add repair_orphans() method to Python bindings for deterministic HNSW orphan repair
```

`8972063` adds `check_integrity()`; `57e9453` adds `repair_orphans()` (Story #1358 / Epic #1333) — both Python bindings not present in the upstream PyPI release. This commit MUST always match `pyproject.toml`'s `hnswlib @ git+...@<commit-sha>` dependency pin (see "pyproject.toml" above) and the informational `EXPECTED_HNSWLIB_FORK_COMMIT` constant in `src/code_indexer/storage/hnsw_index_manager.py` (Bug #1392) — keep all three in sync manually.

### Verifying Submodule

```bash
cd third_party/hnswlib
git log -1 --oneline
git rev-parse HEAD
# HEAD should be 878cfbe585395a8bdd95f593d071f778d2fac457
```

## Troubleshooting

### Fleet-wide drift between the server and CLI Python environments (Bug #1392)

Production incident: the custom fork was only ever built into the SERVER's own
Python environment by the auto-updater (`deployment_executor.py`'s
`_build_hnswlib_with_fallback()`/`build_custom_hnswlib()`). Real `cidx` CLI
indexing subprocesses run under a SEPARATE, system-wide Python environment
that received no equivalent build step, so it could silently drift to a
stock PyPI `hnswlib` (missing `check_integrity()`/`repair_orphans()`),
causing every finalize-time orphan detect+repair call to fail with a bare
`AttributeError` fleet-wide.

Two fixes now catch this automatically:

- **Deploy pipeline coverage**: `DeploymentExecutor._ensure_cli_hnswlib_capability()`
  resolves the CLI's own interpreter (via the `cidx` console-script's
  shebang, never a hardcoded path) and syncs the fork into it as a new,
  non-fatal deploy step (Step 1.7) alongside the server's own build (Step
  1.6). This should self-heal drift on the next deploy.
- **Graceful degrade + explicit capability signal (Bug #1415)**: Bug #1392's
  original fix made every build/finalize entry point raise a dedicated
  `HNSWCapabilityError` immediately when the fork is missing -- but that
  still aborted the ENTIRE indexing operation (a fleet-wide outage on
  2026-07-14 discarded already-computed embedding spend for ~12 golden
  repos). The design was reversed:
  - CLI side (`storage/hnsw_index_manager.py`): `build_index()`,
    `rebuild_from_vectors()`, and `save_incremental_update()` no longer gate
    on capability at all. `_detect_and_repair_orphans()` -- the single place
    that actually calls `check_integrity()`/`repair_orphans()` -- checks
    capability first; if missing, it logs ONE WARNING and skips the orphan
    hardening pass, and the caller proceeds to persist a valid, correct
    index (orphan repair is a hardening layer, not correctness of the
    vectors themselves). Query-only paths (`index_exists()`, `is_stale()`,
    `query()`, `load_index()`, `__init__`) remain NEVER gated, per the
    "Query Is Everything" invariant.
  - Server side (`server/services/hnswlib_capability_check.py`): unchanged
    from Bug #1392 -- a startup check logs a loud, actionable ERROR (naming
    the interpreter, expected commit, and this doc) but NEVER blocks server
    startup.
  - Health surface (`cidx health` / MCP `check_hnsw_health` / REST / Web):
    exposes a new, SEPARATE `hnswlib_capability_available` field (True/False/
    not-evaluated) distinct from the zero-tolerance `orphan_count` signal
    (which stays exactly 0=OK, >0=ERROR, no WARNING tier) -- so a node
    running stock hnswlib is visibly flagged as degraded rather than either
    crashing or silently reporting a false-clean/false-corrupt result.

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
git checkout 878cfbe585395a8bdd95f593d071f778d2fac457  # The custom commit (repair_orphans + checkIntegrity)
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

### Using repair_orphans()

When `check_integrity()` reports orphan errors ("Element N has no inbound connections (orphan)"), `repair_orphans()` deterministically repairs them in place:

```python
import hnswlib

index = hnswlib.Index(space='cosine', dim=1024)
index.load_index('path/to/index.bin')

result = index.check_integrity()
if not result["valid"]:
    repair_result = index.repair_orphans()
    print(repair_result)
    # {'orphans_before': 12, 'orphans_after': 0, 'repaired_count': 12,
    #  'passes_used': 1, 'forced_evictions': 8, 'valid': True}
    assert index.check_integrity()["valid"]

    # Persist the repair — repair_orphans() mutates the in-memory graph
    # only; production code must save_index() so the fix survives the
    # next load_index() (see tests/unit/hnsw_orphan_repair/ AC5 for the
    # full on-disk round-trip proof).
    index.save_index('path/to/index.bin')
```

Idempotent (safe to call on an already-clean index) and bounded (at most `cur_element_count + 1` repair passes). See Story #1358 (Epic #1333) and `docs/research/hnsw-temporal-orphans-1330.md` for the two orphan-producing regimes this repairs (near-tie deterministic, exact-tie race).

### Testing

Unit tests for the HNSW health service:
```bash
pytest tests/unit/services/test_hnsw_health_service.py -v
```

## CI/CD Considerations

### GitHub Actions

In CI workflows, ensure submodule initialization:

```yaml
- name: Checkout code with submodules
  uses: actions/checkout@v4
  with:
    submodules: recursive

- name: Install dependencies
  run: pip install -e .
```

### Docker Builds

In Dockerfiles:

```dockerfile
# Clone with submodules
RUN git clone --recurse-submodules https://github.com/YOUR_USERNAME/code-indexer.git

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
   from code_indexer.services.hnsw_health_service import HnswHealthService
   HnswHealthService().check_integrity()
   ```

## References

- hnswlib GitHub: https://github.com/nmslib/hnswlib
- Custom commits: 8972063 (checkIntegrity method), 57e9453 (repair_orphans method, Story #1358 / Epic #1333); current submodule/pyproject.toml pin: 878cfbe585395a8bdd95f593d071f778d2fac457
- Orphan repair background: `docs/research/hnsw-temporal-orphans-1330.md`
