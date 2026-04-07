"""SSH key management handlers for CIDX MCP server."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Any, Optional, TYPE_CHECKING

from ...middleware.correlation import get_correlation_id
from ...auth.user_manager import User
from ...services.ssh_key_manager import (
    SSHKeyManager,
    KeyNotFoundError,
    HostConflictError,
)
from ...services.ssh_key_generator import (
    InvalidKeyNameError,
    KeyAlreadyExistsError,
)

from ._utils import _mcp_response

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# SSH Key Manager singleton with double-checked locking for thread safety.
_ssh_key_manager: Optional[SSHKeyManager] = None
_manager_lock = Lock()


def get_ssh_key_manager() -> SSHKeyManager:
    """Get or create the SSH key manager instance with SQLite backend (Story #702)."""
    global _ssh_key_manager
    if _ssh_key_manager is None:
        with _manager_lock:
            if _ssh_key_manager is None:
                from ...services.config_service import get_config_service

                config_service = get_config_service()
                server_dir = config_service.config_manager.server_dir
                db_path = server_dir / "data" / "cidx_server.db"
                metadata_dir = server_dir / "data" / "ssh_keys"

                _ssh_key_manager = SSHKeyManager(
                    metadata_dir=metadata_dir,
                    use_sqlite=True,
                    db_path=db_path,
                )
    return _ssh_key_manager


def _ssh_error(context: str, exc: Exception) -> Dict[str, Any]:
    """Log and return a standard SSH handler error response."""
    logger.exception(
        f"{context}: {exc}",
        extra={"correlation_id": get_correlation_id()},
    )
    return _mcp_response({"success": False, "error": str(exc)})


def _metadata_payload(meta: Any, include_public: bool = False) -> Dict[str, Any]:
    """Build a standard key metadata dict from an SSHKeyMetadata object."""
    payload: Dict[str, Any] = {
        "name": meta.name,
        "fingerprint": meta.fingerprint,
        "key_type": meta.key_type,
        "email": meta.email,
        "description": meta.description,
    }
    if include_public:
        payload["public_key"] = meta.public_key
    else:
        payload["hosts"] = meta.hosts
    return payload


def handle_ssh_key_create(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Create a new SSH key pair.

    Args:
        args: Dict with name, key_type (optional), email (optional), description (optional)
        user: Authenticated user

    Returns:
        Dict with success status and public key
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    key_type = args.get("key_type", "ed25519")
    email = args.get("email")
    description = args.get("description")

    manager = get_ssh_key_manager()

    try:
        metadata = manager.create_key(
            name=name,
            key_type=key_type,
            email=email,
            description=description,
        )
        payload = _metadata_payload(metadata, include_public=True)
        payload["success"] = True
        return _mcp_response(payload)

    except InvalidKeyNameError as e:
        return _mcp_response({"success": False, "error": f"Invalid key name: {str(e)}"})
    except KeyAlreadyExistsError as e:
        return _mcp_response(
            {"success": False, "error": f"Key already exists: {str(e)}"}
        )
    except Exception as e:
        return _ssh_error("Error creating SSH key", e)


def handle_ssh_key_list(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    List all managed and unmanaged SSH keys.

    Args:
        args: Empty dict (no parameters needed)
        user: Authenticated user

    Returns:
        Dict with managed and unmanaged key lists
    """
    manager = get_ssh_key_manager()

    try:
        result = manager.list_keys()

        managed = [
            {
                **_metadata_payload(k),
                "is_imported": k.is_imported,
            }
            for k in result.managed
        ]

        unmanaged = [
            {
                "name": k.name,
                "fingerprint": k.fingerprint,
                "private_path": str(k.private_path),
            }
            for k in result.unmanaged
        ]

        return _mcp_response(
            {
                "success": True,
                "managed": managed,
                "unmanaged": unmanaged,
            }
        )

    except Exception as e:
        return _ssh_error("Error listing SSH keys", e)


def handle_ssh_key_delete(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Delete an SSH key.

    Args:
        args: Dict with name
        user: Authenticated user

    Returns:
        Dict with success status
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    manager = get_ssh_key_manager()

    try:
        manager.delete_key(name)
        return _mcp_response(
            {
                "success": True,
                "message": f"Key '{name}' deleted",
            }
        )

    except Exception as e:
        return _ssh_error("Error deleting SSH key", e)


def handle_ssh_key_show_public(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Get the public key content for copy/paste.

    Args:
        args: Dict with name
        user: Authenticated user

    Returns:
        Dict with public key content
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    manager = get_ssh_key_manager()

    try:
        public_key = manager.get_public_key(name)
        return _mcp_response(
            {
                "success": True,
                "name": name,
                "public_key": public_key,
            }
        )

    except KeyNotFoundError:
        return _mcp_response({"success": False, "error": f"Key not found: {name}"})
    except Exception as e:
        return _ssh_error("Error getting public key", e)


def handle_ssh_key_assign_host(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Assign a host to an SSH key.

    Args:
        args: Dict with name and hostname
        user: Authenticated user

    Returns:
        Dict with updated key information
    """
    name = args.get("name")
    hostname = args.get("hostname")

    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )
    if not hostname:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: hostname"}
        )

    force = args.get("force", False)

    manager = get_ssh_key_manager()

    try:
        metadata = manager.assign_key_to_host(
            key_name=name,
            hostname=hostname,
            force=force,
        )
        payload = _metadata_payload(metadata)
        payload["success"] = True
        return _mcp_response(payload)

    except KeyNotFoundError:
        return _mcp_response({"success": False, "error": f"Key not found: {name}"})
    except HostConflictError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        return _ssh_error("Error assigning host to key", e)


def _register(registry: dict) -> None:
    """Register SSH key handlers into HANDLER_REGISTRY."""
    registry["cidx_ssh_key_create"] = handle_ssh_key_create
    registry["cidx_ssh_key_list"] = handle_ssh_key_list
    registry["cidx_ssh_key_delete"] = handle_ssh_key_delete
    registry["cidx_ssh_key_show_public"] = handle_ssh_key_show_public
    registry["cidx_ssh_key_assign_host"] = handle_ssh_key_assign_host
