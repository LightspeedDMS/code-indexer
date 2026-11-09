# cidx-scip

SCIP code intelligence (symbol definitions, references, dependencies, impact,
call chains, smart context), packaged as a code-indexer feature add-on
(import name `cidx_scip`, feature name `scip`). It registers on the
`code_indexer.features` entry-point group and depends on `cidx-sdk` and
third-party packages only.

The per-language SCIP indexers (`scip-python`, `scip-typescript`, ...) are
not part of this wheel; see `docs/architecture/cidx-scip.md` in the
code-indexer repository for how the server builds SCIP indexes.
