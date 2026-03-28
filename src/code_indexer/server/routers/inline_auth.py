"""
Auth and API key route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 8 route handlers:
- POST /auth/login
- POST /auth/register
- POST /auth/reset-password
- POST /api/auth/refresh
- POST /auth/refresh
- POST /api/keys
- GET /api/keys
- DELETE /api/keys/{key_id}

Zero behavior change: same paths, methods, response models, and handler logic.
"""

from datetime import datetime, timezone

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Request,
    Body,
)

from ..models.auth import (
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    MessageResponse,
    RegistrationRequest,
    PasswordResetRequest,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    ApiKeyListResponse,
)

from ..auth import dependencies
from ..auth.user_manager import UserRole
import math

from ..auth.rate_limiter import refresh_token_rate_limiter
from ..auth.token_bucket import rate_limiter
from ..auth.audit_logger import password_audit_logger
from ..auth.auth_error_handler import auth_error_handler, AuthErrorType


def register_auth_routes(
    app: FastAPI,
    *,
    jwt_manager,
    user_manager,
    refresh_token_manager,
) -> None:
    """
    Register auth and API key route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures in register_inline_routes() before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        refresh_token_manager: RefreshTokenManager instance
    """

    @app.post("/auth/login", response_model=LoginResponse)
    def login(login_data: LoginRequest, request: Request):
        """
        Authenticate user and return JWT token with standardized security error responses.

        SECURITY FEATURES:
        - Generic error messages to prevent user enumeration
        - Timing attack prevention with constant response times (~100ms)
        - Dummy password hashing for non-existent users
        - Comprehensive audit logging of authentication attempts

        Args:
            login_data: Username and password
            request: HTTP request for client IP and user agent extraction

        Returns:
            JWT token and user information

        Raises:
            HTTPException: Generic "Invalid credentials" for all authentication failures
        """
        # Extract client information for audit logging
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent")

        # Story #555: Rate limit check BEFORE credential validation.
        # Uses the same TokenBucketManager singleton as MCP authenticate.
        allowed, retry_after = rate_limiter.consume(login_data.username)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later.",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )

        def authenticate_with_security():
            # Authenticate user
            user = user_manager.authenticate_user(
                login_data.username, login_data.password
            )

            if user is None:
                # Perform dummy password work to prevent timing-based user enumeration
                auth_error_handler.perform_dummy_password_work()

                # Create standardized error response with audit logging
                error_response = auth_error_handler.create_error_response(
                    AuthErrorType.INVALID_CREDENTIALS,
                    login_data.username,
                    internal_message=f"Authentication failed for username: {login_data.username}",
                    ip_address=client_ip,
                    user_agent=user_agent,
                )

                raise HTTPException(
                    status_code=error_response["status_code"],
                    detail=error_response["message"],
                    headers={"WWW-Authenticate": "Bearer"},
                )

            return user

        # Execute authentication with timing attack prevention
        user = auth_error_handler.timing_prevention.constant_time_execute(
            authenticate_with_security
        )

        # Story #555: Refund rate limit token on successful authentication.
        rate_limiter.refund(login_data.username)

        # Create JWT token and refresh token
        user_data = {
            "username": user.username,
            "role": user.role.value,
            "created_at": user.created_at.isoformat(),
        }

        # Create token family and initial refresh token
        family_id = refresh_token_manager.create_token_family(user.username)
        token_data = refresh_token_manager.create_initial_refresh_token(
            family_id=family_id, username=user.username, user_data=user_data
        )

        return LoginResponse(
            access_token=token_data["access_token"],
            token_type="bearer",
            user=user.to_dict(),
            refresh_token=token_data["refresh_token"],
            refresh_token_expires_in=token_data["refresh_token_expires_in"],
        )

    @app.post("/auth/register", response_model=MessageResponse)
    def register(registration_data: RegistrationRequest, request: Request):
        """
        Register new user account with standardized security responses.

        SECURITY FEATURES:
        - Generic success message regardless of account existence
        - Timing attack prevention with constant response times
        - No immediate indication of duplicate accounts
        - Comprehensive audit logging of registration attempts

        Args:
            registration_data: User registration information
            request: HTTP request for client IP and user agent extraction

        Returns:
            Generic success message for all registration attempts
        """
        # Extract client information for audit logging
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent")

        def process_registration():
            # Check if account already exists
            try:
                existing_user = user_manager.get_user(registration_data.username)
                account_exists = existing_user is not None
            except Exception:
                account_exists = False

            if account_exists:
                # Account exists - return generic success but don't create account
                response = auth_error_handler.create_registration_response(
                    email=registration_data.email,
                    account_exists=True,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                return response
            else:
                # New account - actually create the user
                try:
                    user_manager.create_user(
                        registration_data.username,
                        registration_data.password,
                        UserRole.NORMAL_USER,  # Default role for new registrations
                    )

                    response = auth_error_handler.create_registration_response(
                        email=registration_data.email,
                        account_exists=False,
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                    return response
                except Exception:
                    # Even if creation fails, return generic success
                    response = auth_error_handler.create_registration_response(
                        email=registration_data.email,
                        account_exists=False,
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                    return response

        # Execute registration with timing attack prevention
        response = auth_error_handler.timing_prevention.constant_time_execute(
            process_registration
        )

        return MessageResponse(message=response["message"])

    @app.post("/auth/reset-password", response_model=MessageResponse)
    def reset_password(reset_data: PasswordResetRequest, request: Request):
        """
        Initiate password reset process with standardized security responses.

        SECURITY FEATURES:
        - Generic success message regardless of account existence
        - Timing attack prevention with constant response times
        - No indication whether email corresponds to existing account
        - Comprehensive audit logging of reset attempts

        Args:
            reset_data: Password reset request information
            request: HTTP request for client IP and user agent extraction

        Returns:
            Generic message indicating email will be sent if account exists
        """
        # Extract client information for audit logging
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent")

        def process_password_reset():
            # Check if account exists (but don't reveal this to the client)
            try:
                # Note: This is a simplified implementation
                # In a real system, you'd look up users by email
                # For now, we'll simulate account existence check
                account_exists = (
                    False  # Placeholder - would check user database by email
                )

                if account_exists:
                    # Send actual password reset email
                    # TODO: Implement email sending functionality
                    pass
                else:
                    # Don't send email, but perform same timing work
                    pass

                response = auth_error_handler.create_password_reset_response(
                    email=reset_data.email,
                    account_exists=account_exists,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                return response
            except Exception:
                # Even if process fails, return generic success
                response = auth_error_handler.create_password_reset_response(
                    email=reset_data.email,
                    account_exists=False,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                return response

        # Execute password reset with timing attack prevention
        response = auth_error_handler.timing_prevention.constant_time_execute(
            process_password_reset
        )

        return MessageResponse(message=response["message"])

    @app.post("/api/auth/refresh", response_model=RefreshTokenResponse)
    def refresh_token_secure(
        refresh_request: RefreshTokenRequest,
        request: Request,
    ):
        """
        Secure token refresh endpoint with comprehensive security measures.

        SECURITY FEATURES:
        - Refresh token rotation (new access + refresh token pair)
        - Token family tracking for replay attack detection
        - Rate limiting (10 attempts, 5-minute lockout)
        - Comprehensive audit logging (success/failure/security incidents)
        - Concurrent refresh protection with 409 Conflict handling
        - Invalid/expired/revoked token rejection with 401 Unauthorized

        Args:
            refresh_request: Refresh token request containing refresh token
            request: HTTP request for extracting client IP and user agent
            current_user: Current authenticated user

        Returns:
            New access and refresh token pair with user information

        Raises:
            HTTPException: 401 for invalid tokens, 429 for rate limiting,
                          409 for concurrent refresh, 500 for other errors
        """
        # Extract client information for audit logging
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent")

        try:
            # First validate the refresh token to get user information
            # Validate and rotate refresh token
            result = refresh_token_manager.validate_and_rotate_refresh_token(
                refresh_token=refresh_request.refresh_token,
                client_ip=client_ip,
                user_manager=user_manager,
            )

            if not result["valid"]:
                # Get username from result for logging
                username = result.get("user_data", {}).get("username", "unknown")

                # Check rate limiting for failed attempts
                rate_limit_error = refresh_token_rate_limiter.check_rate_limit(username)
                if rate_limit_error:
                    # Get current attempt count for logging
                    attempt_count = refresh_token_rate_limiter.get_attempt_count(
                        username
                    )

                    # Log rate limit hit
                    password_audit_logger.log_rate_limit_triggered(
                        username=username,
                        ip_address=client_ip,
                        attempt_count=attempt_count,
                        user_agent=user_agent,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=rate_limit_error,
                    )

                # Record failed attempt for rate limiting
                should_lock = refresh_token_rate_limiter.record_failed_attempt(username)
                if should_lock:
                    # Get updated attempt count for logging (after recording failed attempt)
                    attempt_count = refresh_token_rate_limiter.get_attempt_count(
                        username
                    )

                    # Log lockout triggered
                    password_audit_logger.log_rate_limit_triggered(
                        username=username,
                        ip_address=client_ip,
                        attempt_count=attempt_count,
                        user_agent=user_agent,
                    )

                # Determine if this is a security incident
                is_security_incident = result.get("security_incident", False)

                # Log failed attempt
                password_audit_logger.log_token_refresh_failure(
                    username=username,
                    ip_address=client_ip,
                    reason=result["error"],
                    security_incident=is_security_incident,
                    user_agent=user_agent,
                    additional_context=result,
                )

                # Determine HTTP status code based on error type
                if "concurrent" in result["error"].lower():
                    status_code = status.HTTP_409_CONFLICT
                else:
                    status_code = status.HTTP_401_UNAUTHORIZED

                raise HTTPException(status_code=status_code, detail=result["error"])

            # Success - get username from successful result
            username = result["user_data"]["username"]

            # Success - clear rate limiting
            refresh_token_rate_limiter.record_successful_attempt(username)

            # Log successful refresh
            password_audit_logger.log_token_refresh_success(
                username=username,
                ip_address=client_ip,
                family_id=result["family_id"],
                user_agent=user_agent,
                additional_context={
                    "token_id": result["token_id"],
                    "parent_token_id": result["parent_token_id"],
                },
            )

            return RefreshTokenResponse(
                access_token=result["new_access_token"],
                refresh_token=result["new_refresh_token"],
                token_type="bearer",
                user=result["user_data"],
                access_token_expires_in=jwt_manager.token_expiration_minutes * 60,
                refresh_token_expires_in=refresh_token_manager.refresh_token_lifetime_days
                * 24
                * 60
                * 60,
            )

        except HTTPException:
            # Re-raise HTTP exceptions (they're already properly formatted)
            raise

        except Exception as e:
            # Log unexpected errors (username might not be defined if error occurred early)
            username_for_log = locals().get("username", "unknown")
            password_audit_logger.log_token_refresh_failure(
                username=username_for_log,
                ip_address=client_ip,
                reason=f"Internal error: {str(e)}",
                security_incident=True,
                user_agent=user_agent,
                additional_context={"error_type": type(e).__name__},
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Token refresh failed due to internal error",
            )

    @app.post("/auth/refresh", response_model=LoginResponse)
    def refresh_token(
        refresh_request: RefreshTokenRequest,
    ):
        """
        Refresh JWT token using refresh token.

        Args:
            refresh_request: Request containing refresh token

        Returns:
            New JWT token with extended expiration and user information

        Raises:
            HTTPException: If token refresh fails
        """
        try:
            # Use the refresh token manager to validate and create new tokens
            result = refresh_token_manager.validate_and_rotate_refresh_token(
                refresh_token=refresh_request.refresh_token, client_ip="unknown"
            )

            return LoginResponse(
                access_token=result["new_access_token"],
                token_type="bearer",
                user=result["user_data"],
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token refresh failed: {str(e)}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/api/keys", response_model=CreateApiKeyResponse, status_code=201)
    def create_api_key(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
        request: CreateApiKeyRequest = Body(...),
    ):
        """
        Generate a new API key for the authenticated user.

        Args:
            request: API key creation request
            current_user: Current authenticated user

        Returns:
            Generated API key with metadata (shown only once)

        Raises:
            HTTPException: If key generation fails
        """
        from code_indexer.server.auth.api_key_manager import ApiKeyManager

        try:
            api_key_manager = ApiKeyManager(user_manager=user_manager)
            name = request.name if request else None

            raw_key, key_id = api_key_manager.generate_key(
                username=current_user.username,
                name=name,
            )

            # Get the created_at timestamp from the stored key
            # Story #702 SQLite migration: Use public get_api_keys() API
            # instead of internal _load_users() to support SQLite mode.
            api_keys = user_manager.get_api_keys(current_user.username)
            created_at = None
            for key in api_keys:
                if key["key_id"] == key_id:
                    created_at = key["created_at"]
                    break

            return CreateApiKeyResponse(
                api_key=raw_key,
                key_id=key_id,
                name=name,
                created_at=created_at or datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"API key generation failed: {str(e)}",
            )

    @app.get("/api/keys", response_model=ApiKeyListResponse)
    def list_api_keys(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List all API keys for the authenticated user.

        Returns metadata only (key_id, name, created_at, key_prefix).
        Never returns hashes or full keys.
        """
        keys = user_manager.get_api_keys(current_user.username)
        return ApiKeyListResponse(keys=keys)

    @app.delete("/api/keys/{key_id}", status_code=200)
    def delete_api_key(
        key_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Delete an API key.

        Args:
            key_id: The unique identifier of the key to delete

        Returns:
            Success message

        Raises:
            HTTPException 404: If key not found
        """
        deleted = user_manager.delete_api_key(current_user.username, key_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="API key not found")
        return {"message": "API key deleted successfully"}
