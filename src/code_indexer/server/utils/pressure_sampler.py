"""Memory-pressure samples for the worker-stall watchdog (Story S12).

One sample is one text line built from small ``/proc`` reads. None of them
touches an NFS server: ``/proc/self/mountstats`` is the kernel's own RPC
counters. A source the kernel does not provide (for example ``/proc/pressure``
on a kernel without PSI) is written as ``unavailable``, never a made-up value.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

UNAVAILABLE = "unavailable"
NO_PREVIOUS_SAMPLE = "n/a"
_NFS_CLIENT_FSTYPES = ("nfs", "nfs4")
# Bounded sample line: at most this many NFS mounts are listed (the rest are
# counted as "+N more"), each mount point cut to this many characters.
MAX_NFS_MOUNTS = 16
MAX_MOUNT_POINT_CHARS = 128


def utc_stamp(epoch: float) -> str:
    """``YYYYmmddTHHMMSSmmmZ``: sortable, filename-safe, millisecond resolution."""
    millis = int((epoch % 1) * 1000)
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(epoch)) + f"{millis:03d}Z"


def _read_proc(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _fields(text: Optional[str], keys: Tuple[str, ...]) -> Dict[str, int]:
    """Integer second column of ``key: value`` / ``key value`` lines."""
    found: Dict[str, int] = {}
    if text is None:
        return found
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[0] in keys and parts[1].isdigit():
            found[parts[0]] = int(parts[1])
    return found


def _psi_avg10(text: Optional[str]) -> str:
    """``some_avg10=X,full_avg10=Y`` from a ``/proc/pressure/*`` file."""
    if text is None:
        return UNAVAILABLE
    values = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("avg10="):
            values.append(f"{parts[0]}_{parts[1]}")
    return ",".join(values) if values else UNAVAILABLE


def parse_nfs_mountstats(text: str) -> List[Tuple[str, int, int]]:
    """``(mount point, RPC retransmissions, major timeouts)`` per NFS mount.

    Per-op lines of an NFS mount read ``NAME: ops transmissions major_timeouts
    ...``; retransmissions are ``transmissions - ops``. Only the lines after
    ``per-op statistics`` are per-op lines (``xprt:``, ``bytes:`` look alike).
    """
    mounts: List[Tuple[str, int, int]] = []
    mount_point: Optional[str] = None
    in_per_op = False
    retrans = timeouts = 0
    for line in text.splitlines():
        parts = line.split()
        if parts[:1] == ["device"]:
            if mount_point is not None:
                mounts.append((mount_point, retrans, timeouts))
            # "device <dev> mounted on <mount point> with fstype <type> ..."
            # Exact match: "nfsd" (the NFS server's pseudo-fs) is not a mount
            # of a remote export.
            is_nfs = len(parts) >= 8 and parts[7] in _NFS_CLIENT_FSTYPES
            mount_point = parts[4] if is_nfs else None
            in_per_op = False
            retrans = timeouts = 0
        elif mount_point is not None and line.strip() == "per-op statistics":
            in_per_op = True
        elif in_per_op and len(parts) >= 4 and parts[0].endswith(":"):
            numbers = parts[1:4]
            if all(number.isdigit() for number in numbers):
                ops, transmissions, major = (int(number) for number in numbers)
                retrans += transmissions - ops
                timeouts += major
    if mount_point is not None:
        mounts.append((mount_point, retrans, timeouts))
    return mounts


class PressureSampler:
    """Takes one sample per call; keeps the previous swap counters for rates."""

    def __init__(self) -> None:
        self._previous_swap: Optional[Tuple[float, int, int]] = None

    def sample(self) -> str:
        now = time.time()
        status = _fields(_read_proc("/proc/self/status"), ("VmRSS", "VmSwap"))
        meminfo = _fields(_read_proc("/proc/meminfo"), ("MemAvailable", "SwapFree"))
        vmstat = _fields(_read_proc("/proc/vmstat"), ("pswpin", "pswpout"))
        mountstats = _read_proc("/proc/self/mountstats")
        return (
            f"{utc_stamp(now)}"
            f" rss_kb={status.get('VmRSS', UNAVAILABLE)}"
            f" swap_kb={status.get('VmSwap', UNAVAILABLE)}"
            f" mem_available_kb={meminfo.get('MemAvailable', UNAVAILABLE)}"
            f" swap_free_kb={meminfo.get('SwapFree', UNAVAILABLE)}"
            f" {self._swap_rates(time.monotonic(), vmstat)}"
            f" psi_memory={_psi_avg10(_read_proc('/proc/pressure/memory'))}"
            f" psi_io={_psi_avg10(_read_proc('/proc/pressure/io'))}"
            f" nfs={self._nfs(mountstats)}"
        )

    def _swap_rates(self, now: float, vmstat: Dict[str, int]) -> str:
        if "pswpin" not in vmstat or "pswpout" not in vmstat:
            self._previous_swap = None
            return (
                f"swap_in_pages_per_s={UNAVAILABLE} swap_out_pages_per_s={UNAVAILABLE}"
            )
        current = (now, vmstat["pswpin"], vmstat["pswpout"])
        previous, self._previous_swap = self._previous_swap, current
        if previous is None or current[0] <= previous[0]:
            return (
                f"swap_in_pages_per_s={NO_PREVIOUS_SAMPLE} "
                f"swap_out_pages_per_s={NO_PREVIOUS_SAMPLE}"
            )
        elapsed = current[0] - previous[0]
        return (
            f"swap_in_pages_per_s={(current[1] - previous[1]) / elapsed:.1f} "
            f"swap_out_pages_per_s={(current[2] - previous[2]) / elapsed:.1f}"
        )

    @staticmethod
    def _nfs(mountstats: Optional[str]) -> str:
        if mountstats is None:
            return UNAVAILABLE
        mounts = parse_nfs_mountstats(mountstats)
        if not mounts:
            return "none"
        entries = []
        for mount, retrans, timeouts in mounts[:MAX_NFS_MOUNTS]:
            if len(mount) > MAX_MOUNT_POINT_CHARS:
                mount = mount[: MAX_MOUNT_POINT_CHARS - 3] + "..."
            entries.append(f"{mount}:retrans={retrans},major_timeouts={timeouts}")
        if len(mounts) > MAX_NFS_MOUNTS:
            entries.append(f"+{len(mounts) - MAX_NFS_MOUNTS} more")
        return ";".join(entries)
