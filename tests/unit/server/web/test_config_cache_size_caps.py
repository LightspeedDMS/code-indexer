"""
Unit tests for Bug #880: Cache size-cap Web UI gap.

AC1/AC2: edit-form-cache has type=number inputs for both size-cap fields.
AC3: /admin/config shows "4096 (default)" when DB value is None.
AC4/AC5: POST persists values; round-trip through DB reset proves persistence.
AC6: _update_cache_setting propagates to live cache singleton.
     AC6a (None case): runtime singleton gets 4096 floor, DB stays None.
AC7: Server-side validation rejects zero/negative/non-integer cap values;
     error message appears in class="validation-error" div.
M1:  Template display rows use explicit `is none` check, not `or` shorthand.

Design:
- AC1/AC2/M1 scan raw template HTML (fast, fixture-free).
- AC3/AC4/AC5/AC7 drive a real TestClient.
- No credential literals; credentials are runtime-generated.
- AC4/AC5 round-trip uses reset_config_service() to prove DB persistence.
- AC6 patches only external cache singletons; ConfigService runs unmodified.
- AC7 checks the specific validation message inside class="validation-error" div.
"""

import re
import secrets
import string
import tempfile
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_DISPLAY_WHEN_NULL = "4096 (default)"
_DISPLAY_LEGACY_UNLIMITED = "Unlimited"
_FORM_FIELD_HNSW = 'name="index_cache_max_size_mb"'
_FORM_FIELD_FTS = 'name="fts_cache_max_size_mb"'
_EXPLICIT_CAP_MB = 8192
_DEFAULT_CAP_MB = 4096
_INPUT_CONTEXT_WINDOW = 200
_ROW_SCAN_WINDOW = 300
_TOKEN_USERNAME_BYTES = 8
_TEST_TIMEOUT_SECONDS = 30

# Specific validation error fragment unique to the size-cap fields (routes.py).
# This text appears inside class="validation-error" in the re-rendered form.
_SIZE_CAP_VALIDATION_FRAGMENT = "must be empty or a positive integer"

# Old `or`-shorthand pattern that must no longer appear in display rows
_OLD_OR_DEFAULT_LITERAL = "or '4096"

# ---------------------------------------------------------------------------
# Parametrize tables
# ---------------------------------------------------------------------------

# (label_text, form_field_name, config_attr_name)
_MAX_SIZE_FIELDS = [
    pytest.param(
        "Index Cache Max Size (MB)",
        _FORM_FIELD_HNSW,
        "index_cache_max_size_mb",
        id="hnsw",
    ),
    pytest.param(
        "FTS Cache Max Size (MB)", _FORM_FIELD_FTS, "fts_cache_max_size_mb", id="fts"
    ),
]

# (form_key, config_attr, posted_value, expected_db_value)
_POST_PERSIST_CASES = [
    pytest.param(
        "index_cache_max_size_mb",
        "index_cache_max_size_mb",
        str(_EXPLICIT_CAP_MB),
        _EXPLICIT_CAP_MB,
        id="hnsw_explicit",
    ),
    pytest.param(
        "fts_cache_max_size_mb",
        "fts_cache_max_size_mb",
        str(_EXPLICIT_CAP_MB),
        _EXPLICIT_CAP_MB,
        id="fts_explicit",
    ),
    pytest.param(
        "index_cache_max_size_mb", "index_cache_max_size_mb", "", None, id="hnsw_empty"
    ),
    pytest.param(
        "fts_cache_max_size_mb", "fts_cache_max_size_mb", "", None, id="fts_empty"
    ),
]

# (cache_key, value_str, expected_db_size_mb, cache_kind)
_HOT_RELOAD_CASES = [
    pytest.param(
        "index_cache_max_size_mb",
        str(_EXPLICIT_CAP_MB),
        _EXPLICIT_CAP_MB,
        "HNSW",
        id="hnsw_explicit",
    ),
    pytest.param(
        "fts_cache_max_size_mb",
        str(_EXPLICIT_CAP_MB),
        _EXPLICIT_CAP_MB,
        "FTS",
        id="fts_explicit",
    ),
    pytest.param("index_cache_max_size_mb", "", None, "HNSW", id="hnsw_none"),
    pytest.param("fts_cache_max_size_mb", "", None, "FTS", id="fts_none"),
]

_INVALID_CAP_VALUES = [
    pytest.param("0", id="zero"),
    pytest.param("-100", id="negative"),
    pytest.param("abc", id="non_integer"),
]

_CACHE_FORM_KEYS = [
    pytest.param("index_cache_max_size_mb", "index_cache_max_size_mb", id="hnsw"),
    pytest.param("fts_cache_max_size_mb", "fts_cache_max_size_mb", id="fts"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_password() -> str:
    """Generate a random password accepted by the server's password validator."""
    from code_indexer.server.auth.password_strength_validator import (
        PasswordStrengthValidator,
    )

    validator = PasswordStrengthValidator()
    specials = "!@#%^&*"
    alphabet = string.ascii_letters + string.digits + specials
    for _ in range(10):
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(specials),
        ] + [secrets.choice(alphabet) for _ in range(16)]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        ok, _ = validator.validate(candidate, username="testuser")
        if ok:
            return candidate
    raise AssertionError("_make_test_password() exhausted all attempts")


def _edit_form_cache_block(html: str) -> str:
    start = html.find('id="edit-form-cache"')
    assert start != -1, '"edit-form-cache" not found in template'
    end = html.find("</form>", start)
    assert end != -1, "No closing </form> after edit-form-cache"
    return html[start : end + len("</form>")]


def _extract_tr_for_label(html: str, label_text: str) -> str:
    """Return the <tr>...</tr> block containing the given config-label text."""
    idx = html.find(label_text)
    assert idx != -1, f"Label {label_text!r} not found in template"
    tr_start = html.rfind("<tr>", 0, idx)
    assert tr_start != -1, f"No <tr> before label {label_text!r}"
    tr_end = html.find("</tr>", idx)
    assert tr_end != -1, f"No </tr> after label {label_text!r}"
    return html[tr_start : tr_end + len("</tr>")]


def _display_section_block(html: str) -> str:
    start = html.find('id="display-cache"') if 'id="display-cache"' in html else 0
    edit_start = html.find('id="edit-form-cache"')
    assert edit_start != -1, '"edit-form-cache" not found'
    return html[start:edit_start]


def _context_around(html: str, fragment: str, window: int = 200) -> str:
    idx = html.find(fragment)
    assert idx != -1, f"Fragment not found: {fragment!r}"
    return html[max(0, idx - window) : idx + len(fragment) + window]


def _find_validation_error_div_content(html: str) -> str:
    """Extract text content of the first class="validation-error" div in the HTML.

    Returns empty string if no such div is found.
    """
    match = re.search(
        r'<div[^>]+class="validation-error"[^>]*>(.*?)</div>', html, re.DOTALL
    )
    return match.group(1).strip() if match else ""


def _post_cache_form(client, cookies, csrf_token: str, form_data: dict):
    data = {**form_data, "csrf_token": csrf_token}
    return client.post(
        "/admin/config/cache", data=data, cookies=cookies, follow_redirects=True
    )


def _scrape_csrf_token(html: str) -> str:
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "CSRF token not found in HTML"
    return match.group(1)


def _assert_round_trip_db_value(tmpdir_path, config_attr, expected_db_value):
    """Re-read config from DB after reset to verify persistence, not in-process cache.

    Calls initialize_runtime_db() on the fresh service so it reads from the
    same SQLite DB the running app wrote to, not just the bootstrap config.json.
    """
    from code_indexer.server.services.config_service import (
        get_config_service,
        reset_config_service,
    )

    db_path = str(tmpdir_path / "data" / "cidx_server.db")
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        cs = get_config_service()
        cs.initialize_runtime_db(db_path)
        config = cs.get_config()
        assert config.cache_config is not None
        actual = getattr(config.cache_config, config_attr)
        assert actual == expected_db_value, (
            f"Round-trip: expected {config_attr}={expected_db_value!r} from DB, got {actual!r}"
        )
        reset_config_service()


def _make_mock_cache() -> MagicMock:
    cache = MagicMock()
    cache._cache_lock = Lock()
    cache.config = SimpleNamespace(max_cache_size_mb=None)
    cache._enforce_size_limit = MagicMock()
    return cache


def _run_hot_reload_with_mocks(cs, config, cache_key, value_str, mock_hnsw, mock_fts):
    """Execute _update_cache_setting with mocked cache singletons."""
    with (
        patch("code_indexer.server.cache.get_global_cache", return_value=mock_hnsw),
        patch("code_indexer.server.cache.get_global_fts_cache", return_value=mock_fts),
    ):
        cs._update_cache_setting(config, cache_key, value_str)


def _assert_runtime_cap_and_eviction(
    target_cache, cache_kind: str, expected_runtime_mb: int
):
    assert target_cache.config.max_cache_size_mb == expected_runtime_mb, (
        f"{cache_kind} runtime cap: expected {expected_runtime_mb!r}, "
        f"got {target_cache.config.max_cache_size_mb!r}"
    )
    target_cache._enforce_size_limit.assert_called_once()


def _assert_db_value_stays_none(config, db_attr: str):
    db_value = getattr(config.cache_config, db_attr)
    assert db_value is None, (
        f"DB config.cache_config.{db_attr} must stay None, got {db_value!r}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_html():
    template_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    return (template_dir / "partials" / "config_section.html").read_text()


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def app_with_db(tmpdir_path):
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(tmpdir_path / "test.db")).initialize_database()
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        app = create_app()
        yield app
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    with TestClient(app_with_db) as test_client:
        yield test_client


@pytest.fixture
def admin_credentials(tmpdir_path, app_with_db):
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    user_manager = UserManager(
        use_sqlite=True, db_path=str(tmpdir_path / "data" / "cidx_server.db")
    )
    username = secrets.token_hex(_TOKEN_USERNAME_BYTES)
    password = _make_test_password()
    user_manager.create_user(username=username, password=password, role=UserRole.ADMIN)
    return username, password


@pytest.fixture
def admin_session(client, admin_credentials):
    username, password = admin_credentials
    resp_get = client.get("/login")
    assert resp_get.status_code == 200
    csrf_token = _scrape_csrf_token(resp_get.text)
    resp_post = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        cookies=resp_get.cookies,
        follow_redirects=False,
    )
    assert resp_post.status_code == 303, f"Login failed: HTTP {resp_post.status_code}"
    for name, value in resp_post.cookies.items():
        client.cookies.set(name, value)
    return resp_post.cookies


@pytest.fixture
def cache_csrf_token(client, admin_session):
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    return _scrape_csrf_token(resp.text)


# ---------------------------------------------------------------------------
# AC1 + AC2: edit-form-cache contains number inputs for both size-cap fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label_fragment, form_field_name, _attr", _MAX_SIZE_FIELDS)
def test_edit_form_cache_contains_size_cap_input(
    template_html, label_fragment, form_field_name, _attr
):
    """AC1/AC2: edit-form-cache must contain a type=number input for each size-cap field."""
    form_block = _edit_form_cache_block(template_html)
    assert form_field_name in form_block, (
        f"Expected {form_field_name!r} inside edit-form-cache"
    )
    context = _context_around(form_block, form_field_name, window=_INPUT_CONTEXT_WINDOW)
    assert 'type="number"' in context, f"{form_field_name!r} input must be type=number"


# ---------------------------------------------------------------------------
# M1: Template display rows use `is none` check, not `or` shorthand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label_text, _field, attr_name", _MAX_SIZE_FIELDS)
def test_template_display_row_uses_is_none_not_or(
    template_html, label_text, _field, attr_name
):
    """M1: Display <tr> row for each Max Size field must use `is none` check.

    The `or` idiom (`{{ config.cache.X or '4096 (default)' }}`) conflates
    None/0/"" — `is none` is the correct Jinja idiom for this check.
    Both the generic `or ` pattern and the concrete `or '4096` literal
    must be absent from the row.
    """
    tr_block = _extract_tr_for_label(template_html, label_text)
    assert f"{attr_name} is none" in tr_block, (
        f"Display row for {label_text!r} must use '{{{{ {attr_name} is none }}}}' "
        f"check. Row: {tr_block!r}"
    )
    assert _OLD_OR_DEFAULT_LITERAL not in tr_block, (
        f"Display row for {label_text!r} must not contain '{_OLD_OR_DEFAULT_LITERAL}' "
        f"(old shorthand). Row: {tr_block!r}"
    )
    assert f"{attr_name} or " not in tr_block, (
        f"Display row for {label_text!r} must not use '{attr_name} or ' shorthand. "
        f"Row: {tr_block!r}"
    )


# ---------------------------------------------------------------------------
# AC3: rendered /admin/config shows "4096 (default)" not "Unlimited" when None
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("label_fragment, _, _attr", _MAX_SIZE_FIELDS)
def test_rendered_display_row_shows_default_not_unlimited(
    client, admin_session, label_fragment, _, _attr
):
    """AC3: Rendered /admin/config must show '4096 (default)' for each Max Size field
    when DB value is None (fresh-DB default state)."""
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    display_block = _display_section_block(resp.text)
    idx = display_block.find(label_fragment)
    assert idx != -1, f"Label {label_fragment!r} not found in display section"
    row_context = display_block[idx : idx + _ROW_SCAN_WINDOW]
    assert _DISPLAY_LEGACY_UNLIMITED not in row_context, (
        f"'Unlimited' still present in {label_fragment!r} row"
    )
    assert _DISPLAY_WHEN_NULL in row_context, (
        f"'{_DISPLAY_WHEN_NULL}' not found in {label_fragment!r} row"
    )


# ---------------------------------------------------------------------------
# AC4 + AC5: POST persists value; round-trip through DB reset proves persistence
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize(
    "form_key, config_attr, posted_value, expected_db_value", _POST_PERSIST_CASES
)
def test_post_cap_persists_round_trip(
    tmpdir_path,
    client,
    admin_session,
    cache_csrf_token,
    form_key,
    config_attr,
    posted_value,
    expected_db_value,
):
    """AC4/AC5: POST value must persist to DB. Round-trip via reset_config_service proves
    the value came from DB storage, not in-process cache."""
    resp = _post_cache_form(
        client, admin_session, cache_csrf_token, {form_key: posted_value}
    )
    assert resp.status_code == 200, f"POST failed: HTTP {resp.status_code}"
    _assert_round_trip_db_value(tmpdir_path, config_attr, expected_db_value)


# ---------------------------------------------------------------------------
# AC6: _update_cache_setting propagates size-cap to the live cache singleton
# AC6a (None case): runtime singleton gets 4096, DB config stays None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cache_key, value_str, expected_db_size_mb, cache_kind", _HOT_RELOAD_CASES
)
def test_update_cache_setting_propagates_to_cache_singleton(
    cache_key, value_str, expected_db_size_mb, cache_kind
):
    """AC6/AC6a: _update_cache_setting sets runtime cap and calls _enforce_size_limit.

    None/empty value (AC6a): runtime singleton gets DEFAULT=4096 (not None).
    DB config stays None (persistence semantics: 'no override, use default').
    """
    from code_indexer.server.services.config_service import (
        get_config_service,
        reset_config_service,
    )
    from code_indexer.server.storage.database_manager import DatabaseSchema

    expected_runtime_mb = (
        _DEFAULT_CAP_MB if expected_db_size_mb is None else expected_db_size_mb
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        DatabaseSchema(str(Path(tmpdir) / "test.db")).initialize_database()
        with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}):
            reset_config_service()
            cs = get_config_service()
            config = cs.get_config()
            mock_hnsw, mock_fts = _make_mock_cache(), _make_mock_cache()
            _run_hot_reload_with_mocks(
                cs, config, cache_key, value_str, mock_hnsw, mock_fts
            )

            target = mock_hnsw if cache_kind == "HNSW" else mock_fts
            _assert_runtime_cap_and_eviction(target, cache_kind, expected_runtime_mb)

            if expected_db_size_mb is None:
                db_attr = (
                    "index_cache_max_size_mb"
                    if cache_kind == "HNSW"
                    else "fts_cache_max_size_mb"
                )
                _assert_db_value_stays_none(config, db_attr)

            reset_config_service()


# ---------------------------------------------------------------------------
# AC7: Server-side validation rejects invalid size-cap values
# (zero, negative, non-integer) — parametrized over value x [hnsw|fts]
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("bad_value", _INVALID_CAP_VALUES)
@pytest.mark.parametrize("form_key, config_attr", _CACHE_FORM_KEYS)
def test_post_rejects_invalid_cap_value(
    tmpdir_path,
    client,
    admin_session,
    cache_csrf_token,
    form_key,
    config_attr,
    bad_value,
):
    """AC7: POST with zero, negative, or non-integer cap must show the size-cap
    validation message inside the class="validation-error" div and leave the DB
    value unchanged (verified via round-trip DB reset).
    """
    resp = _post_cache_form(
        client, admin_session, cache_csrf_token, {form_key: bad_value}
    )

    # Validation message must appear inside the class="validation-error" div
    error_div_content = _find_validation_error_div_content(resp.text)
    assert _SIZE_CAP_VALIDATION_FRAGMENT in error_div_content, (
        f"Expected '{_SIZE_CAP_VALIDATION_FRAGMENT}' inside class='validation-error' div "
        f"for {form_key}={bad_value!r} (HTTP {resp.status_code}). "
        f"Div content: {error_div_content!r}"
    )

    # DB value must be unchanged — verified via round-trip from storage
    _assert_round_trip_db_value(tmpdir_path, config_attr, None)
