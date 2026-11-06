"""The core's side of seam resolution: where its resolvers read entry points.

Resolution itself is the SDK's (:class:`cidx_sdk.seams.SeamResolver`): env var,
then the entry point named for the configured mode, else a loud refusal naming
the seam. What stays in the core is reading WHICH mode is configured (its
config service) and, here, the one adapter every core resolver module uses to
give the SDK its entry points.

:func:`module_entry_point_source` looks the ``entry_points`` function and the
``sys`` module up on the resolver's OWN module at call time. Two reasons:

* tests patch ``<module>.entry_points`` to control what is "installed", and
  that patch must keep reaching the resolver after the logic moved to the SDK;
* the core still parses under Python 3.9, where
  ``importlib.metadata.entry_points()`` takes no arguments and returns a dict
  of group -> list. Both call shapes are supported here, once.
"""

from __future__ import annotations

import sys
from typing import Any, List

from cidx_sdk.seams.resolver import EntryPointLike, EntryPointSource


def select_entry_points(
    entry_points_fn: Any, version_info: Any, group: str
) -> List[EntryPointLike]:
    """*group*'s entry points from an ``importlib.metadata.entry_points``-like
    function, in the call shape of the interpreter version *version_info*."""
    if tuple(version_info[:2]) >= (3, 10):
        return list(entry_points_fn(group=group))
    return list(entry_points_fn().get(group, []))


def module_entry_point_source(module_name: str) -> EntryPointSource:
    """An entry-point source reading ``entry_points``/``sys`` from *module_name*.

    The lookup happens on every call, so a test that patches either attribute
    of that module after the resolver was built is honoured.
    """

    def source(group: str) -> List[EntryPointLike]:
        module = sys.modules[module_name]
        return select_entry_points(
            getattr(module, "entry_points"),
            getattr(module, "sys").version_info,
            group,
        )

    return source
