from typing import List, Optional, Sequence

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    FieldType,
    conforms,
)
from code_indexer.server.services.audit_outcome import record_outcome

XRAY_EXECUTION_ACTION = "xray_evaluator_executed"

_SCHEMA = AUDIT_ACTION_CATALOG[XRAY_EXECUTION_ACTION].details_schema or {}


def _conforming(items: Sequence[str], list_type: FieldType) -> List[str]:
    # ^ invariant keeps items fitting the item type; result <= max_items
    item_type = list_type.item
    if item_type is None:
        raise AssertionError(f"{XRAY_EXECUTION_ACTION} detail is not a list type")
    return [item for item in items if conforms(item_type, item)][: list_type.max_items]


def record_xray_execution(
    *,
    username: str,
    repo_alias: Optional[str],
    evaluator_sha256s: Sequence[str],
    pattern_names: Sequence[str],
    succeeded: bool,
) -> None:
    # ^ security the evaluator source never enters audit details; hashes and names only
    record_outcome(
        actor=username,
        action_type=XRAY_EXECUTION_ACTION,
        target_type="repo",
        target_id=repo_alias,
        outcome="success" if succeeded else "failure",
        details={
            "evaluator_sha256s": _conforming(
                evaluator_sha256s, _SCHEMA["evaluator_sha256s"]
            ),
            "pattern_names": _conforming(pattern_names, _SCHEMA["pattern_names"]),
        },
    )
