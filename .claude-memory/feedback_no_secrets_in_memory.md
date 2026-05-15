---
name: No secrets, IPs, or topology in memory files
description: Memory files are versioned — NEVER write IPs, secrets, credentials, hostnames, DSNs, or topology details into them
type: feedback
originSessionId: d05fe9e8-c6f0-4e53-86ac-410b98b678ba
---
Memory files must NEVER contain IPs, secrets, credentials, hostnames, DSNs, port numbers, node IDs, or any information that could reveal infrastructure topology. Not even partial IPs or obfuscated versions.

**Why:** Memory files are versioned and committed to git. Infrastructure topology and credentials are sensitive. The user was emphatic: "nothing that can be used to learn the topology of this infrastructure."

**How to apply:** All infrastructure details (IPs, credentials, topology, node mappings) belong ONLY in `.local-testing` (gitignored). Memory files may reference `.local-testing` as the source (e.g., "see `.local-testing` section 1b for cluster topology") but must never duplicate the actual values. This applies to all memory types: project, feedback, reference, user.
