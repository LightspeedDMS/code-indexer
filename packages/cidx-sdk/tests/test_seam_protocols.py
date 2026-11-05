"""Every seam has its Protocol in ``cidx_sdk.seams`` and an entry-point group."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import cidx_sdk.seams as seams
from cidx_sdk.entry_points import SEAM_ENTRY_POINT_GROUPS
from cidx_sdk.seams.content_identity import git_blob_hash, git_blob_hash_bytes
from cidx_sdk.seams.selection import SEAM_MODES
from cidx_sdk.seams.xray import XrayGraphRequest, XrayScanOutcome, XrayScanRequest

#: seam name -> the Protocol(s) that seam is typed by.
_SEAM_PROTOCOLS = {
    "content": ("ContentBackend",),
    "content_writer": ("ContentWriter",),
    "git": ("GitBackend", "GitCommandRunner"),
    "git_invoker": ("GitInvoker",),
    "ripgrep": ("RipgrepBackend",),
    "search": ("SearchBackend",),
    "index_artifacts": ("IndexStore",),
    "scip": ("ScipBackend",),
    "xray": ("XrayBackend",),
    "clone": ("CloneBackend",),
    "cli_invoker": ("CliInvoker",),
}


def test_every_seam_group_has_a_protocol_and_modes() -> None:
    assert set(_SEAM_PROTOCOLS) == set(SEAM_ENTRY_POINT_GROUPS) == set(SEAM_MODES)
    for names in _SEAM_PROTOCOLS.values():
        for name in names:
            assert name in seams.__all__, name
            assert getattr(seams, name).__module__.startswith("cidx_sdk.seams.")


def test_the_blob_hash_is_gits(tmp_path: Path) -> None:
    data = b"hello\n"
    expected = hashlib.sha1(b"blob 6\0" + data).hexdigest()
    assert git_blob_hash_bytes(data) == expected
    target = tmp_path / "f"
    target.write_bytes(data)
    assert git_blob_hash(target) == expected
    assert git_blob_hash(tmp_path / "missing") is None


class _Writer:
    def put(
        self, path: str, data: bytes, *, expected_blob_sha: Optional[str] = None
    ) -> str:
        return git_blob_hash_bytes(data)

    def delete(self, path: str, *, expected_blob_sha: Optional[str] = None) -> bool:
        return False


class _Scip:
    def build_index(self, request: Any) -> Any:
        return None

    def is_available(self) -> bool:
        return True


class _Limits:
    max_files = 1
    max_file_bytes = 2
    max_total_bytes = 3


class _InlineXray:
    inline_limits = _Limits()

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        return XrayScanOutcome(results=[])

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        return {}

    def execute_scan_inline(
        self, request: XrayScanRequest, files: Mapping[str, bytes]
    ) -> XrayScanOutcome:
        return XrayScanOutcome(results=[])

    def execute_graph_analysis_inline(
        self, request: XrayGraphRequest, files: Mapping[str, bytes]
    ) -> Dict[str, Any]:
        return {}


class _Invoker:
    def run(
        self, cmd: Sequence[str], cwd: Any, *, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(list(cmd), 0, "", "")


def test_the_new_protocols_are_runtime_checkable() -> None:
    assert isinstance(_Writer(), seams.ContentWriter)
    assert isinstance(_Scip(), seams.ScipBackend)
    assert isinstance(_InlineXray(), seams.XrayBackend)
    assert isinstance(_InlineXray(), seams.XrayInlineExecutionBackend)
    assert isinstance(_Invoker(), seams.GitInvoker)
    assert not isinstance(object(), seams.ContentWriter)
