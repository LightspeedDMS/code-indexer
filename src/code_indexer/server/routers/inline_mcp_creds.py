"""
MCP credential route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 7 route handlers:
- POST /api/mcp-credentials
- GET /api/mcp-credentials
- DELETE /api/mcp-credentials/{credential_id}
- GET /api/admin/users/{username}/mcp-credentials
- POST /api/admin/users/{username}/mcp-credentials
- DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}
- GET /api/admin/mcp-credentials

Zero behavior change: same paths, methods, response models, and handler logic.
"""

import logging

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Request,
    Body,
)

from ..models.auth import (
    CreateMCPCredentialRequest,
    CreateMCPCredentialResponse,
    MCPCredentialListResponse,
)

from ..auth import dependencies
from ..middleware.correlation import get_correlation_id

logger = logging.getLogger(__name__)


def register_mcp_credential_routes(
    app: FastAPI,
    *,
    jwt_manager,
    user_manager,
    mcp_credential_manager,
    mcp_registration_service,
) -> None:
    """
    Register MCP credential route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures in register_inline_routes() before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        mcp_credential_manager: MCPCredentialManager instance
        mcp_registration_service: MCPSelfRegistrationService instance
    """

    @app.post(
        "/api/mcp-credentials",
        response_model=CreateMCPCredentialResponse,
        status_code=201,
    )
    def create_mcp_credential(
        current_user: dependencies.User = Depends(
            dependencies.get_current_user_web_or_api
        ),
        request: CreateMCPCredentialRequest = Body(...),
    ):
        """
        Generate a new MCP client credential for the authenticated user.

        Returns:
            CreateMCPCredentialResponse: Generated client_id and client_secret with metadata
                (client_secret shown only once)

        Raises:
            HTTPException: If credential generation fails
        """
        from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

        try:
            mcp_manager = MCPCredentialManager(user_manager=user_manager)
            name = request.name if request else None

            result = mcp_manager.generate_credential(
                user_id=current_user.username,
                name=name,
            )

            return CreateMCPCredentialResponse(
                client_id=result["client_id"],
                client_secret=result["client_secret"],
                credential_id=result["credential_id"],
                name=result["name"],
                created_at=result["created_at"],
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"MCP credential generation failed: {str(e)}",
            )

    @app.get("/api/mcp-credentials", response_model=MCPCredentialListResponse)
    def list_mcp_credentials(
        current_user: dependencies.User = Depends(
            dependencies.get_current_user_web_or_api
        ),
    ):
        """
        List all MCP credentials for the authenticated user.

        Returns metadata only (credential_id, client_id, client_id_prefix, name, created_at, last_used_at).
        Never returns hashes or full secrets.
        """
        credentials = user_manager.get_mcp_credentials(current_user.username)
        return MCPCredentialListResponse(credentials=credentials)

    @app.delete("/api/mcp-credentials/{credential_id}", status_code=200)
    def delete_mcp_credential(
        credential_id: str,
        current_user: dependencies.User = Depends(
            dependencies.get_current_user_web_or_api
        ),
    ):
        """
        Delete an MCP credential.

        Args:
            credential_id: Credential ID to delete

        Returns:
            Success message

        Raises:
            HTTPException 404: If credential not found
        """
        from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

        mcp_manager = MCPCredentialManager(user_manager=user_manager)
        deleted = mcp_manager.revoke_credential(current_user.username, credential_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="MCP credential not found")
        return {"message": "MCP credential deleted successfully"}

    # Admin MCP Credentials endpoints (require admin role)
    @app.get("/api/admin/users/{username}/mcp-credentials")
    def admin_list_user_mcp_credentials(
        username: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """Admin endpoint to list a user's MCP credentials.

        Args:
            username: Username to list credentials for
            current_user: Current authenticated admin user

        Returns:
            List of MCP credentials for the user

        Raises:
            HTTPException 404: If user not found
        """
        from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

        target_user = user_manager.get_user(username)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        mcp_manager = MCPCredentialManager(user_manager=user_manager)
        credentials = mcp_manager.get_credentials(target_user.username)

        # Audit logging
        logger.info(
            "Admin listed user MCP credentials",
            extra={
                "admin_user": current_user.username,
                "target_user": username,
                "action": "list_mcp_credentials",
                "correlation_id": get_correlation_id(),
            },
        )

        return {"credentials": credentials, "username": username}

    @app.post("/api/admin/users/{username}/mcp-credentials", status_code=201)
    async def admin_create_user_mcp_credential(
        username: str,
        request: Request,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """Admin endpoint to create MCP credential for a user.

        Args:
            username: Username to create credential for
            request: Request object with JSON body containing optional "name"
            current_user: Current authenticated admin user

        Returns:
            Created MCP credential with client_id and client_secret

        Raises:
            HTTPException 404: If user not found
        """
        from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

        target_user = user_manager.get_user(username)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        body = await request.json()
        name = body.get("name")

        mcp_manager = MCPCredentialManager(user_manager=user_manager)
        credential = mcp_manager.generate_credential(target_user.username, name)

        # Audit logging
        logger.info(
            "Admin created MCP credential",
            extra={
                "admin_user": current_user.username,
                "target_user": username,
                "credential_id": credential["credential_id"],
                "action": "create_mcp_credential",
                "correlation_id": get_correlation_id(),
            },
        )

        return {
            "credential_id": credential["credential_id"],
            "client_id": credential["client_id"],
            "client_secret": credential["client_secret"],
            "name": credential.get("name"),
            "created_at": credential["created_at"],
        }

    @app.delete("/api/admin/users/{username}/mcp-credentials/{credential_id}")
    def admin_revoke_user_mcp_credential(
        username: str,
        credential_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """Admin endpoint to revoke a user's MCP credential.

        Args:
            username: Username owning the credential
            credential_id: ID of credential to revoke
            current_user: Current authenticated admin user

        Returns:
            Success message

        Raises:
            HTTPException 404: If user or credential not found
        """
        from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

        target_user = user_manager.get_user(username)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        mcp_manager = MCPCredentialManager(user_manager=user_manager)
        success = mcp_manager.revoke_credential(target_user.username, credential_id)
        if not success:
            raise HTTPException(status_code=404, detail="Credential not found")

        # Audit logging
        logger.info(
            "Admin revoked MCP credential",
            extra={
                "admin_user": current_user.username,
                "target_user": username,
                "credential_id": credential_id,
                "action": "revoke_mcp_credential",
                "correlation_id": get_correlation_id(),
            },
        )

        return {"message": "Credential revoked successfully"}

    @app.get("/api/admin/mcp-credentials")
    def admin_list_all_mcp_credentials(
        limit: int = 100,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """Admin endpoint to list all MCP credentials across all users.

        Args:
            limit: Maximum number of credentials to return
            current_user: Current authenticated admin user

        Returns:
            List of all MCP credentials with username information
        """
        all_credentials = user_manager.list_all_mcp_credentials(limit=limit)
        return {"credentials": all_credentials, "total": len(all_credentials)}
