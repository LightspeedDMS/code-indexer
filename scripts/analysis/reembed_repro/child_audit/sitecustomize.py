"""Network audit for harness-spawned ``cidx`` children (read-only observer).

Loaded automatically by the interpreter because the harness puts this
directory first on the child's PYTHONPATH. It registers a PEP 578 audit
hook and appends a JSON line to ``$REEMBED_AUDIT_LOG`` for every outbound
connect to a non-loopback address and every DNS lookup for a host other
than localhost or the provider host the sandbox maps to the local fake. The
harness fails when this log is non-empty. Inside the sandbox such attempts
also fail at the OS level (no route, no DNS); this log makes them visible.

Allowed traffic is never touched. For a disallowed event, a failure to write
the log propagates out of the hook, which aborts that (already disallowed)
operation loudly rather than losing the record.
"""

import json
import os
import sys

_LOG = os.environ.get("REEMBED_AUDIT_LOG")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ALLOWED_LOOKUPS = _LOOPBACK | {"api.voyageai.com", "", None}


def _append(event, target):
    line = json.dumps({"event": event, "target": target, "pid": os.getpid()})
    with open(_LOG, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _hook(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and str(address[0]) not in _LOOPBACK:
            _append(event, str(address[0]))
    elif event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in _ALLOWED_LOOKUPS:
            _append(event, str(host))


if _LOG:
    sys.addaudithook(_hook)
