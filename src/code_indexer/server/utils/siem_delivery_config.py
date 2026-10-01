"""Runtime configuration of SIEM delivery (Web UI Config section ``siem_delivery``).

Holds references only: a SecOps region, path segments, and the PATH of a
service-account key file -- never key material or a token.  Validation lives
in ``services/siem_delivery/destination.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_BATCH_EVENTS = 1000


@dataclass
class SiemDeliveryConfig:
    # Capture switch.  Delivery of already-captured rows continues while
    # a destination is configured, whatever this says.
    enabled: bool = False
    # One of SECOPS_REGIONS (ignored when harness_endpoint is set).
    region: str = ""
    api_version: str = "v1"
    project_id: str = ""
    location: str = ""
    instance_id: str = ""
    # Absolute path of a service-account key file present on every node.
    service_account_key_path: str = ""
    max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS
    source_instance_label: str = ""
    # Loopback test receiver origin; accepted ONLY in a process whose
    # non-production fault-injection harness passed its startup gate.
    harness_endpoint: str = ""
