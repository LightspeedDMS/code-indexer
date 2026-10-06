"""The LLM-creds provider API key follows the configuration page's "a blank
secret means keep the stored value" rule, like every other stored secret.

Front doors: the real app (``create_app`` via ``isolated_app``, never
~/.cidx-server) with

- an admin Bearer JWT from ``POST /auth/login`` (what the page's own
  ``fetch()`` sends) for ``POST /api/llm-creds/save-config``;
- a real admin Web login for the ``GET /admin/partials/config-section``
  page that renders the key field.

Configuration lives where a server worker keeps it: a real ``ConfigService``
over the isolated server dir with its runtime row in the app's
``cidx_server.db`` (what ``service_init`` wires; the root conftest resets the
singleton per test, so each test installs one). Only the lease lifecycle,
which talks to the external llm-creds-provider service, is replaced by a
recorder at its construction seam (``_build_lifecycle_service``) so no
network call is made.
"""

from __future__ import annotations

import logging
import re
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Union

import h11
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.services.llm_creds_client import (
    LlmCredsAuthError,
    LlmCredsProviderError,
)

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.routers import llm_creds
from code_indexer.server.services.config_service import (
    ConfigService,
    get_config_service,
    set_config_service,
)
from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfig,
)
from code_indexer.utils.credential_redaction import mask_stored_secret
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-LlmCreds-Admin-Passw0rd!"
# Neutral sample values (never real credentials).
STORED_KEY = "example-stored-provider-key-0000Ab12"
NEW_KEY = "example-new-provider-key-1111Cd34"
# A stored key with an interior control character (written by an older writer).
CTRL_KEY = "example-ctrl-provider-key\n0000Ef56"
OLD_URL = "https://creds-old.example.com"
NEW_URL = "https://creds-new.example.com"
KEY_HINT = "A key is set; leave empty to keep it"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
# The Provider API Key <label> on the config page (input plus its hint).
_KEY_FIELD = re.compile(r'<label for="llm-provider-api-key">(.*?)</label>', re.DOTALL)

# JSON payloads returned by the routes under test.
Payload = Dict[str, Union[str, bool, None]]


class _FakeLifecycle:
    """Records start/stop; reports an active status. ``start_error`` (when a
    test sets it) is raised by start(), as a failing provider checkout."""

    start_error: Optional[Exception] = None

    class _Status:
        class _Value:
            value = "active"

        status = _Value()

    def __init__(self) -> None:
        self.started_with: List[str] = []
        self.stopped = False

    def start(self, consumer_id: str) -> None:
        self.started_with.append(consumer_id)
        if _FakeLifecycle.start_error is not None:
            raise _FakeLifecycle.start_error

    def stop(self) -> None:
        self.stopped = True

    def get_status(self) -> "_FakeLifecycle._Status":
        return self._Status()


class _Session:
    """The module's app plus its two authenticated clients."""

    def __init__(self, app: FastAPI, api: TestClient, web: TestClient, token: str):
        self.app = app
        self.api = api
        self.web = web
        self.headers = {"Authorization": f"Bearer {token}"}


class _Build:
    """One ``_build_lifecycle_service`` call the route made."""

    def __init__(self, provider_url: str, api_key: str) -> None:
        self.provider_url = provider_url
        self.api_key = api_key
        self.svc = _FakeLifecycle()


@pytest.fixture(scope="module")
def admin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("llm-creds-blank-key")
    with isolated_app(root) as app:
        name = f"admin-{uuid.uuid4().hex[:8]}"
        app.state.user_manager.create_user(name, PASSWORD, UserRole.ADMIN)
        api = TestClient(app, follow_redirects=False)
        login = api.post("/auth/login", json={"username": name, "password": PASSWORD})
        assert login.status_code == HTTPStatus.OK, login.text

        web = TestClient(app, follow_redirects=False)
        csrf = _CSRF.search(web.get("/login").text)
        assert csrf, "csrf token not found on the login page"
        web_login = web.post(
            "/login",
            data={"username": name, "password": PASSWORD, "csrf_token": csrf.group(1)},
        )
        assert web_login.status_code == HTTPStatus.SEE_OTHER, web_login.status_code
        yield _Session(app, api, web, login.json()["access_token"])


def _worker_config_service(app: FastAPI) -> ConfigService:
    """A server worker's ConfigService: the isolated server dir with its
    runtime row in the app's cidx_server.db (what service_init wires)."""
    db_path = Path(app.state.user_manager._sqlite_backend._conn_manager.db_path)
    service = ConfigService(server_dir_path=str(db_path.parent.parent))
    service.initialize_runtime_db(str(db_path))
    return service


@pytest.fixture(autouse=True)
def _per_test_state(admin: _Session) -> Iterator[None]:
    set_config_service(_worker_config_service(admin.app))
    admin.app.state.llm_lifecycle_service = None
    yield
    admin.app.state.llm_lifecycle_service = None


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> List[_Build]:
    """Every lifecycle the route builds, with the credentials it was given."""
    calls: List[_Build] = []

    def _record(provider_url: str, api_key: str) -> _FakeLifecycle:
        calls.append(_Build(provider_url, api_key))
        return calls[-1].svc

    monkeypatch.setattr(llm_creds, "_build_lifecycle_service", _record)
    monkeypatch.setattr(_FakeLifecycle, "start_error", None)
    return calls


def _seed(mode: str, url: str, key: str) -> None:
    """Commit the starting LLM-creds settings through the real service."""

    def _mutate(candidate: ServerConfig) -> None:
        integration = candidate.claude_integration_config
        assert integration is not None
        integration.claude_auth_mode = mode
        integration.llm_creds_provider_url = url
        integration.llm_creds_provider_api_key = key
        integration.llm_creds_provider_consumer_id = "cidx-server"

    get_config_service().apply_system_change(_mutate)


def _stored() -> ClaudeIntegrationConfig:
    """This process's (cached) view of the settings."""
    integration = get_config_service().get_config().claude_integration_config
    assert integration is not None
    return integration


def _committed() -> Dict[str, str]:
    """The settings as committed to the shared runtime row."""
    _version, section = get_config_service().read_committed_section(
        "claude_integration_config"
    )
    return section


def _commit_underneath(app: FastAPI, **fields: str) -> None:
    """Another worker commits *fields* (ClaudeIntegrationConfig attributes)
    to the shared runtime row; this process's cached configuration is NOT
    refreshed (SQLite workers never reload)."""

    def _mutate(candidate: ServerConfig) -> None:
        integration = candidate.claude_integration_config
        assert integration is not None
        for name, value in fields.items():
            assert hasattr(integration, name), name
            setattr(integration, name, value)

    _worker_config_service(app).apply_system_change(_mutate)


def _save(admin: _Session, **body: str) -> Payload:
    with enforcement(False):
        response = admin.api.post(
            "/api/llm-creds/save-config", json=body, headers=admin.headers
        )
    assert response.status_code == HTTPStatus.OK, response.text
    payload: Payload = response.json()
    return payload


def _config_section(admin: _Session) -> str:
    response = admin.web.get("/admin/partials/config-section")
    assert response.status_code == HTTPStatus.OK, response.status_code
    assert STORED_KEY not in response.text, "raw provider key rendered"
    assert mask_stored_secret(STORED_KEY) not in response.text, "key mask rendered"
    return response.text


def _key_field_html(admin: _Session) -> str:
    match = _KEY_FIELD.search(_config_section(admin))
    assert match, "Provider API Key field not found on the config page"
    return match.group(1)


@pytest.mark.timeout(180)
def test_test_connection_button_requires_only_the_url(admin) -> None:
    """The page lets Test Connection run with the key field empty (the server
    then tests with the stored key)."""
    _seed("subscription", OLD_URL, STORED_KEY)
    page = _config_section(admin)

    assert "function testLlmConnection()" in page
    assert "Enter URL and API key first" not in page
    assert "Enter the provider URL first" in page


@pytest.mark.timeout(180)
def test_blank_key_on_api_key_mode_save_keeps_stored_key(admin, built) -> None:
    """Scenario 1: switching to "API Key (static)" with the key field empty."""
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is True, result
    assert _stored().claude_auth_mode == "api_key"
    assert _stored().llm_creds_provider_api_key == STORED_KEY


@pytest.mark.timeout(180)
def test_whitespace_key_keeps_stored_key(admin, built) -> None:
    _seed("api_key", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key="   ",
    )

    assert result["success"] is True, result
    assert _stored().llm_creds_provider_api_key == STORED_KEY


@pytest.mark.timeout(180)
def test_masked_display_value_keeps_stored_key(admin, built) -> None:
    _seed("api_key", OLD_URL, STORED_KEY)
    masked = mask_stored_secret(STORED_KEY)
    assert masked and masked != STORED_KEY

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key=masked,
    )

    assert result["success"] is True, result
    assert _stored().llm_creds_provider_api_key == STORED_KEY


@pytest.mark.timeout(180)
def test_new_key_replaces_stored_key(admin, built) -> None:
    _seed("api_key", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key=NEW_KEY,
    )

    assert result["success"] is True, result
    assert _stored().llm_creds_provider_api_key == NEW_KEY


@pytest.mark.timeout(180)
def test_submitted_key_is_stored_stripped(admin, built) -> None:
    """A pasted key's trailing newline is not part of the key."""
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key=NEW_KEY + "\n",
    )

    assert result["success"] is True, result
    assert _stored().llm_creds_provider_api_key == NEW_KEY
    assert [call.api_key for call in built] == [NEW_KEY]


@pytest.mark.timeout(180)
def test_subscription_resave_same_url_blank_key_keeps_stored_key(admin, built) -> None:
    """Scenario 2: changing only the consumer id must not force re-entering
    the key; the restarted lifecycle authenticates with the stored key."""
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=OLD_URL + "  ",
        llm_creds_provider_api_key="",
        llm_creds_provider_consumer_id="example-consumer",
    )

    assert result["success"] is True, result
    assert _stored().llm_creds_provider_consumer_id == "example-consumer"
    assert _stored().llm_creds_provider_api_key == STORED_KEY
    assert len(built) == 1, built
    assert built[0].api_key == STORED_KEY
    assert built[0].svc.started_with == ["example-consumer"]


@pytest.mark.timeout(180)
@pytest.mark.parametrize("mode", ["subscription", "api_key"])
@pytest.mark.parametrize("field", ["", mask_stored_secret(STORED_KEY)])
def test_url_change_without_a_new_key_is_refused(
    admin, built, mode: str, field: str
) -> None:
    """The stored key is bound to the stored provider URL: it is never kept
    for (and so never sent to) another host."""
    _seed(mode, OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode=mode,
        llm_creds_provider_url=NEW_URL,
        llm_creds_provider_api_key=field,
    )

    assert result["success"] is False, result
    assert "when changing the provider URL" in str(result["error"])
    committed = _committed()
    assert committed["claude_auth_mode"] == mode
    assert committed["llm_creds_provider_url"] == OLD_URL
    assert committed["llm_creds_provider_api_key"] == STORED_KEY
    assert built == []


@pytest.mark.timeout(180)
def test_url_change_with_a_new_key_is_accepted(admin, built) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=NEW_URL,
        llm_creds_provider_api_key=NEW_KEY,
    )

    assert result["success"] is True, result
    assert _committed()["llm_creds_provider_url"] == NEW_URL
    assert _committed()["llm_creds_provider_api_key"] == NEW_KEY
    assert [(b.provider_url, b.api_key) for b in built] == [(NEW_URL, NEW_KEY)]


@pytest.mark.timeout(180)
def test_url_binding_is_judged_on_the_committed_url(admin, built) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)
    _commit_underneath(admin.app, llm_creds_provider_url=NEW_URL)
    assert _stored().llm_creds_provider_url == OLD_URL  # stale cache

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is False, result
    assert "when changing the provider URL" in str(result["error"])
    assert _committed()["llm_creds_provider_url"] == NEW_URL
    assert built == []


@pytest.mark.timeout(180)
def test_url_change_with_nothing_stored_needs_no_key(admin, built) -> None:
    _seed("api_key", OLD_URL, "")

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=NEW_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is True, result
    assert _committed()["llm_creds_provider_url"] == NEW_URL
    assert built == []


@pytest.mark.timeout(180)
def test_test_connection_binds_to_the_committed_url(admin) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)
    _commit_underneath(admin.app, llm_creds_provider_url=NEW_URL)
    assert _stored().llm_creds_provider_url == OLD_URL  # stale cache
    probes: List[_Probe] = []
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(_Probe, "calls", probes)
        patcher.setattr(llm_creds, "LlmCredsClient", _Probe)
        result = _test_connection(admin, OLD_URL, "")

    assert result["success"] is False, result
    assert probes == []


@pytest.mark.timeout(180)
def test_switch_to_subscription_with_stored_key_uses_stored_key(admin, built) -> None:
    _seed("api_key", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key=mask_stored_secret(STORED_KEY),
    )

    assert result["success"] is True, result
    assert _stored().claude_auth_mode == "subscription"
    assert _stored().llm_creds_provider_api_key == STORED_KEY
    assert [call.api_key for call in built] == [STORED_KEY]


@pytest.mark.timeout(180)
def test_subscription_save_without_stored_or_submitted_key_is_rejected(
    admin, built
) -> None:
    _seed("api_key", OLD_URL, "")

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=NEW_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is False, result
    assert "llm_creds_provider_api_key is required" in str(result["error"])
    # Nothing was committed and no lifecycle was started.
    assert _stored().claude_auth_mode == "api_key"
    assert _stored().llm_creds_provider_url == OLD_URL
    assert built == []


@pytest.mark.timeout(180)
def test_subscription_save_refused_when_committed_key_was_cleared_underneath(
    admin, built
) -> None:
    """The key requirement is judged on the COMMITTED row the change is
    applied to, not on this process's cached copy."""
    _seed("api_key", OLD_URL, STORED_KEY)
    _commit_underneath(admin.app, llm_creds_provider_api_key="")
    assert _stored().llm_creds_provider_api_key == STORED_KEY  # stale cache
    assert _committed()["llm_creds_provider_api_key"] == ""

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=NEW_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is False, result
    assert "llm_creds_provider_api_key is required" in str(result["error"])
    committed = _committed()
    assert committed["claude_auth_mode"] == "api_key"
    assert committed["llm_creds_provider_url"] == OLD_URL
    assert committed["llm_creds_provider_api_key"] == ""
    assert built == []


class _Probe:
    """Stands in for LlmCredsClient (the external provider): records the
    credentials each probe used; ``fail`` (when a test sets it) builds, from
    the key used, the exception health() raises."""

    calls: List["_Probe"] = []
    fail: Optional[Callable[[str], Exception]] = None

    def __init__(self, provider_url: str, api_key: str) -> None:
        self.provider_url = provider_url
        self.api_key = api_key
        _Probe.calls.append(self)

    def health(self) -> bool:
        if _Probe.fail is not None:
            raise _Probe.fail(self.api_key)
        return True


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> List[_Probe]:
    monkeypatch.setattr(_Probe, "calls", [])
    monkeypatch.setattr(_Probe, "fail", None)
    monkeypatch.setattr(llm_creds, "LlmCredsClient", _Probe)
    return _Probe.calls


def _post_test_connection(admin: _Session, url: str, key: str):
    return admin.api.post(
        "/api/llm-creds/test-connection",
        json={"provider_url": url, "api_key": key},
        headers=admin.headers,
    )


def _test_connection(admin: _Session, url: str, key: str) -> Payload:
    response = _post_test_connection(admin, url, key)
    assert response.status_code == HTTPStatus.OK, response.text
    assert STORED_KEY not in response.text, "stored key returned to the browser"
    payload: Payload = response.json()
    return payload


@pytest.mark.timeout(180)
@pytest.mark.parametrize("field", ["", "   ", mask_stored_secret(STORED_KEY)])
def test_test_connection_without_a_key_uses_the_committed_stored_key(
    admin, probed, field: str
) -> None:
    _seed("subscription", OLD_URL, NEW_KEY)
    # The cache still holds NEW_KEY.
    _commit_underneath(admin.app, llm_creds_provider_api_key=STORED_KEY)

    result = _test_connection(admin, OLD_URL, field)

    assert result == {"success": True, "error": None}, result
    assert [(p.provider_url, p.api_key) for p in probed] == [(OLD_URL, STORED_KEY)]


@pytest.mark.timeout(180)
def test_test_connection_uses_a_submitted_key(admin, probed) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _test_connection(admin, NEW_URL, NEW_KEY)

    assert result["success"] is True, result
    assert [(p.provider_url, p.api_key) for p in probed] == [(NEW_URL, NEW_KEY)]


@pytest.mark.timeout(180)
def test_test_connection_never_sends_the_stored_key_to_another_url(
    admin, probed
) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _test_connection(admin, NEW_URL, "")

    assert result["success"] is False, result
    assert "API key" in str(result["error"])
    assert probed == []


@pytest.mark.timeout(180)
def test_test_connection_without_any_key_is_refused(admin, probed) -> None:
    _seed("subscription", OLD_URL, "")

    result = _test_connection(admin, OLD_URL, "")

    assert result["success"] is False, result
    assert "API key" in str(result["error"])
    assert probed == []


def _leak_forms(key: str) -> List[str]:
    """Every spelling of *key* an error text or log line could carry."""
    return [key, repr(key), repr(key)[1:-1], repr(key.encode())[2:-1]]


def _assert_never_shown(text: str, key: str) -> None:
    for form in _leak_forms(key):
        assert form not in text, f"key leaked as {form!r}"


@pytest.mark.timeout(180)
def test_test_connection_provider_error_text_is_redacted(admin, probed) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)
    _Probe.fail = lambda key: LlmCredsAuthError(f"provider rejected credential {key}")

    result = _test_connection(admin, OLD_URL, "")

    assert result["success"] is False, result
    assert "provider rejected credential" in str(result["error"])
    _assert_never_shown(str(result), STORED_KEY)
    assert [p.api_key for p in probed] == [STORED_KEY]


@pytest.mark.timeout(180)
def test_stored_key_with_control_character_never_leaks_from_test_connection(
    admin, probed, caplog: pytest.LogCaptureFixture
) -> None:
    """A key written underneath with an interior newline makes the HTTP
    layer reject the header and quote it escaped; neither the response nor
    the logs may carry it, raw or escaped."""
    _seed("subscription", OLD_URL, STORED_KEY)
    _commit_underneath(admin.app, llm_creds_provider_api_key=CTRL_KEY)
    _Probe.fail = lambda key: h11.LocalProtocolError(
        f"Illegal header value {('Bearer ' + key).encode()!r}"
    )
    caplog.set_level(logging.DEBUG, logger=llm_creds.__name__)

    response = _post_test_connection(admin, OLD_URL, "")

    assert response.status_code == HTTPStatus.OK, response.status_code
    assert response.json() == {
        "success": False,
        "error": "Connection test failed (LocalProtocolError)",
    }
    assert [p.api_key for p in probed] == [CTRL_KEY]
    _assert_never_shown(response.text, CTRL_KEY)
    assert caplog.records, "the failure was not logged"
    _assert_never_shown(caplog.text, CTRL_KEY)


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "bad_key",
    [
        CTRL_KEY,
        "example-provider key-0000Gh78",
        "example-tab\tkey-0000",
        "kéy-0000Ij90",
    ],
)
def test_save_rejects_a_key_that_is_not_printable_ascii(
    admin, built, bad_key: str
) -> None:
    _seed("api_key", OLD_URL, STORED_KEY)

    result = _save(
        admin,
        claude_auth_mode="api_key",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key=bad_key,
    )

    assert result["success"] is False, result
    assert "printable" in str(result["error"])
    _assert_never_shown(str(result), bad_key.strip())
    assert _committed()["llm_creds_provider_api_key"] == STORED_KEY
    assert built == []


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "error, shown",
    [
        (RuntimeError(f"checkout failed for {STORED_KEY!r} / {STORED_KEY}"), None),
        (LlmCredsAuthError(f"provider rejected {STORED_KEY}"), "provider rejected"),
    ],
)
def test_save_config_lifecycle_failure_is_redacted(
    admin,
    built,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    shown: Optional[str],
) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)
    _FakeLifecycle.start_error = error
    caplog.set_level(logging.DEBUG, logger=llm_creds.__name__)

    result = _save(
        admin,
        claude_auth_mode="subscription",
        llm_creds_provider_url=OLD_URL,
        llm_creds_provider_api_key="",
    )

    assert result["success"] is False, result
    message = str(result["error"])
    if shown is None:
        assert message.endswith(f"({type(error).__name__})"), message
    else:
        assert shown in message, message
    assert isinstance(error, (RuntimeError, LlmCredsProviderError))
    _assert_never_shown(str(result), STORED_KEY)
    assert caplog.records, "the failure was not logged"
    _assert_never_shown(caplog.text, STORED_KEY)


@pytest.fixture
def elevation_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Elevation enforcement ON with a real (empty) elevation-window store:
    this test's admin has no elevation window."""
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(dependencies, "elevated_session_manager", manager)
    with enforcement(True):
        yield


@pytest.mark.timeout(180)
@pytest.mark.parametrize("field", ["", mask_stored_secret(STORED_KEY)])
def test_test_connection_with_the_stored_key_requires_elevation(
    admin, probed, elevation_enforced, field: str
) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)

    response = _post_test_connection(admin, OLD_URL, field)

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    _assert_never_shown(response.text, STORED_KEY)
    assert probed == []


@pytest.mark.timeout(180)
def test_test_connection_with_a_typed_key_needs_no_elevation(
    admin, probed, elevation_enforced
) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)

    result = _test_connection(admin, OLD_URL, NEW_KEY)

    assert result == {"success": True, "error": None}, result
    assert [p.api_key for p in probed] == [NEW_KEY]


@pytest.mark.parametrize("blank", ["", "  \n"])
def test_lifecycle_is_never_built_with_a_blank_key(blank: str) -> None:
    """Invariant at the one construction point both lifecycle builds use."""
    with pytest.raises(ValueError, match="provider API key"):
        llm_creds._build_lifecycle_service(provider_url=OLD_URL, api_key=blank)


@pytest.mark.timeout(180)
def test_key_hint_shown_only_when_key_is_stored(admin) -> None:
    _seed("subscription", OLD_URL, STORED_KEY)
    assert KEY_HINT in _key_field_html(admin)

    _seed("subscription", OLD_URL, "")
    assert KEY_HINT not in _key_field_html(admin)


# What the client reports for a key that cannot travel in an HTTP header.
_UNSAFE_KEY_ERROR = "provider API key must be printable ASCII"


@pytest.fixture
def listening_url() -> Iterator[str]:
    """A real local TCP listener that accepts connections and never answers:
    a request with the stored key would reach header serialization, where
    the HTTP layer rejects (and quotes, escaped) a header-unsafe key."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        yield f"http://127.0.0.1:{server.getsockname()[1]}"


def _app_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, DEBUG included, formatted -- except third-party
    httpcore DEBUG, which traces raw request headers and is not asserted on
    here (no request is made with a header-unsafe key anyway)."""
    return "\n".join(
        caplog.handler.format(record)
        for record in caplog.records
        if not record.name.startswith("httpcore")
    )


@pytest.mark.timeout(180)
def test_stored_control_character_key_never_leaks_through_the_lease_lifecycle(
    admin, listening_url, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """Real lifecycle and client: save-config (re)starts the lease with a
    stored key written underneath with an interior newline; neither that
    response, /lease-status, nor any log carries the key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # start() pops it
    _seed("subscription", listening_url, STORED_KEY)
    _commit_underneath(admin.app, llm_creds_provider_api_key=CTRL_KEY)
    caplog.set_level(logging.DEBUG)

    with enforcement(False):
        saved = admin.api.post(
            "/api/llm-creds/save-config",
            json={
                "claude_auth_mode": "subscription",
                "llm_creds_provider_url": listening_url,
                "llm_creds_provider_api_key": "",
            },
            headers=admin.headers,
        )
    lease = admin.api.get("/api/llm-creds/lease-status", headers=admin.headers)

    assert saved.status_code == HTTPStatus.OK, saved.text
    assert lease.status_code == HTTPStatus.OK, lease.text
    assert lease.json()["status"] == "degraded", lease.json()
    assert lease.json()["error"] == _UNSAFE_KEY_ERROR
    _assert_never_shown(saved.text, CTRL_KEY)
    _assert_never_shown(lease.text, CTRL_KEY)
    _assert_never_shown(_app_log_text(caplog), CTRL_KEY)


@pytest.mark.timeout(180)
def test_startup_lifecycle_with_control_character_key_degrades_safely(
    tmp_path: Path, listening_url, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """The startup path (lifespan.py builds the client and lifecycle outside
    any error handler, then calls start()): construction must not raise, and
    the lease degrades with a fixed error and clean logs."""
    from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
    from code_indexer.server.services.claude_credentials_file_manager import (
        ClaudeCredentialsFileManager,
    )
    from code_indexer.server.services.llm_creds_client import LlmCredsClient
    from code_indexer.server.services.llm_lease_lifecycle import (
        LeaseLifecycleStatus,
        LlmLeaseLifecycleService,
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # start() pops it
    caplog.set_level(logging.DEBUG)

    client = LlmCredsClient(provider_url=listening_url, api_key=CTRL_KEY)
    service = LlmLeaseLifecycleService(
        client=client,
        state_manager=LlmLeaseStateManager(server_dir_path=str(tmp_path / "state")),
        credentials_manager=ClaudeCredentialsFileManager(
            credentials_path=tmp_path / "creds" / ".credentials.json"
        ),
        claude_json_path=tmp_path / ".claude.json",
    )
    service.start(consumer_id="cidx-server")

    status = service.get_status()
    assert status.status == LeaseLifecycleStatus.DEGRADED
    assert status.error == _UNSAFE_KEY_ERROR
    _assert_never_shown(_app_log_text(caplog), CTRL_KEY)
