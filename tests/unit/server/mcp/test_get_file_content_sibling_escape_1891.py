"""Bug #1891 round 2 (S1/S4): real MCP get_file_content front-door tests.

Drives the ACTUAL MCP handler (`code_indexer.server.mcp.handlers.files.
get_file_content`), not a mocked `file_service`, against a real on-disk
sibling-repo layout (`golden-repos/foo` + `golden-repos/foo-private`). This
proves the fix closes the vulnerability at the real front door a non-admin
MCP user reaches, not just at the underlying FileListingService method
(covered separately in
tests/unit/server/services/test_file_service_sibling_prefix_escape_1891.py).

Only the DI seams (`activated_repo_manager`/`golden_repo_manager` bare-alias
lazy probes, and the real `file_service` singleton's ActivatedRepoManager
resolution) are stubbed -- the handler, the pagination parsing, the
response-building, and FileListingService.get_file_content/
resolve_confined_path all run for real (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.file_service import file_service

_ARM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.activated_repo_manager"
_GRM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.golden_repo_manager"
_INFO_LEVEL = logging.INFO
_WARNING_LEVEL = logging.WARNING

OUTSIDE_MARKER = "SECRET_SIBLING_MCP_HANDLER_1891"
INSIDE_MARKER = "legitimate mcp handler content"


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.NORMAL_USER,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    text = handler_response["content"][0]["text"]
    return cast(Dict[str, Any], json.loads(text))


def _build_sibling_tree(tmp_path):
    golden_repos = tmp_path / "golden-repos"
    foo = golden_repos / "foo"
    (foo / "src").mkdir(parents=True)
    (foo / "src" / "a.py").write_text(INSIDE_MARKER)

    foo_private = golden_repos / "foo-private"
    foo_private.mkdir()
    (foo_private / "secret.txt").write_text(OUTSIDE_MARKER)

    return foo


def _call_handler(params: Dict[str, Any], repo_root):
    """Call the real get_file_content handler with only the DI seams
    stubbed: no bare-to-global fallback (both lazy probes return None), and
    the real file_service singleton's activated_repo_manager resolves to
    the on-disk repo_root.

    Deliberately bypasses unittest.mock.patch.object on the
    `activated_repo_manager` PROPERTY: its getter lazily constructs a real
    ActivatedRepoManager (Bug #1650) with no matching deleter, so
    patch.object's teardown -- which calls delattr() when its own
    getattr()-based "capture original value" step raced the lazy
    construction -- raises `AttributeError: can't delete attribute`.
    Saving/restoring the private backing attribute directly sets the
    already-constructed value without ever invoking the getter.
    """
    from code_indexer.server.mcp.handlers.files import get_file_content

    user = _make_user()
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(repo_root)

    original_arm = getattr(file_service, "_activated_repo_manager_lazy", None)
    file_service._activated_repo_manager_lazy = arm
    try:
        with (
            patch(_ARM_PATH, new=None),
            patch(_GRM_PATH, new=None),
        ):
            return get_file_content(params, user)
    finally:
        file_service._activated_repo_manager_lazy = original_arm


class TestHandlerBlocksSiblingPrefixEscape:
    def test_handler_blocks_sibling_prefix_escape_and_does_not_leak_secret(
        self, tmp_path
    ):
        foo = _build_sibling_tree(tmp_path)
        params = {
            "repository_alias": "foo",
            "file_path": "../foo-private/secret.txt",
        }

        result = _call_handler(params, foo)
        data = _json_result(result)

        assert data.get("success") is False, data
        assert OUTSIDE_MARKER not in json.dumps(data), (
            "the secret sibling file's content must never reach the MCP "
            f"response: {data}"
        )

    def test_handler_legitimate_nested_path_still_returns_content(self, tmp_path):
        foo = _build_sibling_tree(tmp_path)
        params = {"repository_alias": "foo", "file_path": "src/a.py"}

        result = _call_handler(params, foo)
        data = _json_result(result)

        assert data.get("success") is True, data
        assert INSIDE_MARKER in json.dumps(data), data


class TestHandlerMalformedPathDoesNotCrash:
    def test_handler_malformed_nul_byte_path_returns_clean_error_not_traceback(
        self, tmp_path
    ):
        foo = _build_sibling_tree(tmp_path)
        params = {
            "repository_alias": "foo",
            "file_path": "src/a.py\x00evil",
        }

        result = _call_handler(params, foo)
        data = _json_result(result)

        assert data.get("success") is False, data
        assert OUTSIDE_MARKER not in json.dumps(data), data
        serialized = json.dumps(data)
        assert "Traceback" not in serialized, serialized
        assert "embedded null byte" not in serialized, (
            "the raw ValueError message must not leak past the "
            f"PermissionError normalization: {serialized}"
        )


class TestEscapeAttemptDoesNotEmitErrorLog:
    """Bug #1891 round 3 (D3): before the fix, get_file_content had no
    explicit PermissionError branch, so every escape attempt fell into
    the generic ``except Exception: logger.exception(...)`` -- an
    ERROR-level record WITH a traceback. A client-triggerable ERROR log
    can trip the E2E Phase 3/4 log-audit gate
    (tests/e2e/log_audit_gate.py), which fails a phase on any new
    unallowlisted ERROR/WARNING entry. The fix adds an explicit
    ``except PermissionError`` branch that logs at INFO with no
    traceback."""

    def _assert_no_error_or_warning(self, caplog):
        bad_records = [r for r in caplog.records if r.levelno >= _WARNING_LEVEL]
        assert bad_records == [], (
            "an escape attempt must never emit an ERROR/WARNING log "
            f"record: {[(r.levelname, r.getMessage()) for r in bad_records]}"
        )
        assert not any(r.exc_info for r in caplog.records), (
            "an escape attempt must never emit a record with a traceback"
        )

    def test_sibling_escape_attempt_emits_no_error_or_warning_record(
        self, tmp_path, caplog
    ):
        foo = _build_sibling_tree(tmp_path)
        params = {
            "repository_alias": "foo",
            "file_path": "../foo-private/secret.txt",
        }

        with caplog.at_level(_INFO_LEVEL, logger="code_indexer.server.mcp"):
            result = _call_handler(params, foo)

        data = _json_result(result)
        assert data.get("success") is False, data
        self._assert_no_error_or_warning(caplog)

    def test_malformed_nul_byte_path_emits_no_error_or_warning_record(
        self, tmp_path, caplog
    ):
        foo = _build_sibling_tree(tmp_path)
        params = {
            "repository_alias": "foo",
            "file_path": "src/a.py\x00evil",
        }

        with caplog.at_level(_INFO_LEVEL, logger="code_indexer.server.mcp"):
            result = _call_handler(params, foo)

        data = _json_result(result)
        assert data.get("success") is False, data
        self._assert_no_error_or_warning(caplog)
