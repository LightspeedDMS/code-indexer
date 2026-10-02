"""Runtime configuration of SIEM delivery (Web UI Config section ``siem_delivery``).

Holds references only: a SecOps region and path segments -- never key
material or a token.  The service-account key is configured separately in
the same Web UI section and stored encrypted in the database
(``services/siem_delivery/credential.py``); there is no key-file path.
Validation lives in ``services/siem_delivery/destination.py``.
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
    max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS
    source_instance_label: str = ""
    # Loopback test receiver origin; accepted ONLY in a process whose
    # non-production fault-injection harness passed its startup gate.
    harness_endpoint: str = ""
    # Optional additional trusted CA certificates (PEM, public) ADDED to the
    # default trust of both outbound legs; set, replaced and removed only
    # through the elevated CA form (services/siem_delivery/trust.py), never
    # the generic section form.  The fingerprint (SHA-256 over the DER of
    # every certificate) is what the config-change audit records.
    trusted_ca_pem: str = ""
    trusted_ca_fingerprint: str = ""
