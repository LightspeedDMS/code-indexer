"""Log-audit entries allowed ONLY for the Phase 7 (SIEM delivery) audits.

Passed per call as ``extra_allowlist`` by the Phase 7 conftest; they are
never part of the global ``LOG_AUDIT_ALLOWLIST``, so no other phase can hide
behind them.
"""

from __future__ import annotations

from typing import Tuple

PHASE7_LOG_ALLOWLIST: Tuple[str, ...] = (
    # The Phase 7 servers run with the non-production fault-injection gate ON
    # (the only way to target the mock receiver).  Every gated boot logs this
    # fixed banner at WARNING (fault_injection/startup.py); test_04 restarts
    # its own server after its watermark.  A gate REFUSAL logs CRITICAL with
    # different text, so it is not hidden.
    "FAULT INJECTION HARNESS ACTIVE (non-prod mode)",
    # The scenarios script the mock receiver to answer 409 ALREADY_EXISTS
    # (test_05 hold, test_07), 401 UNAUTHENTICATED (test_06) and an unindexed
    # 400 for one poison row (test_05).  SIEM delivery logs each resulting
    # halt / quarantine at WARNING -- the asserted signals.  Each entry is
    # anchored on the full sanitised signature of the scripted reply, so a
    # halt of any other class, status or path is NOT suppressed.
    "SIEM delivery halted: class=duplicate_response "
    "signature=409|ALREADY_EXISTS|duplicate_response|unindexed",
    "SIEM delivery halted: class=credential "
    "signature=401|UNAUTHENTICATED|credential|unindexed",
    "SIEM delivery quarantined 1 event(s): reason=row_rejection "
    "signature=400|INVALID_ARGUMENT|row_rejection|unindexed",
)
