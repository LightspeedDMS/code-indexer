"""The trigram pre-filter on a pod that does not hold the repository.

With content AND ripgrep delegated, refresh builds the trigram index through
the backends: ripgrep's own walk (listed where ripgrep runs, by a
``--files-with-matches`` search workspace-mcp's ``/content/rg`` admits -- paths
only -- or, against a server that refuses it, by the ``--json`` one) and bytes fetched through the
content backend -- only for files whose size/mtime changed since the index on
this pod was built. Searches then read the index this pod serves for the alias
target and send its candidates back to ripgrep as explicit paths. Pinned:

* the availability rule (both delegated, or local; never on an unknown setting);
* the listing: allowlisted argv, ripgrep's paths made repo-relative, and an
  incomplete listing refused rather than turned into a subset index;
* the build: oversized and unreadable files are always-candidates, and an
  incremental build fetches only what changed while keeping the rest findable;
* refresh builds it through the backends; no lazy build on a search;
* the search reads ``trigram_root``'s index and passes lexical candidate paths.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from code_indexer.global_repos import regex_search as rs
from code_indexer.global_repos import trigram_index_manager as tim
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.startup import ripgrep_backend_wiring as rg_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    FetchedFile,
)
from code_indexer.server.storage.shared.ripgrep_backend import RipgrepRun
from code_indexer.utils.index_store import INDEX_DIRNAME

ROOT = Path("/golden-repos/example-repo")


def _ok(exit_code: Optional[int] = 0, **kw: Any) -> RipgrepRun:
    return RipgrepRun(
        exit_code=exit_code,
        stderr_output=kw.pop("stderr_output", ""),
        timed_out=kw.pop("timed_out", False),
        timeout_seconds=30.0,
        **kw,
    )


class _Ripgrep:
    """A delegated ripgrep backend: writes the given stdout, records argv."""

    def __init__(
        self,
        stdout: str = "",
        result: Optional[RipgrepRun] = None,
        *,
        paths_stdout: Optional[str] = None,
        refuses_paths_only: bool = False,
    ):
        self.stdout = stdout
        self.result = result or _ok()
        #: What a ``--files-with-matches`` run prints. By default the paths of the
        #: ``--json`` listing in ``stdout``, so one fake serves both shapes.
        self.paths_stdout = paths_stdout
        #: A server that predates the paths-only listing: it answers 400.
        self.refuses_paths_only = refuses_paths_only
        self.argvs: List[List[str]] = []
        self.timeouts: List[float] = []
        self.max_output_bytes: List[int] = []

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
        self.argvs.append(list(argv))
        self.timeouts.append(timeout_seconds)
        self.max_output_bytes.append(max_output_bytes)
        if "--files-with-matches" in argv:
            if self.refuses_paths_only:
                return _ok(
                    exit_code=None,
                    is_error=True,
                    error_message="delegated ripgrep failed: HTTP 400: exactly one of "
                    "--json or --files-with-matches is required",
                )
            Path(output_file_path).write_text(self._paths_output())
            return self.result
        Path(output_file_path).write_text(self.stdout)
        return self.result

    def _paths_output(self) -> str:
        if self.paths_stdout is not None:
            return self.paths_stdout
        events = (json.loads(line) for line in self.stdout.splitlines() if line)
        return "".join(
            e["data"]["path"]["text"] + "\n" for e in events if e["type"] == "begin"
        )

    async def supports_pcre2(self) -> bool:
        return False


def _listing(paths: Sequence[str]) -> str:
    return "".join(
        json.dumps({"type": "begin", "data": {"path": {"text": f"{ROOT}/{p}"}}})
        + "\n"
        + json.dumps({"type": "match", "data": {"path": {"text": f"{ROOT}/{p}"}}})
        + "\n"
        for p in paths
    )


class _Content:
    """A delegated content backend over an in-memory repository."""

    manifest_options = frozenset({"hash"})

    def __init__(self, files: Dict[str, bytes], mtimes: Dict[str, int]) -> None:
        self.files = files
        self.mtimes = mtimes
        self.fetched: List[str] = []
        self.manifest_kwargs: List[Dict[str, Any]] = []
        self.fail: Dict[str, str] = {}

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kwargs: Any) -> ContentPage:
        self.manifest_kwargs.append(kwargs)
        return ContentPage(
            entries=tuple(
                ContentEntry(
                    path=p,
                    size=len(data),
                    blob_sha=None,
                    is_symlink=False,
                    mtime_ns=self.mtimes[p],
                )
                for p, data in sorted(self.files.items())
            ),
            next_cursor=None,
        )

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        for p in paths:
            self.fetched.append(p)
            if p in self.fail:
                yield FetchedFile(path=p, blob_sha=None, data=b"", error=self.fail[p])
            else:
                yield FetchedFile(path=p, blob_sha="x", data=self.files[p])


def _facts(content: _Content) -> Dict[str, tim.ManifestFacts]:
    return tim.manifest_facts(content, ROOT)


def _recording_build(calls: List[Path]) -> Any:
    def build(self: object, repo_path: Path, file_list: object = None) -> int:
        calls.append(repo_path)
        return 0

    return build


def _candidates(index_dir: Path, needle: str) -> List[str]:
    from code_indexer.global_repos.regex_trigram import extract_required_trigrams

    required = extract_required_trigrams(needle, case_insensitive=False)
    assert required
    result = tim.TrigramIndexManager(index_dir).query(required)
    assert result is not None
    return sorted(result)


# ── availability ───────────────────────────────────────────────────────────


def test_available_only_with_both_backends_delegated_or_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, ripgrep = _Content({}, {}), _Ripgrep()
    monkeypatch.setattr(wiring, "server_content_backend", lambda: content)
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)

    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "local")
    assert tim.trigram_prefilter_available() is True
    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "delegated")
    assert tim.trigram_prefilter_available() is True
    assert tim.delegated_trigram_backends() == (content, ripgrep)

    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: None)
    assert tim.trigram_prefilter_available() is False
    assert tim.delegated_trigram_backends() is None


# ── listing ────────────────────────────────────────────────────────────────


_LISTING_FLAGS = {
    "--multiline",
    "-e",
    "-g",
    tim._LISTING_PATTERN,
    f"!{INDEX_DIRNAME}/**",
    "!.git/**",
}


def test_listing_is_an_allowlisted_paths_only_search_made_repo_relative() -> None:
    ripgrep = _Ripgrep(_listing(["a.py", "sub/b.java"]))

    assert sorted(tim.list_ripgrep_files(ripgrep, ROOT)) == ["a.py", "sub/b.java"]

    (argv,) = ripgrep.argvs
    assert argv[0] == "rg" and "--files" not in argv
    # Paths only: --json would carry the matched text of every file (205 MB for
    # a repository of 28 thousand files, over workspace-mcp's 64 MiB cap).
    assert "--json" not in argv
    assert {"--files-with-matches", "-e", "--"} <= set(argv)
    assert argv[-1] == str(ROOT)
    assert set(argv[1 : argv.index("--")]) <= _LISTING_FLAGS | {"--files-with-matches"}


def test_paths_only_output_ignores_blank_lines_and_keeps_spaces() -> None:
    stdout = f"{ROOT}/a b.py\n\n{ROOT}/sub/c.py\n"

    paths = tim.list_ripgrep_files(_Ripgrep(paths_stdout=stdout), ROOT)

    assert sorted(paths) == ["a b.py", "sub/c.py"]


def test_a_path_outside_the_root_is_refused_in_either_listing() -> None:
    outside = json.dumps({"type": "begin", "data": {"path": {"text": "/elsewhere/x.py"}}})
    ripgrep = _Ripgrep(outside + "\n", paths_stdout="/elsewhere/x.py\n")

    with pytest.raises(tim.TrigramListingError):
        tim.list_ripgrep_files(ripgrep, ROOT)


def test_a_filename_with_a_newline_is_listed_by_the_json_listing() -> None:
    """One path per line cannot carry a newline in a name; --json escapes it. The
    paths-only output then reads as paths outside the root, and the JSON listing
    is used rather than failing the whole build."""
    ripgrep = _Ripgrep(_listing(["a.py", "odd\nname.py"]))

    assert sorted(tim.list_ripgrep_files(ripgrep, ROOT)) == ["a.py", "odd\nname.py"]

    first, second = ripgrep.argvs
    assert "--files-with-matches" in first and "--json" in second


def test_a_server_that_refuses_the_paths_only_listing_is_given_the_json_one() -> None:
    ripgrep = _Ripgrep(_listing(["a.py", "sub/b.java"]), refuses_paths_only=True)

    assert sorted(tim.list_ripgrep_files(ripgrep, ROOT)) == ["a.py", "sub/b.java"]

    first, second = ripgrep.argvs
    assert "--files-with-matches" in first and "--json" not in first
    assert "--json" in second and "--files-with-matches" not in second
    assert second[-1] == str(ROOT)


@pytest.mark.parametrize(
    "result",
    [
        _ok(exit_code=None, timed_out=True),
        _ok(exit_code=-9, output_capped=True),
        _ok(exit_code=2, stderr_output="boom", is_error=True),
    ],
)
def test_an_incomplete_paths_only_listing_is_not_retried_as_json(
    result: RipgrepRun,
) -> None:
    """A timeout, a cap or a real ripgrep error says nothing about the server
    refusing the argv, and the JSON listing is the larger of the two."""
    ripgrep = _Ripgrep(_listing(["a.py"]), result)

    with pytest.raises(tim.TrigramListingError):
        tim.list_ripgrep_files(ripgrep, ROOT)

    assert len(ripgrep.argvs) == 1


def test_listing_limits_fit_workspace_mcps_content_rg_limits() -> None:
    """EVO-64616: /content/rg rejects timeout_ms above 300000 and
    max_output_bytes above 67108864 with a 400, which failed every delegated
    trigram build while the listing asked for 600 s / 256 MiB."""
    ripgrep = _Ripgrep(_listing(["a.py"]))
    tim.list_ripgrep_files(ripgrep, ROOT)
    (timeout,) = ripgrep.timeouts
    assert 0 < timeout * 1000 <= 300_000
    (max_bytes,) = ripgrep.max_output_bytes
    assert 0 < max_bytes <= 64 * 1024 * 1024


def test_listing_limits_are_not_capped_for_a_local_rg() -> None:
    """Only the delegated boundary is subject to workspace-mcp's ceiling."""

    class _LocalRipgrep(_Ripgrep):
        @property
        def is_local(self) -> bool:
            return True

    ripgrep = _LocalRipgrep(_listing(["a.py"]))
    tim.list_ripgrep_files(ripgrep, ROOT)
    assert ripgrep.timeouts == [tim._RG_FILES_TIMEOUT_SECONDS]
    assert ripgrep.max_output_bytes == [tim._RG_FILES_MAX_OUTPUT_BYTES]
    assert tim._RG_FILES_TIMEOUT_SECONDS > tim._DELEGATED_RG_MAX_TIMEOUT_SECONDS
    assert tim._RG_FILES_MAX_OUTPUT_BYTES > tim._DELEGATED_RG_MAX_OUTPUT_BYTES


def test_listing_with_no_files_is_empty() -> None:
    assert tim.list_ripgrep_files(_Ripgrep("", _ok(exit_code=1)), ROOT) == []


@pytest.mark.parametrize(
    "result",
    [
        _ok(exit_code=None, timed_out=True),
        _ok(exit_code=-9, output_capped=True),
        _ok(exit_code=2, stderr_output="boom", is_error=True),
    ],
)
def test_an_incomplete_listing_is_refused(result: RipgrepRun) -> None:
    with pytest.raises(tim.TrigramListingError):
        tim.list_ripgrep_files(_Ripgrep(_listing(["a.py"]), result), ROOT)


def test_a_listed_path_outside_the_root_is_refused() -> None:
    stdout = json.dumps(
        {"type": "begin", "data": {"path": {"text": "/elsewhere/x.py"}}}
    )
    with pytest.raises(tim.TrigramListingError):
        tim.list_ripgrep_files(_Ripgrep(stdout + "\n"), ROOT)


# ── build ──────────────────────────────────────────────────────────────────


def test_change_detection_manifest_skips_the_hash() -> None:
    content = _Content({"a.py": b"alpha"}, {"a.py": 1})
    facts = _facts(content)
    assert facts["a.py"].change_key == "s5:m1"
    assert content.manifest_kwargs[0]["hash"] is False


def test_full_build_then_incremental_fetches_only_changes(tmp_path: Path) -> None:
    files = {
        "keep.py": b"def unchanged_needle(): pass\n",
        "edit.py": b"old_content_here\n",
        "gone.py": b"vanishing_needle\n",
    }
    content = _Content(dict(files), {p: 1 for p in files})
    index_dir = tmp_path / "trigram_index"
    manager = tim.TrigramIndexManager(index_dir)

    first = manager.build_delegated(content, sorted(files), ROOT, _facts(content))
    assert (first.files, first.fetched, first.carried) == (3, 3, 0)
    assert _candidates(index_dir, "unchanged_needle") == ["keep.py"]

    # Second refresh: edit.py changed, gone.py deleted, new.py added.
    content.files = {
        "keep.py": files["keep.py"],
        "edit.py": b"brand_new_needle\n",
        "new.py": b"fresh_needle_text\n",
    }
    content.mtimes = {"keep.py": 1, "edit.py": 2, "new.py": 2}
    content.fetched.clear()
    second = manager.build_delegated(
        content, sorted(content.files), ROOT, _facts(content)
    )

    assert sorted(content.fetched) == ["edit.py", "new.py"]
    assert (second.files, second.carried) == (3, 1)
    assert _candidates(index_dir, "unchanged_needle") == ["keep.py"]
    assert _candidates(index_dir, "brand_new_needle") == ["edit.py"]
    assert _candidates(index_dir, "fresh_needle_text") == ["new.py"]
    assert _candidates(index_dir, "vanishing_needle") == []
    assert _candidates(index_dir, "old_content_here") == []


def test_unreadable_and_oversized_files_are_always_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tim, "_MAX_INDEX_BYTES", 10)
    files = {"big.txt": b"x" * 50, "bad.py": b"zzz", "ok.py": b"needle_one"}
    content = _Content(files, {p: 1 for p in files})
    content.fail["bad.py"] = "EIO"
    index_dir = tmp_path / "trigram_index"
    tim.TrigramIndexManager(index_dir).build_delegated(
        content, sorted(files), ROOT, _facts(content)
    )

    assert "big.txt" not in content.fetched  # never moved, as locally on a stat
    assert _candidates(index_dir, "needle_one") == ["bad.py", "big.txt", "ok.py"]

    # A file that failed is fetched again next time, never carried unindexed.
    content.fail.clear()
    content.fetched.clear()
    tim.TrigramIndexManager(index_dir).build_delegated(
        content, sorted(files), ROOT, _facts(content)
    )
    assert content.fetched == ["bad.py"]


def test_a_transport_failure_keeps_the_current_index(tmp_path: Path) -> None:
    files = {"a.py": b"stable_needle"}
    content = _Content(files, {"a.py": 1})
    index_dir = tmp_path / "trigram_index"
    tim.TrigramIndexManager(index_dir).build_delegated(
        content, ["a.py"], ROOT, _facts(content)
    )

    class _Broken(_Content):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
            raise RuntimeError("workspace-mcp unreachable")
            yield  # pragma: no cover - makes this a generator

    broken = _Broken({"a.py": b"x", "b.py": b"y"}, {"a.py": 2, "b.py": 2})
    with pytest.raises(RuntimeError):
        tim.TrigramIndexManager(index_dir).build_delegated(
            broken, ["a.py", "b.py"], ROOT, _facts(broken)
        )
    assert _candidates(index_dir, "stable_needle") == ["a.py"]
    assert not list(index_dir.glob("*.building"))


# ── refresh and the lazy build ─────────────────────────────────────────────


def test_refresh_builds_through_the_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.refresh_scheduler import _build_source_trigram_index

    source = tmp_path / "golden-repos" / "example-repo"
    source.mkdir(parents=True)
    files = {"a.py": b"refresh_needle"}
    content = _Content(files, {"a.py": 1})
    listing = "".join(
        json.dumps({"type": "begin", "data": {"path": {"text": f"{source}/a.py"}}})
        + "\n"
    )
    ripgrep = _Ripgrep(listing)
    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "delegated")
    monkeypatch.setattr(wiring, "server_content_backend", lambda: content)
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)
    local_builds: List[Path] = []
    monkeypatch.setattr(
        tim.TrigramIndexManager,
        "build",
        _recording_build(local_builds),
    )

    _build_source_trigram_index("example-repo-global", str(source))

    assert local_builds == []
    assert ripgrep.argvs[0][-1] == str(source)
    assert _candidates(tim.trigram_index_dir(source), "refresh_needle") == ["a.py"]


@pytest.mark.parametrize("content_backend, builds", [("delegated", 0), ("local", 1)])
def test_no_lazy_build_under_delegated_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_backend: str,
    builds: int,
) -> None:
    """With everything else equal, ``local`` (the positive control) builds and
    ``delegated`` does not -- so the skip is the content backend's doing."""
    monkeypatch.setattr(tim, "trigram_prefilter_available", lambda: True)
    monkeypatch.setattr(
        wiring, "configured_content_backend_name", lambda: content_backend
    )
    monkeypatch.delenv("CIDX_TRIGRAM_LAZY_BUILD", raising=False)
    rs._lazy_build_in_progress.clear()
    rs._lazy_build_last_attempt.clear()
    built: List[Path] = []
    monkeypatch.setattr(
        tim.TrigramIndexManager,
        "build",
        _recording_build(built),
    )

    rs._maybe_trigger_lazy_index_build(tmp_path)
    deadline = time.monotonic() + 5
    while len(built) < builds and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)

    assert len(built) == builds
    if not builds:
        assert str(tmp_path) not in rs._lazy_build_in_progress


# ── search ─────────────────────────────────────────────────────────────────


def _match_line(path: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": "target_needle()\n"},
                "line_number": 1,
                "submatches": [
                    {"match": {"text": "target_needle"}, "start": 0, "end": 13}
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_search_reads_the_served_index_and_sends_lexical_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / ".versioned" / "example-repo" / "v_1"
    snapshot.mkdir(parents=True)
    tim.TrigramIndexManager(tim.trigram_index_dir(snapshot))._write_index(
        iter(
            [
                ("src/hit.py", tim._data_trigrams(b"target_needle()"), True),
                ("src/miss.py", tim._data_trigrams(b"unrelated"), True),
            ]
        )
    )
    monkeypatch.setattr(tim, "trigram_prefilter_available", lambda: True)
    ripgrep = _Ripgrep(_match_line(f"{ROOT}/src/hit.py") + "\n")

    svc = rs.RegexSearchService(ROOT, ripgrep_backend=ripgrep, trigram_root=snapshot)
    result = await svc.search(pattern="target_needle", max_results=5)

    argv = ripgrep.argvs[0]
    assert argv[argv.index("--") + 1 :] == [f"{ROOT}/src/hit.py"]
    assert [m.file_path for m in result.matches] == ["src/hit.py"]


@pytest.mark.asyncio
async def test_no_candidate_means_no_ripgrep_run_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index proves no file can match: answered without running ripgrep.
    An empty explicit-path list would make ripgrep walk the whole repository
    -- the very scan the pre-filter exists to avoid."""
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    tim.TrigramIndexManager(tim.trigram_index_dir(snapshot))._write_index(
        iter(
            [
                ("a.py", tim._data_trigrams(b"unrelated content"), True),
                ("b.py", tim._data_trigrams(b"other words"), True),
            ]
        )
    )
    monkeypatch.setattr(tim, "trigram_prefilter_available", lambda: True)
    ripgrep = _Ripgrep(_match_line(f"{ROOT}/a.py") + "\n")

    svc = rs.RegexSearchService(ROOT, ripgrep_backend=ripgrep, trigram_root=snapshot)
    assert svc._prefilter_candidate_files("target_needle", ROOT, None, True) == []
    result = await svc.search(pattern="target_needle", max_results=5)

    assert ripgrep.argvs == []
    assert result.matches == [] and result.total_matches == 0
    assert result.truncated is False


@pytest.mark.asyncio
async def test_a_vanished_candidate_does_not_fail_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    tim.TrigramIndexManager(tim.trigram_index_dir(snapshot))._write_index(
        iter(
            [
                ("a.py", tim._data_trigrams(b"target_needle()"), True),
                ("gone.py", tim._data_trigrams(b"target_needle()"), True),
            ]
        )
    )
    monkeypatch.setattr(tim, "trigram_prefilter_available", lambda: True)
    stderr = (
        f"rg: {ROOT}/gone.py: IO error for operation on {ROOT}/gone.py: "
        "No such file or directory (os error 2)"
    )
    ripgrep = _Ripgrep(
        _match_line(f"{ROOT}/a.py") + "\n",
        _ok(exit_code=2, stderr_output=stderr, is_error=True),
    )

    svc = rs.RegexSearchService(ROOT, ripgrep_backend=ripgrep, trigram_root=snapshot)
    result = await svc.search(pattern="target_needle", max_results=5)

    assert [m.file_path for m in result.matches] == ["a.py"]


def test_trigram_root_defaults_to_the_repository(tmp_path: Path) -> None:
    svc = rs.RegexSearchService(tmp_path, ripgrep_backend=_Ripgrep())
    assert svc._trigram_root == svc.repo_path


def _capture_service(monkeypatch: pytest.MonkeyPatch) -> Tuple[Dict[str, Any], Any]:
    captured: Dict[str, Any] = {}

    def fake(root: Path, **kwargs: Any) -> Any:
        captured["root"] = root
        captured.update(kwargs)
        raise RuntimeError("stop after construction")

    return captured, fake


@pytest.mark.asyncio
async def test_rest_route_passes_the_alias_target_as_trigram_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.routes import regex_routes

    base = tmp_path / "base"
    captured, fake = _capture_service(monkeypatch)
    monkeypatch.setattr(regex_routes, "RegexSearchService", fake)
    monkeypatch.setattr(
        regex_routes, "server_regex_target", lambda p: (base, _Ripgrep())
    )
    body = type(
        "B",
        (),
        {
            "pattern": "x",
            "path": None,
            "include_patterns": None,
            "exclude_patterns": None,
        },
    )()
    with pytest.raises(RuntimeError, match="stop after construction"):
        await regex_routes._execute_single_search(body, str(tmp_path))
    assert captured["root"] == base
    assert captured["trigram_root"] == tmp_path


@pytest.mark.asyncio
async def test_mcp_handler_passes_the_alias_target_as_trigram_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from code_indexer.global_repos import regex_search as regex_module
    from code_indexer.server.mcp.handlers.search import _execute_regex_search_impl
    from code_indexer.server.mcp.handlers.search import regex_search as handler

    base = tmp_path / "base"
    captured, fake = _capture_service(monkeypatch)
    monkeypatch.setattr(regex_module, "RegexSearchService", fake)
    monkeypatch.setattr(rg_wiring, "server_regex_target", lambda p: (base, _Ripgrep()))
    config = MagicMock()
    config.search_limits_config.timeout_seconds = 30
    config.background_jobs_config.subprocess_max_workers = 2
    monkeypatch.setattr(
        handler, "get_config_service", lambda: MagicMock(get_config=lambda: config)
    )

    with pytest.raises(RuntimeError, match="stop after construction"):
        await _execute_regex_search_impl(
            {"pattern": "x"}, tmp_path, "example-repo-global", MagicMock()
        )
    assert captured["root"] == base
    assert captured["trigram_root"] == tmp_path
