"""What a feature's REST routes and web pages receive from the core.

The core authenticates every request to a contributed router or UI page and
checks the contribution's ``required_permission`` before the feature's
handler runs. It then stores, on the request state:

* :data:`PRINCIPAL_STATE_ATTR` -- the caller, a :class:`Principal`;
* :data:`RENDER_STATE_ATTR` (UI pages only) -- a :class:`PageRenderer` that
  renders one of the feature's templates inside the core's web shell
  (layout, navigation, theming). A template extends the shell with
  ``{% extends shell_template %}`` (the admin or the user shell, chosen by
  the viewer's role) and fills ``{% block title %}`` and
  ``{% block content %}``. The context also carries ``principal``,
  ``page_title`` and ``feature_name``.

A handler reads them without importing the core::

    principal = getattr(request.state, PRINCIPAL_STATE_ATTR)
    render = getattr(request.state, RENDER_STATE_ATTR)
    return render("example.html", {"rows": rows})
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

#: Attribute of ``request.state`` holding the caller's :class:`Principal`.
PRINCIPAL_STATE_ATTR = "cidx_principal"
#: Attribute of ``request.state`` holding the page's :class:`PageRenderer`.
RENDER_STATE_ATTR = "cidx_render"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a feature route or page.

    Attributes:
        username: the account name.
        role: the account role (``admin``, ``power_user``, ``normal_user``).
    """

    username: str
    role: str


class PageRenderer(Protocol):
    """Renders a feature template inside the core's web shell."""

    def __call__(self, template_name: str, context: Mapping[str, object]) -> object:
        """An HTML response (a Starlette ``Response``) for *template_name*,
        looked up in the page's ``template_dirs`` first, then the shell's."""
        ...
