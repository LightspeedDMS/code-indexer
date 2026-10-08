#!/usr/bin/env python3
"""Story #2079: fail when any tracked file references a doc path that is missing.

Two checks, over GIT-TRACKED files only (``git ls-files``):

1. Markdown links. In every tracked ``*.md``, each inline link ``[text](target)``
   (images included) and each reference definition ``[id]: target`` whose target
   is a repository path. Targets with a URI scheme (``https:``, ``mailto:``...)
   and pure ``#anchor`` targets are skipped. ``#fragment`` and ``?query`` are
   stripped; the target resolves relative to the containing file (a leading
   ``/`` resolves from the repository root, as GitHub does). Links inside
   fenced code blocks and inline code spans are ignored.
2. Literal doc paths. Each ``docs/<path>.md`` / ``docs/<path>.rs`` (resolved
   from the repository root) in the RAW text -- inline code and fences
   included -- of tracked files under the CODE_SIDE_PREFIXES, the
   CODE_SIDE_ROOT_FILES, every root-level ``*.sh``, and every tracked ``*.md``
   outside PROSE_SCAN_EXCLUDED; plus each Rust ``include_str!("<path>")`` in
   code-side ``.rs`` files (resolved from the .rs file).

A target must exist inside the repository. Files that deliberately contain
fake doc paths (synthetic test repositories) are excluded by FIXTURE_ALLOWLIST,
which is keyed by FILE path -- never by value.

Exit status: 0 clean, 1 broken references found, 2 the checker could not run.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT_SECONDS = 60

_SYNTHETIC_REPO = "test builds a temp repository containing these fake doc paths"
_PATH_AS_DATA = "test passes fake repository-relative doc paths as plain data"
_BROKEN_BY_DESIGN = "edge-case corpus whose broken image links are the test subject"
_VALIDATOR_INPUT = 'include_str!("secret.txt") is validator test INPUT in a string'
_FIXTURE_REPO_README = "README of a fixture repo; its docs/ paths are the fixture's"

# Tracked files that build synthetic repositories or use fake doc paths as
# test data. Key: repository-relative file path. Value: why it is excluded.
# Derived by running this checker on the tree and inspecting every hit; remove
# an entry as soon as its file stops needing it.
FIXTURE_ALLOWLIST: Mapping[str, str] = {
    # Example values describing a USER repository's files, not this repo's docs.
    "src/code_indexer/logging/adaptive_logger.py": "docstring example log line",
    "src/code_indexer/server/mcp/tool_docs/git/git_file_history.md": (
        "example 'path' argument value for a user repository"
    ),
    "scripts/utils/create_test_repo.py": "generates a synthetic test repository",
    # This checker and its own tests: the source names example doc paths in
    # docstrings and regex comments; the tests build synthetic repos.
    "scripts/check_doc_references.py": "example doc paths in docstrings/comments",
    "tests/unit/scripts/test_check_doc_references.py": _SYNTHETIC_REPO,
    "rust/xray-core/src/validator.rs": _VALIDATOR_INPUT,
    "rust/xray-core/tests/s0a_validator_reject_sites.rs": _VALIDATOR_INPUT,
    "test-fixtures/versioned_fresh/v_fresh/README.md": _FIXTURE_REPO_README,
    "test-fixtures/versioned_test/v_123456/README.md": _FIXTURE_REPO_README,
    "tests/fixtures/cidx-test-repo/README.md": _FIXTURE_REPO_README,
    "test-fixtures/versioned_fresh/v_fresh/docs/edge-cases/code-blocks.md": (
        _BROKEN_BY_DESIGN
    ),
    "test-fixtures/versioned_fresh/v_fresh/docs/edge-cases/missing-image.md": (
        _BROKEN_BY_DESIGN
    ),
    "test-fixtures/versioned_fresh/v_fresh/docs/edge-cases/mixed-valid-invalid.md": (
        _BROKEN_BY_DESIGN
    ),
    "test-fixtures/versioned_test/v_123456/docs/edge-cases/code-blocks.md": (
        _BROKEN_BY_DESIGN
    ),
    "test-fixtures/versioned_test/v_123456/docs/edge-cases/missing-image.md": (
        _BROKEN_BY_DESIGN
    ),
    "test-fixtures/versioned_test/v_123456/docs/edge-cases/mixed-valid-invalid.md": (
        _BROKEN_BY_DESIGN
    ),
    "tests/unit/cli/test_cli_multimodal_query_integration.py": _PATH_AS_DATA,
    "tests/unit/global_repos/test_regex_search_glob_parity_1876.py": _SYNTHETIC_REPO,
    "tests/unit/global_repos/test_regex_search_multiline_glob_path_1876.py": (
        _SYNTHETIC_REPO
    ),
    "tests/unit/logging/test_adaptive_logger.py": _PATH_AS_DATA,
    "tests/unit/remote/test_staleness_detector.py": _PATH_AS_DATA,
    "tests/unit/server/mcp/test_wiki_url_enrichment.py": _PATH_AS_DATA,
    "tests/unit/server/query/extension_filter_env_2047.py": _SYNTHETIC_REPO,
    "tests/unit/server/query/test_extension_filter_2047.py": _PATH_AS_DATA,
    "tests/unit/server/services/test_file_crud_service_symlink_escape_1891.py": (
        _SYNTHETIC_REPO
    ),
    "tests/unit/server/wiki/test_wiki_cache_invalidator.py": _PATH_AS_DATA,
    "tests/unit/server/wiki/test_wiki_invalidation_hooks.py": _PATH_AS_DATA,
    "tests/unit/services/test_chunk_migration_cli_multimodal_1488.py": _PATH_AS_DATA,
    "tests/unit/services/test_claude_integration_gitignore_patterns.py": (
        _SYNTHETIC_REPO
    ),
    "tests/unit/services/test_multi_index_query_service.py": _PATH_AS_DATA,
    "tests/unit/services/test_multi_index_query_service_1496.py": _PATH_AS_DATA,
    "tests/unit/services/test_multi_index_query_service_separate_kwargs_1480.py": (
        _PATH_AS_DATA
    ),
    "tests/unit/services/test_override_filter_service.py": _PATH_AS_DATA,
    "tests/unit/services/test_path_pattern_matcher_selector_and_normalization_1876.py": (
        _PATH_AS_DATA
    ),
    "tests/unit/services/test_tantivy_fuzzy_snippet.py": _PATH_AS_DATA,
    "tests/unit/storage/test_filesystem_status_monitoring.py": _PATH_AS_DATA,
    "tests/unit/storage/test_filesystem_vector_store_subdirectory.py": _PATH_AS_DATA,
    "tests/unit/xray/test_search_engine_filename_glob_path_1876.py": _SYNTHETIC_REPO,
    "tests/unit/xray/test_search_engine_include_exclude_trigram_1876.py": (
        _SYNTHETIC_REPO
    ),
}

CODE_SIDE_PREFIXES: Tuple[str, ...] = (
    "src/",
    "rust/",
    "tests/",
    "scripts/",
    "tools/",
    ".github/",
)
CODE_SIDE_ROOT_FILES = frozenset({"CLAUDE.md", "lint.sh", "pyproject.toml"})

# Path prefixes whose Markdown PROSE is not scanned for docs/ paths (their
# links are still checked). These are historical records that must keep the
# old path names they describe.
PROSE_SCAN_EXCLUDED: Tuple[str, ...] = (
    "CHANGELOG.md",  # release history names paths as they were at the time
    "plans/",  # trackers and designs intentionally list old source paths
    "reports/",  # dated bug/review/troubleshooting records
)

_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+).+?\1")
_INLINE_LINK = re.compile(r"\]\(\s*(<[^>\n]*>|[^\s)]+)")
_REFERENCE_DEF = re.compile(r"^ {0,3}\[[^\]^][^\]]*\]:\s*(<[^>\n]*>|\S+)")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DOCS_PATH = re.compile(r"(?<![\w./-])docs/[\w./-]*?\.(?:md|rs)\b")
_INCLUDE_STR = re.compile(r'include_str!\s*\(\s*"([^"\\]+)"\s*\)')


class BrokenReference(NamedTuple):
    source: str  # repository-relative path of the referencing file
    line: int  # 1-based line of the reference
    target: str  # the target exactly as written


def _tracked_files(root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed in {root}: {detail}")
    # surrogateescape: a non-UTF-8 filename round-trips to its real bytes on disk.
    names = result.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in names.split("\0") if p]


def _printable(name: str) -> str:
    """Render a surrogateescape-decoded name safely for any stdout encoding."""
    return name.encode("utf-8", errors="surrogateescape").decode(
        "ascii", errors="backslashreplace"
    )


def _read_text(path: Path) -> Optional[str]:
    """Return the file's text, or None for a binary file."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


class _Repo(NamedTuple):
    root: Path
    known: FrozenSet[str]  # tracked files, their parent directories, and "."


def _load_repo(root: Path) -> Tuple[_Repo, List[str]]:
    files = _tracked_files(root)
    known = {"."}
    for rel in files:
        known.add(rel)
        parent = os.path.dirname(rel)
        while parent and parent not in known:
            known.add(parent)
            parent = os.path.dirname(parent)
    return _Repo(root, frozenset(known)), files


def _target_exists(repo: _Repo, base_dir: str, target: str) -> bool:
    """True when ``target`` (relative to ``base_dir``) is tracked and on disk.

    Tracked, because CI checks out only tracked files; on disk, because a
    tracked file deleted in the working tree is about to stop existing.
    A target escaping the repository normalizes to ``../..`` and is never
    in ``repo.known``.
    """
    if target.startswith("/"):
        rel = os.path.normpath(target.lstrip("/"))
    else:
        rel = os.path.normpath(os.path.join(base_dir, target))
    return rel in repo.known and (repo.root / rel).exists()


def _markdown_link_targets(text: str) -> Iterator[Tuple[int, str]]:
    """Yield (line, raw target) for links outside fenced code and inline code."""
    fence: Optional[str] = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        opener = _FENCE_OPEN.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0]:
                closing = line.strip()
                if len(closing) >= len(fence) and set(closing) == {fence[0]}:
                    fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        visible = _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line)
        ref = _REFERENCE_DEF.match(visible)
        if ref:
            yield line_no, ref.group(1)
        for link in _INLINE_LINK.finditer(visible):
            yield line_no, link.group(1)


def _check_markdown(repo: _Repo, rel: str, text: str) -> Iterator[BrokenReference]:
    base_dir = os.path.dirname(rel)
    for line_no, raw in _markdown_link_targets(text):
        target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        if target.startswith("#") or _URI_SCHEME.match(target):
            continue
        path = unquote(_strip_fragment_and_query(target))
        if path and not _target_exists(repo, base_dir, path):
            yield BrokenReference(rel, line_no, target)


def _is_code_side(rel: str) -> bool:
    if rel.startswith(CODE_SIDE_PREFIXES) or rel in CODE_SIDE_ROOT_FILES:
        return True
    return "/" not in rel and rel.endswith(".sh")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _check_docs_paths(repo: _Repo, rel: str, text: str) -> Iterator[BrokenReference]:
    for match in _DOCS_PATH.finditer(text):
        if not _target_exists(repo, "", match.group(0)):
            yield BrokenReference(rel, _line_of(text, match.start()), match.group(0))


def _check_include_str(repo: _Repo, rel: str, text: str) -> Iterator[BrokenReference]:
    base_dir = os.path.dirname(rel)
    for match in _INCLUDE_STR.finditer(text):
        if not _target_exists(repo, base_dir, match.group(1)):
            line = _line_of(text, match.start(1))
            yield BrokenReference(rel, line, match.group(1))


def _strip_fragment_and_query(target: str) -> str:
    return target.split("#", 1)[0].split("?", 1)[0]


def _dedupe(hits: List[BrokenReference]) -> List[BrokenReference]:
    """Collapse one broken path seen by two checks on the same line.

    A Markdown link ``[x](docs/a.md#part)`` is reported by the link check as
    ``docs/a.md#part`` and by the prose scan as ``docs/a.md``; keep the form
    as written (the longer one).
    """
    best: Dict[Tuple[str, int, str], BrokenReference] = {}
    for hit in hits:
        key = (hit.source, hit.line, _strip_fragment_and_query(hit.target))
        if key not in best or len(hit.target) > len(best[key].target):
            best[key] = hit
    return sorted(best.values())


def find_broken_references(
    root: Path, allowlist: Mapping[str, str] = FIXTURE_ALLOWLIST
) -> List[BrokenReference]:
    """Return every broken doc reference in the tracked files of ``root``."""
    repo, files = _load_repo(root)
    hits: List[BrokenReference] = []
    for rel in files:
        is_markdown = rel.endswith(".md")
        code_side = _is_code_side(rel)
        if rel in allowlist or not (is_markdown or code_side):
            continue
        path = root / rel
        if not path.is_file():  # tracked but deleted in the working tree
            continue
        text = _read_text(path)
        if text is None:
            continue
        if is_markdown:
            hits.extend(_check_markdown(repo, rel, text))
        scan_prose = is_markdown and not rel.startswith(PROSE_SCAN_EXCLUDED)
        if code_side or scan_prose:
            hits.extend(_check_docs_paths(repo, rel, text))
        if code_side and rel.endswith(".rs"):
            hits.extend(_check_include_str(repo, rel, text))
    return _dedupe(hits)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        hits = find_broken_references(root)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"check_doc_references: {exc}", file=sys.stderr)
        return 2
    if not hits:
        print("Doc reference check passed: no broken references.")
        return 0
    print(f"Doc reference check FAILED: {len(hits)} broken reference(s):")
    for hit in hits:
        print(f"  {_printable(hit.source)}:{hit.line} -> {_printable(hit.target)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
