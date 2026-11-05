# cidx-testkit

Development-only test kit for the code-indexer seams. Depends on `cidx-sdk`
and pytest; never a runtime dependency.

| Module | What it gives |
|---|---|
| `cidx_testkit.registry` | Registers the entry points declared by in-repo distributions (`pyproject.toml`), so a source-tree test run resolves seams exactly as an install would |
| `cidx_testkit.conformance.<seam>` | Per-seam conformance suites (content, content writer, git, git invoker, ripgrep, index store, clone, SCIP, X-Ray, CLI invoker): subclass the contract class and supply the implementation under test |
| `cidx_testkit.fakes` | Fakes of the remote services: workspace-mcp, git-mcp and sandbox-mcp in-process, the agent orchestrator as a loopback HTTP server |
| `cidx_testkit.guards` | Static guards (content-seam inventory, git-seam AST check, index-store ratchet) parameterised by the source tree they scan |

See `docs/architecture/seams.md` in the code-indexer repository.
