"""Story #1787 (S2 amendment, AC12-AC17): memory-governor integration for
the X-Ray whole-repository graph build.

See docs/adr/ADR-003-graph-memory-governor-integration.md for the
architectural decisions this package implements around. No new
`MemoryGovernor` API is added anywhere in this package -- every admission
decision goes through the EXISTING `admission_allowed()`/`.band`/
`attach_cache()` surface of the core's memory governor.

Part of the cidx-xray feature since modular plan phase 4c: the feature's
on_startup attaches the wire-file cache (`cache_proxy`) to the governor
through the server platform and installs the K provider
(`k_calibration_store`) over the `storage` capability.
"""
