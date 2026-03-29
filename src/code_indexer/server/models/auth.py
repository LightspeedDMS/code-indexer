"""
Authentication and credential Pydantic models for CIDX Server API.

Extracted from app.py as part of Story #409 (app.py modularization).
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, field_validator

from ..auth.password_validator import (
    validate_password_complexity,
    get_password_complexity_error_message,
)
from ..auth.user_manager import UserRole


class LoginRequest(BaseModel):
    """Login request model with input validation."""

    username: str = Field(
        ..., min_length=1, max_length=255, description="Username for authentication"
    )
    password: str = Field(
        ..., min_length=1, max_length=1000, description="Password for authentication"
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate username is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Username cannot be empty or contain only whitespace")
        return v.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Password cannot be empty or contain only whitespace")
        return v


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: Dict[str, Any]
    refresh_token: Optional[str] = None
    refresh_token_expires_in: Optional[int] = None


class RefreshTokenRequest(BaseModel):
    """Request model for token refresh endpoint."""

    refresh_token: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Refresh token for token rotation",
    )

    @field_validator("refresh_token")
    @classmethod
    def validate_refresh_token(cls, v: str) -> str:
        """Validate refresh token is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Refresh token cannot be empty or contain only whitespace")
        return v.strip()


class RefreshTokenResponse(BaseModel):
    """Response model for token refresh endpoint."""

    access_token: str
    refresh_token: str
    token_type: str
    user: Dict[str, Any]
    access_token_expires_in: Optional[int] = None
    refresh_token_expires_in: Optional[int] = None


class UserInfo(BaseModel):
    username: str
    role: str
    created_at: str


class CreateUserRequest(BaseModel):
    """Request model for creating new user."""

    username: str = Field(
        ..., min_length=1, max_length=255, description="Username for new user"
    )
    password: str = Field(
        ..., min_length=1, max_length=1000, description="Password for new user"
    )
    role: str = Field(
        ..., description="Role for new user (admin, power_user, normal_user)"
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate username is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Username cannot be empty or contain only whitespace")
        return v.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password complexity."""
        if not v or not v.strip():
            raise ValueError("Password cannot be empty or contain only whitespace")
        if not validate_password_complexity(v):
            raise ValueError(get_password_complexity_error_message())
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        """Validate role is valid UserRole."""
        try:
            UserRole(v)
            return v
        except ValueError:
            raise ValueError(
                f"Invalid role. Must be one of: {', '.join([role.value for role in UserRole])}"
            )


class UpdateUserRequest(BaseModel):
    """Request model for updating user."""

    role: str = Field(
        ..., description="New role for user (admin, power_user, normal_user)"
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        """Validate role is valid UserRole."""
        try:
            UserRole(v)
            return v
        except ValueError:
            raise ValueError(
                f"Invalid role. Must be one of: {', '.join([role.value for role in UserRole])}"
            )


class ChangePasswordRequest(BaseModel):
    """Request model for changing password."""

    old_password: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Current password for verification",
    )
    new_password: str = Field(
        ..., min_length=1, max_length=1000, description="New password"
    )

    @field_validator("old_password")
    @classmethod
    def validate_old_password(cls, v: str) -> str:
        """Validate old password is not empty."""
        if not v or not v.strip():
            raise ValueError("Old password cannot be empty or contain only whitespace")
        return v

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        """Validate new password complexity."""
        if not v or not v.strip():
            raise ValueError("Password cannot be empty or contain only whitespace")
        if not validate_password_complexity(v):
            raise ValueError(get_password_complexity_error_message())
        return v


class UserResponse(BaseModel):
    """Response model for user operations."""

    user: UserInfo
    message: str


class MessageResponse(BaseModel):
    """Response model for simple messages."""

    message: str


class RegistrationRequest(BaseModel):
    """Request model for user registration."""

    username: str = Field(
        ..., min_length=1, max_length=50, description="Username for registration"
    )
    email: str = Field(..., min_length=1, max_length=255, description="Email address")
    password: str = Field(
        ..., min_length=1, max_length=1000, description="Password for new account"
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate username is not empty."""
        if not v or not v.strip():
            raise ValueError("Username cannot be empty or contain only whitespace")
        return v.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Validate email format."""
        if not v or not v.strip():
            raise ValueError("Email cannot be empty or contain only whitespace")
        # Basic email validation
        if "@" not in v or "." not in v:
            raise ValueError("Invalid email format")
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password complexity."""
        if not v or not v.strip():
            raise ValueError("Password cannot be empty or contain only whitespace")
        if not validate_password_complexity(v):
            raise ValueError(get_password_complexity_error_message())
        return v


class PasswordResetRequest(BaseModel):
    """Request model for password reset."""

    email: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Email address for password reset",
    )

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Validate email format."""
        if not v or not v.strip():
            raise ValueError("Email cannot be empty or contain only whitespace")
        if "@" not in v or "." not in v:
            raise ValueError("Invalid email format")
        return v.strip().lower()


class CreateApiKeyRequest(BaseModel):
    """Request model for API key creation."""

    name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional name for the API key",
    )


class CreateApiKeyResponse(BaseModel):
    """Response model for API key creation."""

    api_key: str = Field(..., description="The generated API key (shown only once)")
    key_id: str = Field(..., description="Unique identifier for the key")
    name: Optional[str] = Field(default=None, description="Name of the key")
    created_at: str = Field(..., description="ISO format timestamp of creation")
    message: str = Field(
        default="Save this key - it will not be shown again",
        description="Warning message to save the key",
    )


class ApiKeyListResponse(BaseModel):
    """Response model for listing API keys."""

    keys: List[Dict[str, Any]] = Field(..., description="List of API key metadata")


class CreateMCPCredentialRequest(BaseModel):
    """Request model for MCP credential creation."""

    name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional name for the MCP credential",
    )


class CreateMCPCredentialResponse(BaseModel):
    """Response model for MCP credential creation."""

    client_id: str = Field(..., description="The generated client_id (shown always)")
    client_secret: str = Field(
        ..., description="The generated client_secret (shown only once)"
    )
    credential_id: str = Field(..., description="Unique identifier for the credential")
    name: Optional[str] = Field(default=None, description="Name of the credential")
    created_at: str = Field(..., description="ISO format timestamp of creation")
    message: str = Field(
        default="Save this client_secret - it will not be shown again",
        description="Warning message to save the secret",
    )


class MCPCredentialListResponse(BaseModel):
    """Response model for listing MCP credentials."""

    credentials: List[Dict[str, Any]] = Field(
        ..., description="List of MCP credential metadata"
    )


class MfaChallengeResponse(BaseModel):
    """Response when login requires MFA verification (Story #561)."""

    mfa_required: bool = Field(
        default=True, description="Always True for MFA challenge"
    )
    mfa_token: str = Field(
        ..., description="Opaque token to submit with TOTP/recovery code"
    )


class MfaVerifyRequest(BaseModel):
    """Request to verify MFA code after login challenge (Story #561)."""

    mfa_token: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Challenge token from login response",
    )
    totp_code: Optional[str] = Field(
        default=None,
        min_length=6,
        max_length=6,
        description="6-digit TOTP code from authenticator app",
    )
    recovery_code: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="One-time recovery code",
    )
