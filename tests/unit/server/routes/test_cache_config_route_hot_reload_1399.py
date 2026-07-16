"""
Bug #1399 testing requirement: "A real route-level POST/display round-trip
test for at least the CRITICAL cache-family fix (mirror the pattern
established in #1397/#1398 -- a dataclass-round-trip alone is
insufficient)."

Mirrors test_search_timeouts_config_route_1398.py's exact structure: drives
the ACTUAL update_config_section() FastAPI route coroutine end-to-end
(route -> ConfigService -> live cache singleton), rather than calling
ConfigService.update_setting() directly. A test that only calls
update_setting() directly would NOT catch a route-layer regression (e.g. a
missing/renamed form field, a broken _validate_config_section, or a
skip_validation path that bypasses the hot-reload dispatch).
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Cache singleton fixture (mirrors test_cache_ttl_cleanup_hot_reload_1399.py)
# ---------------------------------------------------------------------------

INITIAL_TTL_MINUTES = 10.0
UPDATED_TTL_MINUTES = 3.0


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        instance.stop_background_cleanup()
        setattr(cache_module, attr, None)


def _seed_singletons(cache_module: ModuleType) -> None:
    from code_indexer.server.cache.hnsw_index_cache import (
        HNSWIndexCache,
        HNSWIndexCacheConfig,
    )

    cache_module._global_cache_instance = HNSWIndexCache(  # type: ignore[attr-defined]
        config=HNSWIndexCacheConfig(ttl_minutes=INITIAL_TTL_MINUTES)
    )


@pytest.fixture(autouse=True)
def _reset_and_seed_singletons() -> Iterator[None]:
    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    _seed_singletons(cache_module)
    yield
    _stop_and_clear_singletons(cache_module)


# ---------------------------------------------------------------------------
# Route-level helpers (mirrors test_search_timeouts_config_route_1398.py)
# ---------------------------------------------------------------------------


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    return ConfigService(config_manager=mgr)


def _build_fake_request(form_data: dict):
    from unittest.mock import AsyncMock, MagicMock
    from starlette.datastructures import ImmutableMultiDict

    req = MagicMock()
    req.session = {}
    items = list(form_data.items())
    multi = ImmutableMultiDict(items)
    req.form = AsyncMock(return_value=multi)
    return req


def _run_update_config_section(section: str, form_data: dict, svc):
    from fastapi.responses import HTMLResponse
    from code_indexer.server.web.routes import update_config_section

    fake_session = mock.MagicMock()
    fake_session.username = "admin"
    fake_session.role = "admin"

    req = _build_fake_request(form_data)

    def _fake_page_response(request, session, **kwargs):
        body = kwargs.get("error_message") or kwargs.get("success_message") or "ok"
        return HTMLResponse(content=body)

    with (
        mock.patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=fake_session,
        ),
        mock.patch(
            "code_indexer.server.web.routes.validate_login_csrf_token",
            return_value=True,
        ),
        mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ),
        mock.patch(
            "code_indexer.server.web.routes._create_config_page_response",
            side_effect=_fake_page_response,
        ),
    ):
        response = asyncio.run(
            update_config_section(
                request=req, section=section, csrf_token="dummy-token"
            )
        )
    return response


class TestCacheConfigRoutePostRoundTrip:
    def test_post_index_cache_ttl_minutes_persists_and_hot_reloads_live_singleton(
        self, tmp_path: Path
    ) -> None:
        """
        Drives the real update_config_section("cache", ...) route handler
        end-to-end with index_cache_ttl_minutes. Asserts BOTH:
        1. The DB-backed ConfigService value is persisted (route -> service).
        2. The LIVE HNSW cache singleton's ttl_minutes reflects the new
           value (service -> hot-reload), proving the full chain the
           operator actually depends on -- not just a dataclass round trip.
        """
        from code_indexer.server.cache import get_global_cache

        svc = _make_service(str(tmp_path))
        cache = get_global_cache()
        assert cache.config.ttl_minutes == INITIAL_TTL_MINUTES

        response = _run_update_config_section(
            "cache",
            {"index_cache_ttl_minutes": str(UPDATED_TTL_MINUTES)},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response: {body!r}"
        )

        assert svc.get_config().cache_config.index_cache_ttl_minutes == (
            UPDATED_TTL_MINUTES
        ), "index_cache_ttl_minutes must be persisted via the real route handler."

        assert get_global_cache().config.ttl_minutes == UPDATED_TTL_MINUTES, (
            "Bug #1399: POSTing index_cache_ttl_minutes through the real "
            "update_config_section() route handler must hot-reload the "
            "live HNSW cache singleton -- a dataclass round-trip alone "
            "would not catch a broken route -> hot-reload wiring."
        )
