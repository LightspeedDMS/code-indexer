"""Structural contract for a service a delegated backend hands work to.

A delegated backend -- one that performs its work by calling out to another
service rather than running it in this process -- needs exactly two things from
configuration: where to reach that service, and how to authenticate to it.

Typing a backend against this Protocol instead of a concrete configuration class
is what keeps the backend reusable. Any object exposing ``endpoint`` and
``api_key`` satisfies it: a deployment's own config block, a test double, or a
different deployment's unrelated config type. The backend never learns which
service it is talking to, and the deployment never has to adopt someone else's
config shape to use the backend.

Deployment-specific knowledge -- which config key holds the endpoint, what the
service is called, what to tell an operator when it is missing -- belongs in the
startup wiring that resolves the config, not in the backend. See
``server/startup/scip_backend_wiring.py`` and ``clone_backend_wiring.py`` for
that layer.
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class RemoteServiceEndpoint(Protocol):
    """Where to reach a delegated service, and how to authenticate to it.

    Both attributes are Optional because a config object may exist while the
    deployment has not filled it in; a backend that requires delegation is
    expected to reject an empty ``endpoint`` with a clear error rather than
    attempt a call against "".

    Read-only properties, not plain attributes: a backend only reads them, and
    a mutable Protocol attribute is invariant, so a config dataclass declaring
    ``endpoint: str`` (never None) would not satisfy ``Optional[str]``.
    """

    @property
    def endpoint(self) -> Optional[str]: ...

    @property
    def api_key(self) -> Optional[str]: ...
