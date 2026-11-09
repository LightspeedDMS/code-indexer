# cidx-depmap

The dependency map -- cross-repository domains analysed by an LLM from the
golden repositories, kept in the catalog (`cidx-meta/dependency-map/`) with
MCP tools (`depmap_*`, `trigger_dependency_analysis`) and admin pages --
packaged as a code-indexer feature add-on (import name `cidx_depmap`,
feature name `depmap`). It registers on the `code_indexer.features`
entry-point group and depends on `cidx-sdk` and third-party packages only
(plus the one transitional adapter, `cidx_depmap/_platform.py`).

The server serves it when the distribution is installed and `"depmap"` is in
the bootstrap key `enabled_features`. See `docs/architecture/cidx-depmap.md`
in the code-indexer repository.
