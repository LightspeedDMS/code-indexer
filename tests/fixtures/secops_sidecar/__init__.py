"""Mock Google SecOps (Chronicle) receiver sidecar -- test support only.

A REAL local HTTP service, run as its own process on two loopback-only ports,
that stands in for Chronicle's ``events:import`` API and Google's OAuth
service-account token endpoint.  It enforces the documented request contract,
records everything it receives, and can be scripted to fail in specific ways
through its control API.  Nothing under ``src/`` imports it; it is never
shipped.

Start it with ``python3 -m tests.fixtures.secops_sidecar --help`` or through
``harness.start_sidecar``.  Storage is in memory only.
"""
