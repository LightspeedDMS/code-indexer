"""Regex search with a delegated ripgrep backend never resolves paths on this pod.

The delegated backend runs ripgrep in workspace-mcp, at the repository's
absolute path. This pod holds only index state there, so ``Path.resolve()`` of
the root or of a reported match would follow THIS disk's symlinks -- a tree
that is not the one searched. The containment policy (Bug #1401) is applied
lexically instead; with the local backend it is exactly as before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import pytest

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    RipgrepExecutionError,
)
from code_indexer.server.storage.shared.ripgrep_backend import RipgrepRun


class _DelegatedBackend:
    """Answers as workspace-mcp would: ripgrep's --json lines on stdout."""

    def __init__(self, stdout: str = "", run: Optional[RipgrepRun] = None) -> None:
        self._stdout = stdout
        self._run = run or RipgrepRun(
            exit_code=0, stderr_output="", timed_out=False, timeout_seconds=30.0
        )
        self.working_dirs: List[str] = []

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
        self.working_dirs.append(working_dir)
        Path(output_file_path).write_text(self._stdout)
        return self._run

    async def supports_pcre2(self) -> bool:
        return True


def _match(path: str, line: int) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": "needle()\n"},
                "line_number": line,
                "submatches": [{"match": {"text": "needle"}, "start": 0, "end": 6}],
            },
        }
    )


@pytest.fixture
def symlinked_pod_root(tmp_path: Path) -> Path:
    """golden-repos/<alias> reached through a symlink on THIS pod: resolving it
    would rewrite the root to a path the delegated backend never sees."""
    real = tmp_path / "pod-volume" / "golden-repos" / "agent-router"
    (real / ".code-indexer").mkdir(parents=True)
    link = tmp_path / "golden-repos"
    link.symlink_to(real.parent, target_is_directory=True)
    return link / "agent-router"


@pytest.fixture
def no_resolve(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A delegated pod: no trigram pre-filter (trigram_prefilter_available is
    False there), and any Path.resolve() fails the test."""
    from code_indexer.global_repos import trigram_index_manager

    monkeypatch.setattr(
        trigram_index_manager, "trigram_prefilter_available", lambda: False
    )

    def _boom(self: Path, strict: bool = False) -> Path:
        raise AssertionError(f"Path.resolve({self}) on a delegated pod")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(Path, "resolve", _boom)
        yield


@pytest.mark.asyncio
async def test_delegated_root_and_matches_are_never_resolved_on_this_pod(
    symlinked_pod_root: Path, no_resolve: None
) -> None:
    hit = f"{symlinked_pod_root}/src/router.py"
    backend = _DelegatedBackend(_match(hit, 3) + "\n")

    svc = RegexSearchService(symlinked_pod_root, ripgrep_backend=backend)
    result = await svc.search(pattern="needle", max_results=5)

    # The root stays the path the backend knows, not this pod's symlink target.
    assert svc.repo_path == symlinked_pod_root
    assert backend.working_dirs == [str(symlinked_pod_root)]
    assert [(m.file_path, m.line_number) for m in result.matches] == [
        ("src/router.py", 3)
    ]


@pytest.mark.asyncio
async def test_delegated_match_escaping_the_root_is_still_dropped(
    symlinked_pod_root: Path, no_resolve: None
) -> None:
    inside = f"{symlinked_pod_root}/a.py"
    escaping = f"{symlinked_pod_root}/../other-repo/secret.py"
    relative_escape = "../other-repo/x.py"
    backend = _DelegatedBackend(
        "\n".join([_match(inside, 1), _match(escaping, 2), _match(relative_escape, 3)])
        + "\n"
    )

    svc = RegexSearchService(symlinked_pod_root, ripgrep_backend=backend)
    result = await svc.search(pattern="needle", max_results=5)

    assert [m.file_path for m in result.matches] == ["a.py"]


@pytest.mark.asyncio
async def test_delegated_ripgrep_failure_is_an_error_not_an_empty_result(
    symlinked_pod_root: Path, no_resolve: None
) -> None:
    backend = _DelegatedBackend(
        run=RipgrepRun(
            exit_code=2,
            stderr_output="workspace-mcp unavailable\n",
            timed_out=False,
            timeout_seconds=30.0,
            is_error=True,
        )
    )
    svc = RegexSearchService(symlinked_pod_root, ripgrep_backend=backend)

    with pytest.raises(RipgrepExecutionError):
        await svc.search(pattern="needle", max_results=5)


def test_local_backend_still_resolves_the_root(symlinked_pod_root: Path) -> None:
    """Parity: the local backend keeps Bug #1401's resolved canonical root."""
    svc = RegexSearchService(symlinked_pod_root)

    assert svc.repo_path == symlinked_pod_root.resolve()
    assert svc.repo_path != symlinked_pod_root


def test_local_backend_still_follows_symlinks_for_containment(
    symlinked_pod_root: Path,
) -> None:
    """Parity: a match path given through the symlinked root is contained,
    and an in-repo symlink pointing outside is dropped, exactly as before."""
    real_root = symlinked_pod_root.resolve()
    outside = real_root.parent / "outside.py"
    outside.write_text("x")
    os.symlink(outside, real_root / "escape.py")
    svc = RegexSearchService(symlinked_pod_root)

    assert svc._to_repo_relative(f"{symlinked_pod_root}/src/a.py") == "src/a.py"
    assert svc._to_repo_relative(f"{symlinked_pod_root}/escape.py") is None
