"""The GitHub/GitLab token diagnostics never expose a stored token's leading
characters; at most the last 4 are shown.

Real ``DiagnosticsService`` over a real SQLite server database and the real
``CITokenManager`` it builds. The stored tokens fail the format check (as a
token saved before the format rules tightened would), so the check returns
its WARNING result without any network call.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticsService,
    DiagnosticStatus,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.utils.credential_redaction import DISPLAY_MASK_CHAR

# Neutral sample values that do NOT match the provider token formats.
GITHUB_LEGACY_TOKEN = "zqgh-example-legacy-sample-token-" + "Hb3w"
GITLAB_LEGACY_TOKEN = "zqgl-example-legacy-sample-token-" + "Lq8v"


def _service_with_stored_token(
    tmp_path: Path, platform: str, token: str
) -> DiagnosticsService:
    db_path = tmp_path / "data" / "cidx_server.db"
    db_path.parent.mkdir(parents=True)
    DatabaseSchema(str(db_path)).initialize_database()
    service = DiagnosticsService(db_path=str(db_path))
    manager = service._get_token_manager()
    # Stored as-is (bypassing today's format validation on save).
    backend = manager._sqlite_backend
    assert backend is not None
    backend.save_token(platform, manager._encrypt_token(token), None)
    stored = manager.get_token(platform)
    assert stored is not None and stored.token == token
    return service


def _assert_masked(result: DiagnosticResult, token: str) -> None:
    assert result.status == DiagnosticStatus.WARNING, result
    rendered = repr(result.details) + result.message
    assert token not in rendered
    assert token[:4] not in rendered
    assert token[:10] not in rendered
    assert result.details["masked_token"] == DISPLAY_MASK_CHAR * 8 + token[-4:]


@pytest.mark.asyncio
async def test_github_invalid_format_details_show_only_last_four(tmp_path) -> None:
    service = _service_with_stored_token(tmp_path, "github", GITHUB_LEGACY_TOKEN)
    _assert_masked(await service.check_github_token(), GITHUB_LEGACY_TOKEN)


@pytest.mark.asyncio
async def test_gitlab_invalid_format_details_show_only_last_four(tmp_path) -> None:
    service = _service_with_stored_token(tmp_path, "gitlab", GITLAB_LEGACY_TOKEN)
    _assert_masked(await service.check_gitlab_token(), GITLAB_LEGACY_TOKEN)


# --- Persisted rows carrying the retired ``token_prefix`` key ---------------

_LEGACY_PREFIX = "zqgh-examp"
_LEGACY_RESULTS = [
    {
        "name": "GitHub Token",
        "status": "warning",
        "message": "GitHub token has invalid format (expected ghp_* or github_pat_*)",
        "details": {"token_prefix": _LEGACY_PREFIX},
        "timestamp": "2026-10-01T10:00:00",
    }
]


def _served_external_api_details(service: DiagnosticsService) -> str:
    results = service.get_status()[DiagnosticCategory.EXTERNAL_APIS]
    assert any(r.name == "GitHub Token" for r in results), results
    return repr([r.details for r in results])


def test_legacy_sqlite_row_token_prefix_never_served(tmp_path) -> None:
    db_path = tmp_path / "data" / "cidx_server.db"
    db_path.parent.mkdir(parents=True)
    DatabaseSchema(str(db_path)).initialize_database()
    run_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO diagnostic_results "
            "(category, results_json, run_at) VALUES (?, ?, ?)",
            ("external_apis", json.dumps(_LEGACY_RESULTS), run_at),
        )
        conn.commit()

    service = DiagnosticsService(db_path=str(db_path))  # startup load path
    assert "token_prefix" not in _served_external_api_details(service)
    assert _LEGACY_PREFIX not in _served_external_api_details(service)

    service.clear_cache()  # forces the per-category DB read path
    assert "token_prefix" not in _served_external_api_details(service)


class _PgShapedDiagnosticsBackend:
    """Rows as psycopg returns them from PostgreSQL diagnostic_results:
    JSONB already a native list, TIMESTAMPTZ an aware datetime."""

    def __init__(self) -> None:
        self.run_at = datetime.now(timezone.utc)

    def load_all_results(self) -> List[Tuple[str, object, object]]:
        return [("external_apis", copy.deepcopy(_LEGACY_RESULTS), self.run_at)]

    def load_category_results(self, category: str) -> Optional[Tuple[object, object]]:
        if category != "external_apis":
            return None
        return copy.deepcopy(_LEGACY_RESULTS), self.run_at

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        raise AssertionError("a read must not write")


def test_legacy_pg_shaped_row_token_prefix_never_served(tmp_path) -> None:
    service = DiagnosticsService(
        db_path=str(tmp_path / "unused.db"),
        storage_backend=_PgShapedDiagnosticsBackend(),  # type: ignore[arg-type]
    )
    assert "token_prefix" not in _served_external_api_details(service)

    service.clear_cache()
    assert "token_prefix" not in _served_external_api_details(service)
