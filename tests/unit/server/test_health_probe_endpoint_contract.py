"""Health checks use the unauthenticated /healthz endpoint; /docs requires login.

``scripts/install-cidx-server.sh`` (post-start check) and the HAProxy
``option httpchk GET /healthz`` both rely on this contract: an unauthenticated
probe of ``GET /healthz`` is answered with the node's health (200 serviceable,
503 unhealthy -- never an auth refusal), while an unauthenticated ``GET /docs``
never answers 200 (it redirects to the login page), so it can never serve as a
health probe.

Drives a real ``create_app()`` app over an isolated server home
(``isolated_app``) with no credentials of any kind. The real system-health
computation depends on the host (disk/RAM pressure can make it 503), so the
reachability assertions accept either health answer and the 200 mapping is
checked with the computed status pinned.
"""

import importlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.models.api_models import HealthStatus
from tests.unit.server._isolated_app import isolated_app

HEALTHZ_PATH = "/healthz"
DOCS_PATH = "/docs"
HEALTH_ANSWERS = {200, 503}
# The running app's routes reference the unprefixed module objects (see
# test_healthz_liveness_endpoint_1433.py for the dual-import-path note).
INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"
DB_HEALTH_MODULE_PATH = "code_indexer.server.services.database_health_service"


@contextmanager
def _anon_client(root: Path) -> Iterator[TestClient]:
    """A credential-free client over a fresh isolated app under *root*."""
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    # Imported before isolated_app captures process state, so its exit
    # restore puts the caller's database-health singleton back.
    db_health = importlib.import_module(DB_HEALTH_MODULE_PATH)
    # /healthz keeps a short-TTL status cache at module level; start clean so
    # no status cached by another test in this process is observed here.
    inline_misc._reset_healthz_cache()
    try:
        with isolated_app(root) as app:
            # isolated_app restores globals on exit but does not clear them on
            # entry: drop any database-health singleton bound to another
            # server dir so /healthz builds one under the isolated home.
            db_health._reset_singleton_for_testing()
            client = TestClient(app, follow_redirects=False)
            assert "Authorization" not in client.headers
            yield client
    finally:
        inline_misc._reset_healthz_cache()


@pytest.fixture(scope="module")
def anon_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with _anon_client(tmp_path_factory.mktemp("health-probe-contract")) as client:
        yield client


class TestUnauthenticatedHealthProbeContract:
    def test_healthz_answers_health_not_auth_refusal(self, anon_client):
        resp = anon_client.get(HEALTHZ_PATH)

        assert resp.status_code in HEALTH_ANSWERS, resp.text
        assert set(resp.json().keys()) == {"status"}

    def test_healthz_answers_200_without_authentication_when_healthy(
        self, anon_client, monkeypatch
    ):
        inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
        monkeypatch.setattr(
            inline_misc, "_get_healthz_status", lambda: HealthStatus.HEALTHY
        )

        resp = anon_client.get(HEALTHZ_PATH)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "healthy"}

    def test_docs_does_not_answer_200_without_authentication(self, anon_client):
        resp = anon_client.get(DOCS_PATH)

        assert resp.status_code != 200
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


def test_stale_database_health_singleton_is_not_reused(tmp_path, monkeypatch):
    """A database-health singleton left by earlier code in this process (bound
    to another server dir) must not answer the isolated app's /healthz."""
    db_health = importlib.import_module(DB_HEALTH_MODULE_PATH)
    missing_dir = tmp_path / "missing-server-dir"
    callers_singleton = db_health.DatabaseHealthService(server_dir=str(missing_dir))
    monkeypatch.setattr(db_health, "_db_health_service_instance", callers_singleton)
    root = tmp_path / "isolated"

    with _anon_client(root) as client:
        resp = client.get(HEALTHZ_PATH)
        in_use = db_health._db_health_service_instance

    assert resp.status_code in HEALTH_ANSWERS, resp.text
    assert in_use is not None
    assert Path(in_use.server_dir).is_relative_to(root), in_use.server_dir
    assert not missing_dir.exists()
    # The isolated app's exit restore puts the caller's singleton back.
    assert db_health._db_health_service_instance is callers_singleton
