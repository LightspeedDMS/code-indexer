# Package Dependency Contracts and Catalog Guards

Two CI checks that keep the modular-packaging plan ([end state](../../../plans/designs/modular-endstate.md),
[migration phases](../../../plans/designs/modular-packaging-addons.md)) from sliding backwards while the code moves.
Both are ratchets: today's tree does not meet the rule, the known shortfall is committed, and the shortfall may only
shrink.

## Import contracts

The rules live in `.importlinter` (import-linter's own format). `scripts/check_import_contracts.py` evaluates them
with import-linter, pinned in `pyproject.toml` (dev extra), `.gitlab-ci.yml` (`lint`) and `.pre-commit-config.yaml`,
and compares the result with `scripts/import_contracts_ratchet.json`.

| Contract | Rule | Why |
|----------|------|-----|
| `library-packages-no-server` | `global_repos`, `storage`, `xray`, `scip` do not import `code_indexer.server` | End-state target: zero. These cannot leave the kernel or become tool modules while they reach into the server |
| `core-libraries-no-server` | every other non-shell top-level package (`services`, `utils`, `indexing`, `backends`, `api_clients`, ...) does not import `code_indexer.server` | The server is the kernel's HTTP shell; a library importing it inverts the layering |
| `features-not-imported` | nothing in the core imports `code_indexer.scip`, SCIP's plugin-API compatibility package (the feature itself is `cidx_scip` since phase 4d) | The kernel never imports a tool module. Baseline: zero (was 74 lines before SCIP left the core) |
| `xray-feature-no-core` | the X-Ray feature distribution (`cidx_xray`, `packages/features/cidx-xray`) does not import `code_indexer` | Features depend on `cidx-sdk` only. Baseline: the one adapter, `cidx_xray._platform` ([cidx-xray.md](../cidx-xray.md)) |
| `core-does-not-import-xray-feature` | `code_indexer` does not import `cidx_xray` | The kernel never imports a feature. Baseline: zero since phase 4c |
| `scip-feature-no-core` | the SCIP feature distribution (`cidx_scip`, `packages/features/cidx-scip`) does not import `code_indexer` | Features depend on `cidx-sdk` only. Baseline: the one adapter, `cidx_scip._platform` ([cidx-scip.md](../cidx-scip.md)) |
| `core-does-not-import-scip-feature` | `code_indexer` does not import `cidx_scip` | The kernel never imports a feature: SCIP is loaded through its hooks only. Baseline: zero |
| `wiki-feature-no-core` | the wiki feature distribution (`cidx_wiki`, `packages/features/cidx-wiki`) does not import `code_indexer` | Baseline: the one adapter, `cidx_wiki._platform` ([cidx-wiki.md](../cidx-wiki.md)) |
| `core-does-not-import-wiki-feature` | `code_indexer` does not import `cidx_wiki` | Baseline: 0 |
| `memory-feature-no-core` | the memory feature distribution (`cidx_memory`, `packages/features/cidx-memory`) does not import `code_indexer` | Baseline: the one adapter, `cidx_memory._platform` ([cidx-memory.md](../cidx-memory.md)) |
| `core-does-not-import-memory-feature` | `code_indexer` does not import `cidx_memory` | Baseline: 0 |
| `depmap-feature-no-core` | the dependency-map feature distribution (`cidx_depmap`, `packages/features/cidx-depmap`) does not import `code_indexer` | Baseline: the one adapter, `cidx_depmap._platform` ([cidx-depmap.md](../cidx-depmap.md)) |
| `core-does-not-import-depmap-feature` | `code_indexer` does not import `cidx_depmap` | Baseline: 0 |
| `wiki-feature-no-other-feature`, `xray-feature-no-other-feature`, `scip-feature-no-other-feature`, `memory-feature-no-other-feature`, `depmap-feature-no-other-feature` | a feature does not import another feature | Baseline: zero. What they share becomes an SDK capability |
| `plugins-no-kernel` | `plugins/cidx_git_backend` and `plugins/cidx_orchestrator_invoker` do not import `code_indexer` | They are the future `cidx-delegate-*` distributions, which depend on `cidx-sdk` only. Baseline: zero |
| `backends-no-kernel` | the local backends (`cidx_backend_workspace`, `cidx_backend_git`) and the test kit (`cidx_testkit`) do not import `code_indexer` | Backends depend on `cidx-sdk` only; a core service they need is handed to their factory, typed by an SDK Protocol ([seams.md](../seams.md)). Baseline: zero |
| `core-does-not-import-backends` | `code_indexer` does not import `cidx_backend_workspace` or `cidx_backend_git` | The core reaches a local backend only through the seam resolver, so a pod that delegates a seam can leave the module out. Baseline: the six compatibility re-exports at the old import paths |

How violations are counted:

- One violation per import statement line, keyed `importer -> imported`. A second import of the same module on another
  line is a new violation.
- Function-level imports count, as do imports under `TYPE_CHECKING`, relative imports and `from pkg import submodule`
  (import-linter parses every import statement in a module).
- Dynamic imports by string literal (`importlib.import_module("...")`, `__import__("...")`) are invisible to
  import-linter; the script finds them by AST and keys them `importer -> imported (dynamic)`.
- Only direct imports count (`allow_indirect_imports = True`). Indirect chains become worth checking once the direct
  counts reach zero.
- Every top-level module of `code_indexer` must be a source of a "no server" contract or listed in `SHELL_MODULES`
  (`cli*`, `daemon`, `server`, the CLI front-end helpers). A new package cannot escape the rule by not being listed.

### Updating

- A new forbidden import fails. Move the code, or depend on an interface instead. Do not add it to the baseline.
- Removing an import also fails until the baseline is lowered, so the freed headroom cannot be reused:
  `python3 scripts/check_import_contracts.py --write`, then commit the JSON with the change.
- An upstream snapshot merge that adds imports must lower or raise the baseline in the same merge, with the growth
  named in the merge description.

Gates: `tests/unit/ci/test_import_contracts.py` (smoke job; also proves the function-level and dynamic cases), the
`lint` job, `./lint.sh` and the pre-commit hook.

## Catalog access guards

A restricted caller sees a filtered projection of the internal catalog. The projection is applied by one family of
guards in `server/services/cidx_meta_access/file_doors.py` (`judge_file_door`, `judge_app_request`, `view_for`,
`served_file_payload`, `refuse_hidden_catalog_writes`, the view's `lines_view` and `served_text`) and by
`restricted_catalog_view` in `server/services/cidx_meta_access/catalog_wiki.py`, which the core's `access_projection`
capability (`server/features/access_projection.py`) binds for the wiki feature. Every door that serves the catalog
must call its guard; a door that stops calling it keeps working, so tests of its normal behaviour do not notice.
The wiki's doors are in the cidx-wiki distribution (`packages/features/cidx-wiki/.../server/routes.py`, `catalog_view.py`,
`mcp/analytics.py`); their requirements are named from the repository root. Memory files (`memories/<id>.md`)
are decided by the access-filtering service's memory scope rule (`filter_visible_memory_ids`), which the same
capability binds as `visible_memory_ids`; the memory feature's search section
(`packages/features/cidx-memory/.../server/retrieval.py`) asks it before any memory body is read. The dependency
map's MCP tools and web views (`packages/features/cidx-depmap/.../server/mcp/handlers.py`, `web/caller_view.py`)
ask its `dependency_map_access`, which binds `resolve_meta_visibility` + `source_for_visibility`.

`tests/unit/ci/test_catalog_guard_presence.py` parses the door modules (file service listing and reads, file writes,
MCP file tools, REST file and repository routes, repository description, wiki pages, assets and search, the
memory search section) and requires
each guard call, keyed on the enclosing function. The required set is the upstream v12.93.0 wiring.

`tests/unit/ci/catalog_guard_known_gaps.json` lists requirements this tree does not meet yet. A listed gap that is now
met fails until its entry is removed; an unlisted unmet requirement fails as a removed guard. The list only shrinks.
It is empty since the v12.93.0 snapshot landed (all 39 requirements met).
If a door is renamed or moved, update `REQUIRED` in the test in the same change.
