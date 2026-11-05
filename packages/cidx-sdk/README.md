# cidx-sdk

Interfaces between the code-indexer core and its add-ons: the `Feature`
contract, the `FeatureContext` capability names and their Protocols
(`cidx_sdk.capability_types`), the request-state contract for feature routes
and pages (`cidx_sdk.web`), entry-point group names and the SDK version
check. No runtime dependencies.

Seam Protocols, DTOs, errors and the seam resolver are in `cidx_sdk.seams`
(one module per seam).

An add-on depends on `cidx-sdk` only, never on `code-indexer`. See
`docs/architecture/feature-sdk.md` in the code-indexer repository for how the
core loads features and what it does with each hook.
