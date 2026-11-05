# cidx-wiki

The wiki -- a repository's markdown served as browsable, searchable articles
under `/wiki/` -- packaged as a code-indexer feature add-on (import name
`cidx_wiki`, feature name `wiki`). It registers on the `code_indexer.features`
entry-point group and depends on `cidx-sdk` and third-party packages only
(plus the one transitional adapter, `cidx_wiki/_platform.py`).

The server serves it when the distribution is installed and `"wiki"` is in
the bootstrap key `enabled_features`. See `docs/architecture/cidx-wiki.md` in
the code-indexer repository.
