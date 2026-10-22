"""The environment a real ``RefreshScheduler`` refresh needs to run the REAL
``cidx index`` child against the local fake VoyageAI server: a ``cidx`` on
PATH running this interpreter's CLI, the fake's sentinel key, an offline
tokenizer cache and a network guard refusing any non-loopback connection.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest

from tests.utils.cidx_fake_provider_repo import SRC_DIR, install_network_guard
from tests.utils.fake_voyage_provider import running_fake_voyage_server

#: Pins the vendor endpoint in server context; the only flag the shim drops
#: (as in the Story #2118 test), so the child reaches the local fake that the
#: repository config names. The network guard still refuses anything else.
ENDPOINT_PIN_FLAG = "--server-managed-provider-settings"


def install_endpoint_shim(bin_dir: Path) -> Path:
    """A ``cidx`` on PATH running this interpreter's real CLI without
    ``ENDPOINT_PIN_FLAG``."""
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "cidx"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"args = [a for a in sys.argv[1:] if a != {ENDPOINT_PIN_FLAG!r}]\n"
        "os.execv(sys.executable, "
        "[sys.executable, '-m', 'code_indexer.cli', *args])\n"
    )
    shim.chmod(0o755)
    return bin_dir


def point_repo_at_fake(repo: Path, fake: Any) -> None:
    """Route an initialised repository's embeddings to the local fake."""
    config_path = repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config.setdefault("voyage_ai", {})["api_endpoint"] = (
        f"{fake.base_url}/v1/embeddings"
    )
    config_path.write_text(json.dumps(config))


@contextlib.contextmanager
def fake_provider_refresh_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_key: str
) -> Iterator[Tuple[Any, Dict[str, str], Path]]:
    """Yield ``(fake server, child env, refused-network log)``; on exit
    assert no child attempted a non-loopback connection."""
    with running_fake_voyage_server(api_key) as fake:
        guard_root = tmp_path / "guard"
        guard_root.mkdir()
        site_dir, blocked_log = install_network_guard(guard_root)
        shim_dir = install_endpoint_shim(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("VOYAGE_API_KEY", api_key)
        monkeypatch.setenv("PYTHONPATH", f"{site_dir}{os.pathsep}{SRC_DIR}")
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        yield fake, dict(os.environ), blocked_log
        assert not blocked_log.exists(), blocked_log.read_text()
