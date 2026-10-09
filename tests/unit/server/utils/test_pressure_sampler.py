"""Story S12: memory-pressure samples for the worker-stall watchdog."""

from __future__ import annotations

import os
import re
import time

from code_indexer.server.utils.pressure_sampler import (
    PressureSampler,
    _psi_avg10,
    parse_nfs_mountstats,
)

# Real /proc/self/mountstats layout (statvers=1.1); neutral RFC 5737 addresses.
# The "events:" / "bytes:" / "xprt:" lines look like per-op lines on purpose.
_MOUNTSTATS = """\
device proc mounted on /proc with fstype proc
device nfsd mounted on /proc/fs/nfsd with fstype nfsd
device 192.0.2.10:/export/golden mounted on /mnt/golden with fstype nfs statvers=1.1
\topts:\trw,vers=3,hard,nolock,proto=tcp,timeo=600,retrans=2,sec=sys
\tage:\t123456
\tcaps:\tcaps=0x3fc7,wtmult=4096,dtsize=1048576,bsize=0,namlen=255
\tsec:\tflavor=1,pseudoflavor=1
\tevents:\t900 800 700 600 500 400 300 200 100
\tbytes:\t5000 4000 3000 2000 1000 900 800 700
\tRPC iostats version: 1.1  p/v: 100003/3 (nfs)
\txprt:\ttcp 0 0 1 0 12 1000 990 0 5000 0 2 100 200
\tper-op statistics
\t        NULL: 1 1 0 44 24 0 0 0 0
\t     GETATTR: 500 503 2 70000 56000 10 300 320 0
\t        READ: 100 101 0 14000 1048000 5 900 920 0
\t       WRITE: 50 50 1 6000000 7000 1 400 410 0

device /dev/sda1 mounted on /boot with fstype xfs
device 192.0.2.11:/export/cow mounted on /mnt/cow with fstype nfs4 statvers=1.1
\tRPC iostats version: 1.1  p/v: 100003/4 (nfs)
\tper-op statistics
\t        NULL: 0 0 0 0 0 0 0 0 0
\t        READ: 10 10 0 1400 104800 0 90 92 0
"""


def test_parse_nfs_mountstats_counts_retransmits_and_major_timeouts_per_nfs_mount() -> (
    None
):
    assert parse_nfs_mountstats(_MOUNTSTATS) == [
        # retrans = sum(transmissions - ops) = 0 + 3 + 1 + 0; timeouts = 0+2+0+1
        ("/mnt/golden", 4, 3),
        ("/mnt/cow", 0, 0),
    ]


def test_psi_avg10_reads_some_and_full_from_kernel_pressure_format() -> None:
    memory = (
        "some avg10=1.23 avg60=0.50 avg300=0.10 total=123456\n"
        "full avg10=0.40 avg60=0.20 avg300=0.05 total=65432\n"
    )
    assert _psi_avg10(memory) == "some_avg10=1.23,full_avg10=0.40"
    some_only = "some avg10=7.00 avg60=3.00 avg300=1.00 total=99\n"
    assert _psi_avg10(some_only) == "some_avg10=7.00"
    assert _psi_avg10(None) == "unavailable"
    assert _psi_avg10("garbage\n") == "unavailable"


def test_parse_nfs_mountstats_without_nfs_mounts_is_empty() -> None:
    local_only = (
        "device proc mounted on /proc with fstype proc\n"
        "device /dev/sda1 mounted on /boot with fstype xfs\n"
    )
    assert parse_nfs_mountstats(local_only) == []


def test_sample_reads_real_proc_and_reports_swap_rates_from_the_second_sample() -> None:
    sampler = PressureSampler()
    first = sampler.sample()
    time.sleep(0.05)
    second = sampler.sample()

    for sample in (first, second):
        assert re.search(r" rss_kb=\d+ ", sample), sample
        assert re.search(r" mem_available_kb=\d+ ", sample), sample
        assert re.search(r" nfs=\S+$", sample), sample
        psi = re.search(r" psi_memory=(\S+) ", sample)
        assert psi, sample
        if os.path.exists("/proc/pressure/memory"):
            assert psi.group(1).startswith("some_avg10="), sample
        else:
            assert psi.group(1) == "unavailable", sample
    assert "swap_in_pages_per_s=n/a swap_out_pages_per_s=n/a" in first
    assert re.search(
        r" swap_in_pages_per_s=\d+\.\d swap_out_pages_per_s=\d+\.\d ", second
    ), second
