from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

DEFAULT_CORPUS_PATH = Path(__file__).resolve().parent / "corpus.yaml"

SEARCH_MODES = ("semantic", "fts", "regex", "xray")
FILE_MODES = ("files_list", "file_read")
QUERY_MODES = SEARCH_MODES + FILE_MODES
FANOUT_MODES = ("semantic", "fts", "regex")


class CorpusError(Exception):
    pass


@dataclass(frozen=True)
class Query:
    mode: str
    expect: str
    query: str = ""
    path: str = ""
    include: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoSpec:
    url: str
    probe: str
    queries: Tuple[Query, ...]


@dataclass(frozen=True)
class FanoutSpec:
    mode: str
    query: str
    expect: Dict[str, str]


@dataclass(frozen=True)
class Corpus:
    top_n: int
    xray_evaluator: str
    repos: Tuple[RepoSpec, ...]
    fanout: Tuple[FanoutSpec, ...] = field(default_factory=tuple)


def normalize_repo_url(url: str) -> str:
    text = url.strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    else:
        if text.startswith("git@") and ":" in text:
            host, _, rest = text[len("git@") :].partition(":")
            text = f"{host}/{rest}"
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.rstrip("/")


def resolve_aliases(
    manifest_urls: Sequence[str], global_repos: Sequence[Mapping[str, Any]]
) -> Tuple[Dict[str, str], List[str]]:
    by_url: Dict[str, str] = {}
    for entry in global_repos:
        alias = entry.get("alias_name")
        repo_url = entry.get("repo_url")
        # ^ why skip, not raise; one odd row on a large fleet must not abort the run
        if not isinstance(alias, str) or not alias or not isinstance(repo_url, str):
            continue
        by_url[normalize_repo_url(repo_url)] = alias
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for url in manifest_urls:
        alias = by_url.get(normalize_repo_url(url))
        if alias is None:
            missing.append(url)
        else:
            resolved[url] = alias
    return resolved, missing


def hit_found(hits: Sequence[str], expect: str) -> bool:
    return any(hit == expect or hit.endswith("/" + expect) for hit in hits)


def _require(mapping: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, kind):
        raise CorpusError(f"{where}: '{key}' must be a {kind.__name__}, got {value!r}")
    return value


def _load_query(raw: Any, where: str) -> Query:
    if not isinstance(raw, dict):
        raise CorpusError(f"{where}: query entry must be a mapping")
    mode = _require(raw, "mode", str, where)
    if mode not in QUERY_MODES:
        raise CorpusError(f"{where}: unknown mode {mode!r}; allowed {QUERY_MODES}")
    expect = _require(raw, "expect", str, where)
    if mode in FILE_MODES:
        return Query(mode=mode, expect=expect, path=_require(raw, "path", str, where))
    include = raw.get("include", [])
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        raise CorpusError(f"{where}: 'include' must be a list of strings")
    return Query(
        mode=mode,
        expect=expect,
        query=_require(raw, "query", str, where),
        include=tuple(include),
    )


def _load_repo(raw: Any, index: int) -> RepoSpec:
    if not isinstance(raw, dict):
        raise CorpusError(f"repos[{index}] must be a mapping")
    url = _require(raw, "url", str, f"repos[{index}]")
    probe = _require(_require(raw, "probe", dict, url), "query", str, f"{url} probe")
    queries = tuple(
        _load_query(q, f"{url} queries[{i}]")
        for i, q in enumerate(_require(raw, "queries", list, url))
    )
    return RepoSpec(url=url, probe=probe, queries=queries)


def _load_fanout(raw: Any, index: int, repo_urls: Sequence[str]) -> FanoutSpec:
    where = f"fanout[{index}]"
    if not isinstance(raw, dict):
        raise CorpusError(f"{where} must be a mapping")
    mode = _require(raw, "mode", str, where)
    if mode not in FANOUT_MODES:
        raise CorpusError(f"{where}: unknown mode {mode!r}; allowed {FANOUT_MODES}")
    expect = _require(raw, "expect", dict, where)
    for url, path in expect.items():
        if url not in repo_urls or not isinstance(path, str):
            raise CorpusError(f"{where}: expect key {url!r} is not a corpus repo url")
    return FanoutSpec(
        mode=mode, query=_require(raw, "query", str, where), expect=dict(expect)
    )


def load_corpus(path: Path) -> Corpus:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise CorpusError(f"{path}: top level must be a mapping")
    top_n = _require(raw, "top_n", int, str(path))
    if top_n < 1:
        raise CorpusError(f"{path}: top_n must be >= 1")
    repos = tuple(
        _load_repo(r, i) for i, r in enumerate(_require(raw, "repos", list, str(path)))
    )
    if not repos:
        raise CorpusError(f"{path}: corpus has no repos")
    urls = [r.url for r in repos]
    fanout = tuple(
        _load_fanout(f, i, urls) for i, f in enumerate(raw.get("fanout") or [])
    )
    evaluator: Optional[str] = raw.get("xray_evaluator")
    has_xray = any(q.mode == "xray" for r in repos for q in r.queries)
    if has_xray and not isinstance(evaluator, str):
        raise CorpusError(f"{path}: xray queries need a string 'xray_evaluator'")
    return Corpus(
        top_n=top_n, xray_evaluator=evaluator or "", repos=repos, fanout=fanout
    )
