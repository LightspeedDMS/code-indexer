"""Attribution of embedding_call_stats rows to the indexing work that caused
them (Story #2118 slice 1a).

Every embedding call made by indexing that the SERVER starts must be
recorded with the golden-repo alias, the job id and the node id, so per-alias
index spend can be computed from ``embedding_call_stats``. Server indexing
runs in a ``cidx index`` child process: the server passes a
``StatsAttribution`` as one ``--stats-attribution=<json>`` argument (stamped
by ``server/utils/index_command_layout.append_server_layout_args``, which
requires it). The child validates it before any provider call and installs
it as the PROCESS-WIDE default in ``embedding_call_instrumentation``.

``purpose`` is ``index`` (initial registration, add-index, user reindex) or
``refresh`` (a golden-repo refresh re-indexing an existing collection). In
the child, a row the provider labels ``index`` records the attribution's
purpose; a row labelled ``temporal`` (the contextual temporal embedder)
records ``temporal_refresh`` when the attribution purpose is ``refresh``
and stays ``temporal`` otherwise, so refresh-time temporal spend is
countable apart from registration-time temporal spend.

This module is imported by the ``cidx index`` child, so it must stay
dependency-light: the server node-id lookup is imported lazily inside
``build_server_stats_attribution``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Optional

#: Purposes an attribution may carry. Both are members of
#: embedding_call_stats.VALID_PURPOSES.
ATTRIBUTION_PURPOSES = frozenset({"index", "refresh"})

#: The CLI option the server uses to hand an attribution to a `cidx index`
#: child (single ``--opt=value`` token, see index_command_layout.py).
STATS_ATTRIBUTION_OPTION = "--stats-attribution"

_FIELD_NAMES = ("golden_repo_alias", "job_id", "node_id", "purpose")


@dataclass(frozen=True)
class StatsAttribution:
    """Who caused an embedding call: alias, job, node and purpose.

    Every field is a non-empty string; ``purpose`` is one of
    ``ATTRIBUTION_PURPOSES``. Construction raises ``ValueError`` otherwise.
    """

    golden_repo_alias: str
    job_id: str
    node_id: str
    purpose: str

    def __post_init__(self) -> None:
        for name in _FIELD_NAMES:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"stats attribution field {name!r} must be a non-empty "
                    f"string, got {value!r}"
                )
        if self.purpose not in ATTRIBUTION_PURPOSES:
            raise ValueError(
                f"stats attribution purpose must be one of "
                f"{sorted(ATTRIBUTION_PURPOSES)}, got {self.purpose!r}"
            )

    def to_json(self) -> str:
        """Compact JSON object with exactly the four fields."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "StatsAttribution":
        """Parse ``to_json`` output. Raises ``ValueError`` on anything else:
        invalid JSON, a non-object, a missing or unknown key, or a field
        that fails validation."""
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"stats attribution is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("stats attribution must be a JSON object")
        expected = {f.name for f in fields(cls)}
        if set(data) != expected:
            raise ValueError(
                f"stats attribution keys must be exactly {sorted(expected)}, "
                f"got {sorted(data)}"
            )
        return cls(**data)


def nojob_id(site: str) -> str:
    """The job id recorded for indexing that runs outside any background
    job: ``nojob:<site>``, naming the spawn site."""
    if not site:
        raise ValueError("nojob_id requires a non-empty site name")
    return f"nojob:{site}"


def build_server_stats_attribution(
    *, alias: str, job_id: Optional[str], site: str, purpose: str
) -> StatsAttribution:
    """Attribution for indexing started by THIS server process.

    ``job_id`` is the background job's id, or None when the work runs outside
    a job, in which case ``nojob:<site>`` is recorded. The node id is the
    job-ownership identity (what JobTracker stamps as ``executing_node``:
    ``cluster.node_id``, else ``<hostname>-cidx``), so stats rows join to
    jobs and nodes.
    """
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.utils.cluster_node_id import (
        resolve_server_config_node_id,
    )

    return StatsAttribution(
        golden_repo_alias=alias,
        job_id=job_id if job_id is not None else nojob_id(site),
        node_id=resolve_server_config_node_id(get_config_service().get_config()),
        purpose=purpose,
    )
