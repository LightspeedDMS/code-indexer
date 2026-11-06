"""Kernel interfaces that library and feature packages depend on instead of the server.

Nothing in this package imports ``code_indexer.server`` or a third-party
package. ``seam_types`` re-exports seam Protocols and DTOs, which now live in
``cidx_sdk.seams``; ``capabilities`` holds
what the platform offers a feature (configuration, telemetry, the content
seam, ...), each installed by the server at its wiring layer
(``server/kernel_wiring.py``). ``capabilities`` is written to move into
``cidx-sdk`` unchanged (plans/designs/modular-phase2-inventory.md).
"""
