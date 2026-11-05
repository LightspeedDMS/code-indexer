"""SCIP as an index type of the core's indexing pipeline (``index_builders()``).

The core's pipeline decides WHEN a repository's SCIP index is built (a golden
repository's refresh with ``enable_scip``, an add-index request, an activated
repository's reindex) and owns progress, the snapshot that publishes the
index and the ``enable_scip`` registry flag. It asks this builder to build:

==========================  ===============================================
``scip_backend``            What :func:`build` answers
==========================  ===============================================
``local`` (default)         ``run_locally`` with ``cidx scip generate``: the
                            core runs it in the repository (cancellable with
                            its job, no timeout: Bug #1218), as it always did
``delegated-plugin``        the  funnel's outcome: ``enqueued`` on
                            the serialized delegated build queue,
                            ``deduped``, ``fresh``, or ``skipped`` (fail-soft,
                            reason logged); never a local build
==========================  ===============================================

An activated (per-user) repository always builds locally
(``cidx scip generate --project <path> [--clear]``): the delegated backend
cannot address a per-user checkout ( CRITICAL-2).

With the feature not installed or not enabled the core has no builder for
``scip``: refreshes skip the step, an add-index request for it is refused.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Mapping

from cidx_sdk import IndexBuilderContribution, IndexBuildOutcome, IndexBuildRequest
from cidx_sdk.feature import (
    INDEX_BUILD_DEDUPED,
    INDEX_BUILD_ENQUEUED,
    INDEX_BUILD_FRESH,
    INDEX_BUILD_RUN_LOCALLY,
    INDEX_BUILD_SKIPPED,
)

from cidx_scip.discovery import is_dependency_path
from cidx_scip.indexers.factory import is_delegated_scip_backend
from cidx_scip.paths import scip_index_dir
from cidx_scip.server import runtime

logger = logging.getLogger(__name__)

#: The pipeline's name for the index.
INDEX_TYPE = "scip"

#: What the core runs for a golden repository under the local backend.
GOLDEN_COMMAND = ("cidx", "scip", "generate")


def _activated_command(repo_root: str, clear: bool) -> tuple:
    command = ("cidx", "scip", "generate", "--project", repo_root)
    return command + ("--clear",) if clear else command


def build(request: IndexBuildRequest) -> IndexBuildOutcome:
    """Route one repository's SCIP build (never raises: the funnel is
    fail-soft, and anything else becomes ``skipped`` with the reason logged)."""
    from cidx_scip.server.lifecycle import (
        ScipDelegationOutcome,
        funnel_golden_scip,
        funnel_scip_generation,
    )

    try:
        config = runtime.backend_config()
        if request.activated:
            #  CRITICAL-2: an activated alias is not a golden alias;
            # MAJOR-5: ``clear`` reaches the delegated path too.
            outcome = funnel_scip_generation(
                alias=request.alias,
                repo_root=Path(request.repo_root),
                config=config,
                clear=request.clear,
                activated_repo=True,
            )
        else:
            # the -global -> bare normalization is the funnel's.
            outcome = funnel_golden_scip(
                request.alias, request.repo_root, config, clear=request.clear
            )
    except Exception as exc:
        logger.warning(
            "SCIP index build of '%s' skipped: %s", request.alias, exc, exc_info=True
        )
        return IndexBuildOutcome(INDEX_BUILD_SKIPPED, detail=str(exc))
    if outcome is ScipDelegationOutcome.NOT_DELEGATED:
        command = (
            _activated_command(request.repo_root, request.clear)
            if request.activated
            else GOLDEN_COMMAND
        )
        return IndexBuildOutcome(INDEX_BUILD_RUN_LOCALLY, command)
    status = {
        ScipDelegationOutcome.ENQUEUED: INDEX_BUILD_ENQUEUED,
        ScipDelegationOutcome.DEDUPED: INDEX_BUILD_DEDUPED,
        ScipDelegationOutcome.FRESH: INDEX_BUILD_FRESH,
    }.get(outcome, INDEX_BUILD_SKIPPED)
    return IndexBuildOutcome(status, detail=f"SCIP delegated backend: {outcome.value}")


def builds_locally() -> bool:
    """Whether a SCIP build runs on this pod (``scip_backend=local``)."""
    return not is_delegated_scip_backend(runtime.scip_backend())


def index_present(repo_root: str) -> bool:
    """Whether *repo_root* holds a SCIP index: at least one ``.scip.db``
    under its SCIP directory (a multi-project repository nests one per
    project), not counting dependency packages' databases."""
    scip_dir = scip_index_dir(repo_root)
    if not scip_dir.is_dir():
        return False
    return any(
        not is_dependency_path(db_file, scip_dir)
        for db_file in scip_dir.rglob("*.scip.db")
    )


def annotate_status(alias: str, entry: Mapping[str, object]) -> Mapping[str, object]:
    """``get_index_status``'s scip entry with the alias's latest delegated
    build: a queued/running build shows ``BUILDING``, a failed one
    ``FAILED`` with its reason, while no index is readable here."""
    from cidx_scip.server.build_status import with_build_state

    try:
        annotated: Dict[str, Any] = with_build_state(dict(entry), alias)
    except Exception as exc:  # a status read never fails
        logger.warning("SCIP build state of '%s' unavailable: %s", alias, exc)
        return entry
    return annotated


SCIP_INDEX_BUILDER = IndexBuilderContribution(
    index_type=INDEX_TYPE,
    build=build,
    builds_locally=builds_locally,
    index_present=index_present,
    annotate_status=annotate_status,
)
