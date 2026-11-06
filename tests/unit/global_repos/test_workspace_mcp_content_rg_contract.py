"""cidx's requests to workspace-mcp's ``POST /content/rg``, pinned as a consumer contract.

workspace-mcp validates the argv against an allowlist (``parseCidxRgArgv``) and caps
``timeout_ms`` at 300000 and ``max_output_bytes`` at 64 MiB. cidx builds the argv and
chooses the limits. Each side's tests used a fake of the other (a fake ``rg`` there, a
fake backend here), so a request the server refuses -- twice now: limits over the cap, then
a ``--json`` listing of 205 MB -- showed up only on a live pod.

This test drives cidx's REAL argv builders (the search, through ``RegexSearchService``, and the
trigram file listing) with a recording backend, shapes what the plugin would POST, and writes
it as a Pact (v2, consumer ``cidx``, provider ``workspace-mcp``). workspace-mcp holds a copy
and replays every interaction against its real controller in its own CI, so a change on
either side that breaks the other fails a pipeline instead of a pod.

* Only what cidx SENDS is recorded; the response expectation is just status 200. The body is
  ripgrep's stdout, a NUL and a JSON trailer, which each side tests on its own. What the server
  must REFUSE (both output modes, ``--null``) is a provider unit test, not a consumer contract.
* After changing what cidx sends: regenerate with ``UPDATE_CONTRACTS=1 pytest <this file>``,
  copy ``tests/contracts/workspace-mcp/cidx-workspace-mcp.json`` to workspace-mcp's
  ``test/contracts/pacts/``, and merge and deploy workspace-mcp first.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from code_indexer.global_repos import trigram_index_manager as tim
from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.server.storage.shared.ripgrep_backend import RipgrepRun

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "workspace-mcp"
    / "cidx-workspace-mcp.json"
)
#: A repository path the provider's verification creates; cidx sends the real golden
#: repository path in production, which only has to be inside the server's allowed roots.
WORKING_DIR = "/tmp/cidx-content-rg-contract/repo"
#: The provider replaces this with its own secret when it replays the request.
BEARER = "Bearer cidx-contract-token"
PROVIDER_STATE = "a repository exists under the content root"


class _RecordingBackend:
    """A delegated ripgrep backend that answers "no matches" and keeps the calls."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    @property
    def is_local(self) -> bool:
        return False

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        self.calls.append(
            {
                "argv": list(argv),
                "working_dir": working_dir,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            }
        )
        Path(output_file_path).write_text("")
        return RipgrepRun(
            exit_code=1, stderr_output="", timed_out=False, timeout_seconds=30.0
        )

    async def supports_pcre2(self) -> bool:
        return True


def _request_body(call: Dict[str, Any]) -> Dict[str, Any]:
    """What the cidx-content-backend plugin POSTs for one ``run`` call
    (``WorkspaceMcpRipgrepBackend._run_blocking``): the deadline rounded UP to whole
    milliseconds, never down, and the byte ceiling as an int."""
    return {
        "argv": call["argv"],
        "working_dir": call["working_dir"],
        "timeout_ms": max(1, int(math.ceil(call["timeout_seconds"] * 1000))),
        "max_output_bytes": int(call["max_output_bytes"]),
    }


def _interaction(description: str, call: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "description": description,
        "providerState": PROVIDER_STATE,
        "request": {
            "method": "POST",
            "path": "/content/rg",
            "headers": {"Authorization": BEARER, "Content-Type": "application/json"},
            "body": _request_body(call),
        },
        "response": {"status": 200},
    }


async def _searches() -> List[Dict[str, Any]]:
    """Searches through the real service: every flag cidx can add to the argv."""
    backend = _RecordingBackend()
    service = RegexSearchService(Path(WORKING_DIR), ripgrep_backend=backend)
    await service.search(pattern="needle", max_results=5, timeout_seconds=30)
    await service.search(
        pattern="needle.*thread",
        include_patterns=["*.py"],
        exclude_patterns=["tests/*"],
        case_sensitive=False,
        context_lines=2,
        multiline=True,
        pcre2=True,
        max_results=5,
        timeout_seconds=30,
    )
    return backend.calls


def _listing() -> Dict[str, Any]:
    """The trigram index's listing of the files ripgrep would search."""
    backend = _RecordingBackend()
    tim.list_ripgrep_files(backend, Path(WORKING_DIR))
    (call,) = backend.calls
    return call


def _contract(
    searches: List[Dict[str, Any]], listing: Dict[str, Any]
) -> Dict[str, Any]:
    plain, flagged = searches
    return {
        "consumer": {"name": "cidx"},
        "provider": {"name": "workspace-mcp"},
        "interactions": [
            _interaction("a regex search", plain),
            _interaction("a regex search using every search flag", flagged),
            _interaction(
                "the trigram index's listing of the files a search reads", listing
            ),
        ],
        "metadata": {"pactSpecification": {"version": "2.0.0"}},
    }


def _text(contract: Dict[str, Any]) -> str:
    return json.dumps(contract, indent=2) + "\n"


@pytest.fixture
def generated() -> Dict[str, Any]:
    import asyncio

    return _contract(asyncio.run(_searches()), _listing())


def test_the_committed_contract_is_what_cidx_sends(generated: Dict[str, Any]) -> None:
    if os.environ.get("UPDATE_CONTRACTS") == "1":
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(_text(generated))
    committed: Optional[str] = CONTRACT.read_text() if CONTRACT.exists() else None

    assert committed == _text(generated), (
        "cidx's /content/rg requests changed. Regenerate with "
        "`UPDATE_CONTRACTS=1 pytest tests/unit/global_repos/"
        "test_workspace_mcp_content_rg_contract.py`, then copy the file to workspace-mcp's "
        "test/contracts/pacts/ and merge and deploy workspace-mcp first."
    )


def test_the_listing_asks_for_paths_only_within_the_servers_limits(
    generated: Dict[str, Any],
) -> None:
    listing = generated["interactions"][2]["request"]["body"]

    assert "--files-with-matches" in listing["argv"] and "--json" not in listing["argv"]
    # workspace-mcp rejects anything over these with a 400 (CONTENT_RG_MAX_*).
    assert listing["timeout_ms"] <= 300_000
    assert listing["max_output_bytes"] <= 64 * 1024 * 1024


def test_every_search_stays_within_the_servers_limits(
    generated: Dict[str, Any],
) -> None:
    for interaction in generated["interactions"][:2]:
        body = interaction["request"]["body"]
        assert "--json" in body["argv"] and "--files-with-matches" not in body["argv"]
        assert 0 < body["timeout_ms"] <= 300_000
        assert 0 < body["max_output_bytes"] <= 64 * 1024 * 1024
