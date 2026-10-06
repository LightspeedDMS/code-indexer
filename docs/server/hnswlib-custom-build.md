# Custom hnswlib Build

CIDX uses a fork of hnswlib instead of the PyPI release. The fork adds two Python bindings the PyPI package lacks:

- `Index.check_integrity()`: validates the HNSW graph and reports elements with no inbound connections (orphans).
- `Index.repair_orphans()`: reconnects orphan elements in place so they are reachable by k-NN search again.

It also releases the Python GIL during native save, load and query calls. Index builds run orphan detection and
repair before persisting; health checks (`cidx health`, MCP `check_hnsw_health`) use `check_integrity()`.

This page is the procedure referenced by the server's startup check and the auto-updater when a Python
environment lacks the fork.

## How it is installed

`pyproject.toml` pins hnswlib as a git dependency on the fork:

```toml
"hnswlib @ git+https://github.com/LightspeedDMS/hnswlib.git@8155cfc9d02a5a46528a3faab697555952ed40c6",
```

Any `pip install .` or `pip install -e .` of this project resolves that pin and compiles the extension, so a C++
compiler (`gcc`/`g++`) must be present.

The same commit is recorded in two more places, and all three must match:

| Location | Value |
|----------|-------|
| `pyproject.toml` dependency pin | `8155cfc9d02a5a46528a3faab697555952ed40c6` |
| `third_party/hnswlib` submodule pointer | `git submodule status third_party/hnswlib` |
| `EXPECTED_HNSWLIB_FORK_COMMIT` in `src/code_indexer/storage/hnsw_index_manager.py` | named in warnings and errors |

The fork's commits on top of upstream, newest first (`git -C third_party/hnswlib log -5 --oneline`):

```
8155cfc feat(#1490): release GIL in remaining Index/BFIndex bindings
e03aa23 fix: release GIL during Index save_index()/load_index() native calls
878cfbe fix: Add bounds-check guards to try_connect in repairOrphans()
57e9453 feat: Add repair_orphans() method to Python bindings for deterministic HNSW orphan repair
8972063 feat: Expose checkIntegrity() method to Python bindings
```

## Two Python environments on a server

A server host can have two separate Python environments:

- The server's own interpreter (the one in the `cidx-server` unit).
- The interpreter behind the `cidx` command, which runs every indexing subprocess.

The auto-updater builds the fork into both on each deployment: into the server's interpreter (step 1.6, fatal on
failure, skipped when the submodule commit equals the last build) and into the CLI's interpreter, found through the
`cidx` script's shebang (step 1.7, non-fatal). See [Auto-Update](auto-update.md#deployment-steps).

## Check an environment

Run with the interpreter you want to check:

```bash
python3 -c "import hnswlib; print(all(hasattr(hnswlib.Index, m) for m in ('check_integrity', 'repair_orphans')))"
```

`True` means the fork is installed. From a CIDX checkout, the server's own check prints `(True, 'ok')` or the
failure message:

```bash
PYTHONPATH=src python3 -c "from code_indexer.server.services.hnswlib_capability_check import check_hnswlib_capability; print(check_hnswlib_capability())"
```

Confirm the submodule is on the pinned commit:

```bash
git -C third_party/hnswlib rev-parse HEAD
# 8155cfc9d02a5a46528a3faab697555952ed40c6
```

## Rebuild into an environment

This is the procedure the auto-updater runs (`DeploymentExecutor.build_custom_hnswlib`). From the repository
checkout, with the target interpreter as `python3` (prefix with `sudo` for a system-wide interpreter):

```bash
git submodule update --init third_party/hnswlib
python3 -m pip install --break-system-packages pybind11
cd third_party/hnswlib
python3 -m pip install --break-system-packages --force-reinstall --no-deps .
```

Then re-run the check above, and restart `cidx-server` if you rebuilt the server's interpreter.

## What happens when the fork is missing

| Where | Behaviour | Message |
|-------|-----------|---------|
| Server startup | Logs an ERROR naming the interpreter and the expected commit; startup and queries continue | `hnswlib is not installed on this Python environment ...` or `Installed hnswlib is missing check_integrity()/repair_orphans() -- this Python environment (interpreter: ...) does not have the custom hnswlib fork ...` |
| Index build or finalize | Logs one WARNING, skips orphan detection and repair, and still writes a valid index | `HNSW finalize (...): installed hnswlib lacks check_integrity()/repair_orphans() ...` |
| Auto-updater, CLI environment | Logs a WARNING (`DEPLOY-GENERAL-209`); the deployment continues | `Failed to sync custom hnswlib fork into the CLI's system-wide Python environment ...` |
| Health surfaces | `hnswlib_capability_available` is `false`, separate from `orphan_count` | - |

Queries never depend on the fork.

## Using the bindings

`check_integrity()` returns a dictionary; test its `valid` key. `repair_orphans()` changes only the in-memory
graph, so save the index afterwards:

```python
import hnswlib

index = hnswlib.Index(space="cosine", dim=1024)
index.load_index("path/to/hnsw_index.bin")

result = index.check_integrity()
# {'valid': True, 'connections_checked': ..., 'element_count': ..., 'min_inbound': ..., 'max_inbound': ..., 'errors': []}
if not result["valid"]:
    repair = index.repair_orphans()
    # {'orphans_before': ..., 'orphans_after': ..., 'repaired_count': ..., 'passes_used': ...,
    #  'forced_evictions': ..., 'valid': ...}
    index.save_index("path/to/hnsw_index.bin")
```

`repair_orphans()` is idempotent on a clean index (all counters 0). Background on the two orphan-producing regimes
it repairs: [archived research note](../archive/hnsw-temporal-orphans-1330.md).

## Changing the pinned commit

Update all three locations in the same change: move the submodule to the new commit, change the `pyproject.toml`
pin, and change `EXPECTED_HNSWLIB_FORK_COMMIT`. Changing only the submodule has no effect on installs, which
resolve the `pyproject.toml` pin.
