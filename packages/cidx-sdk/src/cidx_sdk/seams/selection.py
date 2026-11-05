"""Fail-loud seam selection: which installed module satisfies a seam.

Target model (``plans/designs/modular-endstate.md`` section 7): every seam is
satisfied by exactly one installed module, either a local implementation
(``cidx-backend-*``) or a delegate plugin (``cidx-delegate-*``). The kernel
holds neither, so a seam configured with no matching module installed must
stop startup and name the seam.

ONE entry-point group per seam. The configured MODE (``local``,
``delegated``, or a seam-specific one such as the clone seam's ``ontap``) is
chosen by ENTRY NAME within that group:

* ``local`` selects the entry named ``local``;
* any other declared mode selects the entry of that name;
* ``delegated`` selects the entry named ``delegated`` -- and, for the
  delegate plugins published before this rule existed, any entry whose name is
  not one of the seam's other modes (``workspacemcp``, ``gitmcp``, ``sandbox``,
  ``orchestrator``). New delegates register as ``delegated``.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, List, Optional, Sequence

from cidx_sdk.entry_points import SEAM_ENTRY_POINT_GROUPS
from cidx_sdk.errors import SeamUnavailableError

#: The mode whose implementation runs in this process, on this pod's disk.
LOCAL_MODE = "local"
#: The mode whose implementation calls the remote service owning the seam.
DELEGATED_MODE = "delegated"

#: Every mode name a seam may be configured with, beyond local and delegated.
#: An entry with one of these names is never mistaken for a legacy delegate.
SEAM_MODES = {
    "content": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "content_writer": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "git": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "git_invoker": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "ripgrep": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "search": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "index_artifacts": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    # scip_backend's delegated value is spelled "delegated-plugin" in config.
    "scip": frozenset({LOCAL_MODE, DELEGATED_MODE, "delegated-plugin"}),
    "xray": frozenset({LOCAL_MODE, DELEGATED_MODE}),
    "clone": frozenset({LOCAL_MODE, DELEGATED_MODE, "ontap", "cow-daemon"}),
    "cli_invoker": frozenset({LOCAL_MODE, DELEGATED_MODE}),
}


def seam_entry_point_group(seam: str) -> str:
    """The entry-point group for *seam*.

    Raises:
        SeamUnavailableError: *seam* is not a known seam name.
    """
    group = SEAM_ENTRY_POINT_GROUPS.get(seam)
    if group is None:
        raise SeamUnavailableError(
            f"unknown seam {seam!r}; known seams: {sorted(SEAM_ENTRY_POINT_GROUPS)}"
        )
    return group


def seam_modes(seam: str) -> FrozenSet[str]:
    """The mode names *seam* may be configured with (local and delegated for a
    seam this SDK does not list)."""
    return SEAM_MODES.get(seam, frozenset({LOCAL_MODE, DELEGATED_MODE}))


def is_delegate_mode(seam: str, mode: str) -> bool:
    """Whether *mode* selects a delegate (any mode but ``local``)."""
    return mode in seam_modes(seam) and mode != LOCAL_MODE


def entry_names_for_mode(seam: str, mode: str, names: Iterable[str]) -> List[str]:
    """The entry names in *seam*'s group that satisfy *mode*, in input order.

    ``delegated`` also accepts a legacy delegate name: any name that is not
    one of the seam's modes (see the module docstring). Every other mode
    matches its own name only.
    """
    all_names = list(names)
    if mode != DELEGATED_MODE:
        return [name for name in all_names if name == mode]
    other_modes = seam_modes(seam) - {DELEGATED_MODE}
    return [name for name in all_names if name not in other_modes]


def require_seam_provider(
    seam: str, mode: str, providers: Sequence[str], group: Optional[str] = None
) -> str:
    """Return the single provider for *seam*, or refuse loudly.

    Args:
        seam: a key of ``SEAM_ENTRY_POINT_GROUPS``.
        mode: the configured selection (``"local"``, ``"delegated"``, ...),
            used only in the message.
        providers: names of the installed modules that registered for the
            seam in the configured mode.
        group: the seam's entry-point group, when it is not the one
            ``SEAM_ENTRY_POINT_GROUPS`` lists (named in the message).

    Raises:
        SeamUnavailableError: no provider, more than one, or an unknown seam.
    """
    group = group or seam_entry_point_group(seam)
    names = list(providers)
    if not names:
        raise SeamUnavailableError(
            f"seam {seam!r} is configured {mode!r} but no module implementing it "
            f"is installed (entry-point group {group!r}). Install the matching "
            f"module; there is no built-in fallback."
        )
    if len(names) > 1:
        raise SeamUnavailableError(
            f"seam {seam!r} ({mode!r}) has {len(names)} installed providers "
            f"{sorted(names)} in group {group!r}; exactly one is allowed."
        )
    return names[0]
