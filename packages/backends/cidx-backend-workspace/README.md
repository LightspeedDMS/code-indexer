# cidx-backend-workspace

Local implementations of the seams that the remote workspace service
(workspace-mcp) serves when delegated. Each is a provider registered under the
name of the mode it implements and reached only through the seam resolver:

| Seam | Entry point group | Entry names | Module |
|---|---|---|---|
| content | `code_indexer.content_backend` | `local` | `content.LocalContentBackend` |
| content writer | `code_indexer.content_writer` | `local` | `content_writer.LocalContentWriter` |
| ripgrep | `code_indexer.ripgrep_backend` | `local` | `ripgrep.LocalRipgrepBackend` (runs through the core's bounded runner) |
| index store | `code_indexer.index_artifacts` | `local` | `index_store.LocalIndexStore` |
| clone | `code_indexer.clone_backend` | `local`, `ontap`, `cow-daemon` | `clone` |

A clone provider takes `cidx_sdk.seams.clone.CloneBackendSettings`: the mode's
config section and the core runtime it runs on (cancellable process runner,
credential redaction, NFS visibility barrier). ONTAP and the CoW daemon stay
in this distribution until a deployment needs one without the other.

Depends on `cidx-sdk` (and `requests`, for the ONTAP and CoW-daemon REST
clients) only. Its tests run the seams' conformance suites from
`cidx-testkit`.
