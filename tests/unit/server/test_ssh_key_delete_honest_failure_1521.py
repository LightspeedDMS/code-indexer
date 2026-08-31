"""
Regression tests for Bug #1521 (secondary finding) -- delete front doors lie.

Bug #1519 made ``SSHKeyManager.delete_key()`` return ``False`` when it refuses
to delete a same-named file it cannot prove it ever wrote.  That refusal is the
correct, safe behaviour -- but all three delete front doors discarded the
return value and unconditionally reported success:

  * ``routers/ssh_keys.py``      -> ``DeleteKeyResponse(success=True, ...)``
  * ``mcp/handlers/ssh_keys.py`` -> ``{"success": True, "message": "... deleted"}``
  * ``web/routes.py``            -> "SSH key '...' deleted successfully."

So the provenance guard correctly declined to destroy a real key while the API
told the operator it had been deleted -- a silent lie about the outcome of a
safety-critical operation.

These tests drive a REAL ``SSHKeyManager`` over a REAL tmp_path ssh directory
containing a REAL untracked key file, so the ``False`` return is produced by the
genuine production guard rather than by a stubbed return value.  Only the
manager LOOKUP (and, for the web route, session/CSRF/rendering) is redirected.

NOTE: every test here uses a tmp_path-based ssh_dir.  Nothing in this file may
ever touch the real ``~/.ssh``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from code_indexer.server.services.ssh_key_manager import SSHKeyManager

UNTRACKED_KEY_NAME = "id_ed25519"
UNTRACKED_PRIVATE = "PERSONAL_PRIVATE_KEY_CONTENT"
UNTRACKED_PUBLIC = "ssh-ed25519 PERSONAL_PUBLIC"


def _manager_refusing_to_delete(tmp_path: Path) -> SSHKeyManager:
    """A REAL manager whose ssh_dir holds an untracked, same-named key file.

    ``delete_key(UNTRACKED_KEY_NAME)`` therefore genuinely returns False via the
    Bug #1519 provenance guard -- no stubbing of the decision under test.
    """
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata_dir = tmp_path / "meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    (ssh_dir / UNTRACKED_KEY_NAME).write_text(UNTRACKED_PRIVATE)
    (ssh_dir / f"{UNTRACKED_KEY_NAME}.pub").write_text(UNTRACKED_PUBLIC)

    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=metadata_dir,
        config_path=ssh_dir / "config",
        use_sqlite=False,
    )


# `Dict[str, Any]` is the precise shape of an MCP envelope: a "content" list of
# heterogeneous parts. No narrower static type describes it.
def _mcp_payload(response: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the JSON body an MCP handler embeds in its content array."""
    payload: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return payload


class TestRestDeleteReportsRefusalHonestly:
    def test_refused_delete_raises_conflict_instead_of_reporting_success(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.routers import ssh_keys as ssh_keys_router

        manager = _manager_refusing_to_delete(tmp_path)
        assert manager.delete_key(UNTRACKED_KEY_NAME) is False

        with patch.object(ssh_keys_router, "get_ssh_key_manager", return_value=manager):
            with pytest.raises(HTTPException) as excinfo:
                ssh_keys_router.delete_ssh_key(UNTRACKED_KEY_NAME)

        assert excinfo.value.status_code == 409
        assert UNTRACKED_KEY_NAME in str(excinfo.value.detail)
        # The file must still be there -- the refusal is what we are reporting.
        assert (manager.ssh_dir / UNTRACKED_KEY_NAME).read_text() == UNTRACKED_PRIVATE

    def test_successful_delete_still_reports_success(self, tmp_path: Path) -> None:
        """No regression: a genuine (idempotent) delete keeps returning 200."""
        from code_indexer.server.routers import ssh_keys as ssh_keys_router

        manager = _manager_refusing_to_delete(tmp_path)

        with patch.object(ssh_keys_router, "get_ssh_key_manager", return_value=manager):
            response = ssh_keys_router.delete_ssh_key("never_existed")

        assert response.success is True
        assert "never_existed" in response.message


class TestMcpDeleteReportsRefusalHonestly:
    def test_refused_delete_returns_failure_payload(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers import ssh_keys as ssh_keys_handlers

        manager = _manager_refusing_to_delete(tmp_path)

        with patch.object(
            ssh_keys_handlers, "get_ssh_key_manager", return_value=manager
        ):
            response = ssh_keys_handlers._delete(
                {"name": UNTRACKED_KEY_NAME}, MagicMock()
            )

        payload = _mcp_payload(response)
        assert payload["success"] is False
        assert UNTRACKED_KEY_NAME in payload["error"]
        assert (manager.ssh_dir / UNTRACKED_KEY_NAME).read_text() == UNTRACKED_PRIVATE

    def test_successful_delete_still_returns_success_payload(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.mcp.handlers import ssh_keys as ssh_keys_handlers

        manager = _manager_refusing_to_delete(tmp_path)

        with patch.object(
            ssh_keys_handlers, "get_ssh_key_manager", return_value=manager
        ):
            response = ssh_keys_handlers._delete({"name": "never_existed"}, MagicMock())

        payload = _mcp_payload(response)
        assert payload["success"] is True


class TestWebDeleteReportsRefusalHonestly:
    @staticmethod
    def _invoke(tmp_path: Path, key_name: str) -> Dict[str, Any]:
        """Drive the web delete route, capturing how the page was rendered.

        Session lookup, CSRF validation and template rendering are collaborators
        of the route, not the logic under test; the manager itself stays real.
        """
        from code_indexer.server.web import routes as web_routes

        manager = _manager_refusing_to_delete(tmp_path)
        captured: Dict[str, Any] = {}

        def _capture(request: Any, session: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=MagicMock()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(web_routes, "_get_ssh_key_manager", return_value=manager),
            patch.object(
                web_routes, "_create_ssh_keys_page_response", side_effect=_capture
            ),
        ):
            web_routes.delete_ssh_key(
                request=MagicMock(), key_name=key_name, csrf_token="token"
            )

        captured["manager"] = manager
        return captured

    def test_refused_delete_renders_error_message(self, tmp_path: Path) -> None:
        captured = self._invoke(tmp_path, UNTRACKED_KEY_NAME)

        assert captured.get("success_message") is None
        error_message = captured.get("error_message")
        assert error_message is not None
        assert UNTRACKED_KEY_NAME in error_message

        manager = captured["manager"]
        assert (manager.ssh_dir / UNTRACKED_KEY_NAME).read_text() == UNTRACKED_PRIVATE

    def test_successful_delete_renders_success_message(self, tmp_path: Path) -> None:
        captured = self._invoke(tmp_path, "never_existed")

        assert captured.get("error_message") is None
        assert "never_existed" in captured["success_message"]
