"""Story #1787 (S2 amendment, AC12-AC17): memory-governor integration for
the X-Ray whole-repository graph build.

See docs/adr/ADR-003-graph-memory-governor-integration.md for the
architectural decisions this package implements around. No new
`MemoryGovernor` API is added anywhere in this package -- every admission
decision goes through the EXISTING `admission_allowed()`/`.band`/
`attach_cache()` surface in `server.services.memory_governor`.
"""
