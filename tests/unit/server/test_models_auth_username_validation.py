"""
Tests for RegistrationRequest / CreateUserRequest username path-safety
validation (front-door 4xx).

These Pydantic field_validators are the FIRST gate a request meets -- an
unsafe username must fail with a pydantic ValidationError (surfaced by
FastAPI as HTTP 422) before UserManager.create_user is ever called, so no
row is created and no timing/audit side effect from account creation
occurs for an invalid username.

Foundation #1 compliant: real Pydantic model validation, no mocks.
"""

import pytest
from pydantic import ValidationError

from code_indexer.server.models.auth import RegistrationRequest, CreateUserRequest


class TestRegistrationRequestRejectsUnsafeUsername:
    def test_dotdot_username_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RegistrationRequest(
                username="..",
                email="attacker@example.com",
                password="SecurePass123!@#",
            )

    def test_slash_username_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RegistrationRequest(
                username="../../etc/passwd",
                email="attacker@example.com",
                password="SecurePass123!@#",
            )

    def test_legitimate_username_still_accepted(self):
        req = RegistrationRequest(
            username="alice",
            email="alice@example.com",
            password="SecurePass123!@#",
        )
        assert req.username == "alice"


class TestCreateUserRequestRejectsUnsafeUsername:
    def test_dotdot_username_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CreateUserRequest(
                username="..",
                password="SecurePass123!@#",
                role="normal_user",
            )

    def test_backslash_username_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CreateUserRequest(
                username="a\\b",
                password="SecurePass123!@#",
                role="normal_user",
            )

    def test_legitimate_username_still_accepted(self):
        req = CreateUserRequest(
            username="bob.smith",
            password="SecurePass123!@#",
            role="normal_user",
        )
        assert req.username == "bob.smith"
