# cidx-xray

X-Ray, precision AST-aware code search, packaged as a code-indexer feature
add-on (import name `cidx_xray`, feature name `xray`). It registers on the
`code_indexer.features` entry-point group and depends on `cidx-sdk` and
third-party packages only.

The native engine (`xray-cli`, built from the repository's `rust/` workspace)
is not part of this wheel; see `docs/architecture/cidx-xray.md` in the
code-indexer repository for how it is built and located.
