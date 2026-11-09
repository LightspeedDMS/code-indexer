# cidx-memory

Shared technical memories -- short, evidence-backed facts stored as
`cidx-meta/memories/<id>.md` and offered next to code search results --
packaged as a code-indexer feature add-on (import name `cidx_memory`, feature
name `memory`). It registers on the `code_indexer.features` entry-point group
and depends on `cidx-sdk` and third-party packages only (plus the one
transitional adapter, `cidx_memory/_platform.py`).

The server serves it when the distribution is installed and `"memory"` is in
the bootstrap key `enabled_features`. See `docs/architecture/cidx-memory.md`
in the code-indexer repository.
