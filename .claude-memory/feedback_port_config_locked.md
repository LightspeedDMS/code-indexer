---
name: Port configuration is locked — never change
description: NEVER change port config for cidx-server, HAProxy, or firewall — causes HAProxy 503 errors
type: feedback
---

NEVER change port configuration for cidx-server, HAProxy, or firewall.

Locked config (verified 2025-11-30):
- cidx-server systemd: port 8000
- HAProxy backend: forwards to staging on port 8000
- Firewall: allows 8000 from HAProxy

Any port change = HAProxy 503 errors.

**Why:** Changing any port in the chain breaks the HAProxy-to-cidx routing. The three components (systemd, HAProxy, firewall) must all agree on port 8000.

**How to apply:** If anyone asks to change ports, refuse. The port configuration is permanently locked. For server IPs and topology details, read `.local-testing`.
