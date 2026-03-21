"""
Admin user management route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 6 route handlers:
- GET /api/admin/users
- POST /api/admin/users
- PUT /api/admin/users/{username}
- DELETE /api/admin/users/{username}
- PUT /api/admin/users/{username}/change-password
- PUT /api/users/change-password

Zero behavior change: same paths, methods, response models, and handler logic.
"""

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Request,
)

from ..models.auth import (
    CreateUserRequest,
    UpdateUserRequest,
    ChangePasswordRequest,
    UserInfo,
    UserResponse,
    MessageResponse,
)

from ..auth import dependencies
from ..auth.user_manager import UserRole, SSOPasswordChangeError
from ..auth.rate_limiter import password_change_rate_limiter
from ..auth.audit_logger import password_audit_logger
from ..auth.session_manager import session_manager
from ..auth.timing_attack_prevention import timing_attack_prevention
from ..auth.concurrency_protection import (
    password_change_concurrency_protection,
    ConcurrencyConflictError,
)


def register_admin_user_routes(
    app: FastAPI,
    *,
    jwt_manager,
    user_manager,
    refresh_token_manager,
    db_path_str: str,
) -> None:
    """
    Register admin user management route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures in register_inline_routes() before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        refresh_token_manager: RefreshTokenManager instance
        db_path_str: Database path string
    """

    @app.get("/api/admin/users")
    def list_users(
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        List all users (admin only).

        Returns:
            List of all users
        """
        all_users = user_manager.get_all_users()
        return {
            "users": [user.to_dict() for user in all_users],
            "total": len(all_users),
        }

    @app.post("/api/admin/users", response_model=UserResponse, status_code=201)
    def create_user(
        user_data: CreateUserRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Create new user (admin only).

        Args:
            user_data: User creation data
            current_user: Current authenticated admin user

        Returns:
            Created user information

        Raises:
            HTTPException: If user creation fails
        """
        try:
            # Convert string role to UserRole enum
            role_enum = UserRole(user_data.role)

            # Create user through UserManager
            new_user = user_manager.create_user(
                username=user_data.username, password=user_data.password, role=role_enum
            )

            return UserResponse(
                user=UserInfo(
                    username=new_user.username,
                    role=new_user.role.value,
                    created_at=new_user.created_at.isoformat(),
                ),
                message=f"User '{user_data.username}' created successfully",
            )

        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    @app.put("/api/admin/users/{username}", response_model=MessageResponse)
    def update_user(
        username: str,
        user_data: UpdateUserRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Update user role (admin only).

        Args:
            username: Username to update
            user_data: User update data
            current_user: Current authenticated admin user

        Returns:
            Success message

        Raises:
            HTTPException: If user not found or update fails
        """
        # Check if user exists
        existing_user = user_manager.get_user(username)
        if existing_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {username}",
            )

        try:
            # Convert string role to UserRole enum
            role_enum = UserRole(user_data.role)

            # Update user role
            success = user_manager.update_user_role(username, role_enum)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User not found: {username}",
                )

            return MessageResponse(message=f"User '{username}' updated successfully")

        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    @app.delete("/api/admin/users/{username}", response_model=MessageResponse)
    def delete_user(
        username: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Delete user (admin only).

        Args:
            username: Username to delete
            current_user: Current authenticated admin user

        Returns:
            Success message

        Raises:
            HTTPException: If user not found or deletion would remove last admin
        """
        # Get user to check if it exists and get their role
        user_to_delete = user_manager.get_user(username)
        if user_to_delete is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {username}",
            )

        # CRITICAL SECURITY CHECK: Prevent deletion of last admin user
        # This prevents system lockout by ensuring at least one admin remains
        if user_to_delete.role == UserRole.ADMIN:
            all_users = user_manager.get_all_users()
            admin_count = sum(1 for user in all_users if user.role == UserRole.ADMIN)

            if admin_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot delete the last admin user. System requires at least one admin user to remain accessible.",
                )

        success = user_manager.delete_user(username)
        if not success:
            # This should not happen since we already checked user exists above,
            # but keeping for defensive programming
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {username}",
            )

        return MessageResponse(message=f"User '{username}' deleted successfully")

    @app.put("/api/users/change-password", response_model=MessageResponse)
    def change_current_user_password(
        password_data: ChangePasswordRequest,
        request: Request,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Secure password change endpoint with comprehensive security measures.

        SECURITY FEATURES:
        - Old password validation (fixes critical vulnerability)
        - Rate limiting (5 attempts, 15-minute lockout)
        - Timing attack prevention (constant response times)
        - Concurrent change protection (409 Conflict handling)
        - Comprehensive audit logging (success/failure/IP tracking)
        - Session invalidation (all user sessions invalidated after change)

        Args:
            password_data: Password change request with old and new passwords
            request: HTTP request for extracting client IP and user agent
            current_user: Current authenticated user

        Returns:
            Success message

        Raises:
            HTTPException: 401 for invalid old password, 429 for rate limiting,
                          409 for concurrent changes, 500 for other errors
        """
        username = current_user.username

        # Extract client information for audit logging
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent")

        # Check rate limiting first
        rate_limit_error = password_change_rate_limiter.check_rate_limit(username)
        if rate_limit_error:
            # Log rate limit hit
            password_audit_logger.log_rate_limit_triggered(
                username=username,
                ip_address=client_ip,
                attempt_count=password_change_rate_limiter.get_attempt_count(username),
                user_agent=user_agent,
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=rate_limit_error
            )

        # Acquire concurrency protection lock
        try:
            with password_change_concurrency_protection.acquire_password_change_lock(
                username
            ):

                def password_change_operation():
                    """Inner operation with timing attack prevention."""
                    # Get current user data for password verification
                    current_user_data = user_manager.get_user(username)
                    if not current_user_data:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"User not found: {username}",
                        )

                    # SECURITY FIX: Verify old password using constant-time comparison
                    old_password_valid = (
                        timing_attack_prevention.normalize_password_validation_timing(
                            user_manager.password_manager.verify_password,
                            password_data.old_password,
                            current_user_data.password_hash,
                        )
                    )

                    if not old_password_valid:
                        # Record failed attempt for rate limiting
                        should_lockout = (
                            password_change_rate_limiter.record_failed_attempt(username)
                        )

                        # Log failed attempt
                        password_audit_logger.log_password_change_failure(
                            username=username,
                            ip_address=client_ip,
                            reason="Invalid old password",
                            user_agent=user_agent,
                            additional_context={"should_lockout": should_lockout},
                        )

                        # Check if this attempt triggered rate limiting
                        if should_lockout:
                            # Log rate limit trigger event
                            password_audit_logger.log_rate_limit_triggered(
                                username=username,
                                ip_address=client_ip,
                                attempt_count=password_change_rate_limiter.get_attempt_count(
                                    username
                                ),
                                user_agent=user_agent,
                            )

                            raise HTTPException(
                                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="Too many failed attempts. Please try again in 15 minutes.",
                            )
                        else:
                            raise HTTPException(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid old password",
                            )

                    # Old password is valid - proceed with password change
                    success = user_manager.change_password(
                        username, password_data.new_password
                    )
                    if not success:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Password change failed due to internal error",
                        )

                    # Clear rate limiting on successful change
                    password_change_rate_limiter.record_successful_attempt(username)

                    # Invalidate all user sessions (except current one will remain valid)
                    session_manager.invalidate_all_user_sessions(username)

                    # Revoke all refresh tokens for security
                    revoked_families = refresh_token_manager.revoke_user_tokens(
                        username, "password_change"
                    )

                    # Log successful password change
                    password_audit_logger.log_password_change_success(
                        username=username,
                        ip_address=client_ip,
                        user_agent=user_agent,
                        additional_context={
                            "sessions_invalidated": True,
                            "refresh_token_families_revoked": revoked_families,
                        },
                    )

                    return "Password changed successfully"

                # Execute password change with timing attack prevention
                message = timing_attack_prevention.constant_time_execute(
                    password_change_operation
                )
                return MessageResponse(message=message)

        except ConcurrencyConflictError as e:
            # Log concurrent change conflict
            password_audit_logger.log_concurrent_change_conflict(
                username=username, ip_address=client_ip, user_agent=user_agent
            )

            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

        except HTTPException:
            # Re-raise HTTP exceptions (they're already properly formatted)
            raise

        except SSOPasswordChangeError as e:
            # Bug #68: SSO users cannot change passwords locally
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(e),
            )

        except Exception as e:
            # Log unexpected errors
            password_audit_logger.log_password_change_failure(
                username=username,
                ip_address=client_ip,
                reason=f"Internal error: {str(e)}",
                user_agent=user_agent,
                additional_context={"error_type": type(e).__name__},
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Password change failed due to internal error",
            )

    @app.put(
        "/api/admin/users/{username}/change-password", response_model=MessageResponse
    )
    def change_user_password(
        username: str,
        password_data: ChangePasswordRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Change any user's password (admin only).

        Args:
            username: Username whose password to change
            password_data: New password data
            current_user: Current authenticated admin user

        Returns:
            Success message

        Raises:
            HTTPException: If user not found
        """
        try:
            success = user_manager.change_password(username, password_data.new_password)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User not found: {username}",
                )

            return MessageResponse(
                message=f"Password changed successfully for user '{username}'"
            )

        except SSOPasswordChangeError as e:
            # Bug #68: SSO users cannot change passwords locally
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(e),
            )
