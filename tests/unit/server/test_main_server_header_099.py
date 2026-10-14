"""`python -m code_indexer.server.main` launches uvicorn without the Server header."""

import sys
from typing import Any, Dict, List

from code_indexer.server import main as server_main


def test_main_disables_uvicorn_server_header(tmp_path, monkeypatch):
    launches: List[Dict[str, Any]] = []
    # Recorder in place of uvicorn.run: starting a real server is out of scope.
    monkeypatch.setattr(
        server_main.uvicorn, "run", lambda *args, **kwargs: launches.append(kwargs)
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["cidx-server", "--port", "8123"])

    server_main.main()

    assert len(launches) == 1
    assert launches[0]["server_header"] is False
    assert launches[0]["port"] == 8123
