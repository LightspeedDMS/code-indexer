import contextvars
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from code_indexer.server.services import search_event_outcomes as outcomes
from code_indexer.server.services.xray_execution_audit import record_xray_execution
from code_indexer.server.services.xray_evaluator_sources import (
    HASH_SEPARATOR,
    PATTERN_NAME_SEPARATOR,
    EvaluatorSource,
    evaluator_source,
)


@dataclass(frozen=True)
class XRayEvaluator:
    # ^ invariant source = the exact code handed to the engine; pattern_name None => inline or default
    source: str
    pattern_name: Optional[str] = None


@dataclass(frozen=True)
class XRayIdentity:
    sources: Tuple[EvaluatorSource, ...]
    pattern_names: Tuple[str, ...]

    @property
    def column_sha256(self) -> Optional[str]:
        return HASH_SEPARATOR.join(s.sha256 for s in self.sources) or None

    @property
    def column_pattern_names(self) -> Optional[str]:
        return PATTERN_NAME_SEPARATOR.join(self.pattern_names) or None


def xray_identity(evaluators: Sequence[XRayEvaluator]) -> XRayIdentity:
    # ^ invariant distinct sources and names, first-seen order
    sources: Dict[str, EvaluatorSource] = {}
    names: Dict[str, None] = {}
    for evaluator in evaluators:
        stored = evaluator_source(evaluator.source)
        sources.setdefault(stored.sha256, stored)
        if evaluator.pattern_name:
            names.setdefault(evaluator.pattern_name, None)
    return XRayIdentity(tuple(sources.values()), tuple(names))


# (repository alias, match count, error class or None) of one X-Ray row.
XRayRow = Tuple[Optional[str], int, Optional[str]]


def xray_event_rows(
    repo_alias: Optional[str],
    result: Optional[Dict[str, Any]],
    cancelled: Optional[Callable[[], bool]],
    error: Optional[BaseException],
    batch_repositories: Optional[Sequence[str]],
) -> List[XRayRow]:
    # ^ invariant cancelled or rejected => no rows; a batch => one row per repository (#2111)
    if (cancelled is not None and cancelled()) or (
        result is not None and result.get("cancelled")
    ):
        return []
    repositories: List[Optional[str]] = (
        list(batch_repositories) if batch_repositories else [repo_alias]
    )
    if error is not None:
        error_class = outcomes.failure_error_class(error)
        if error_class is None:
            return []
        return [(alias, 0, error_class) for alias in repositories]
    if result is None:
        raise ValueError("record_xray_search needs a result or an error")
    if batch_repositories:
        return list(outcomes.xray_batch_repository_rows(result, batch_repositories))
    return [
        (
            repo_alias,
            len(result.get("matches") or []),
            outcomes.xray_result_outcome(result),
        )
    ]


def audit_xray_run(
    username: str,
    row: XRayRow,
    identity: XRayIdentity,
    audit_context: Optional[contextvars.Context],
) -> None:
    repo_alias, _count, error_class = row

    def _record() -> None:
        record_xray_execution(
            username=username,
            repo_alias=repo_alias,
            evaluator_sha256s=[s.sha256 for s in identity.sources],
            pattern_names=identity.pattern_names,
            succeeded=error_class is None,
        )

    if audit_context is None:
        _record()
    else:
        # ^ why copy(), not run on the bound context; one Context cannot be entered twice at once
        audit_context.copy().run(_record)
