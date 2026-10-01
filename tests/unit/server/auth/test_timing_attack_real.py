"""
Real Timing Attack Prevention Test Suite - Foundation #1 Compliant.

Tests timing attack prevention using real password validation without mocks.
No mocks for timing-critical functionality following MESSI Rule #1.
"""

import pytest
import statistics
import time
import bcrypt
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from unittest.mock import patch

from code_indexer.server.app import create_app
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.mark.e2e
class TestRealTimingAttackPrevention:
    """Test timing attack prevention with real password operations."""

    @pytest.fixture
    def client(self):
        """Create FastAPI test client."""
        app = create_app()
        return TestClient(app)

    @pytest.fixture
    def test_user_with_real_hash(self):
        """Create test user with real bcrypt hash."""
        # Create real bcrypt hash for testing
        real_password = "TestPassword123!"
        password_hash = bcrypt.hashpw(real_password.encode("utf-8"), bcrypt.gensalt())

        return {
            "user": User(
                username="timingtest",
                password_hash=password_hash.decode("utf-8"),
                role=UserRole.NORMAL_USER,
                created_at=datetime.now(timezone.utc),
            ),
            "correct_password": real_password,
            "wrong_password": "WrongPassword123!",
        }

    def test_timing_attack_prevention_real_password_validation(
        self, client, test_user_with_real_hash
    ):
        """
        SECURITY TEST: Real password validation timing should be constant.

        This test uses REAL password hashing and validation without mocks
        to verify timing attack prevention works with actual bcrypt operations.
        """
        # Clear rate limiter state to ensure clean test
        from code_indexer.server.auth.rate_limiter import password_change_rate_limiter

        password_change_rate_limiter._attempts.clear()

        test_data = test_user_with_real_hash
        invalid_times = []
        valid_times = []

        # Captured BEFORE dependencies.user_manager gets mocked below --
        # the route closure was bound to THIS object at create_app() time
        # (#1698: code_indexer.server.app.user_manager is unreachable from
        # register_admin_user_routes()'s closure-bound parameter). Its
        # .password_manager is already the REAL PasswordManager the
        # production UserManager constructs, so timing attack prevention
        # is exercised without any manual swap.
        real_user_manager = dependencies.user_manager

        # Mock only the authentication and user retrieval (not password validation)
        with patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt:
            with patch(
                "code_indexer.server.auth.dependencies.user_manager"
            ) as mock_dep_user_mgr:
                with (
                    patch.object(real_user_manager, "get_user") as mock_get_user,
                    patch.object(
                        real_user_manager, "change_password"
                    ) as mock_change_password,
                ):
                    # Mock JWT authentication
                    mock_jwt.validate_token.return_value = {
                        "username": "timingtest",
                        "role": "normal_user",
                        "exp": 9999999999,
                        "iat": 1234567890,
                    }

                    # Mock user retrieval for authentication
                    mock_dep_user_mgr.get_user.return_value = test_data["user"]
                    mock_get_user.return_value = test_data["user"]

                    # Mock only change_password to avoid actual password changes
                    mock_change_password.return_value = True

                    # Discarded warm-up request (correct password) to absorb the
                    # sporadic first-call cold-path spike (scheduling/GC/allocator
                    # event on the very first real bcrypt call through the route).
                    # PasswordChangeRateLimiter only counts FAILED attempts, so a
                    # successful warm-up does not consume the 3-attempt budget
                    # below. Its timing is intentionally NOT recorded.
                    warmup_response = client.put(
                        "/api/users/change-password",
                        headers={"Authorization": "Bearer valid.jwt.token"},
                        json={
                            "old_password": test_data["correct_password"],
                            "new_password": "NewSecure123!Pass",
                        },
                    )
                    assert warmup_response.status_code == 200, (
                        f"Warm-up request must succeed to properly prime the real "
                        f"bcrypt path, got {warmup_response.status_code}: "
                        f"{warmup_response.text}"
                    )

                    # Test with incorrect passwords (should use timing attack prevention)
                    # Use only 3 attempts to avoid rate limiting (limit is 5)
                    for i in range(3):
                        start_time = time.time()
                        response = client.put(
                            "/api/users/change-password",
                            headers={"Authorization": "Bearer valid.jwt.token"},
                            json={
                                "old_password": test_data["wrong_password"],
                                "new_password": "NewSecure123!Pass",
                            },
                        )
                        elapsed = time.time() - start_time
                        invalid_times.append(elapsed)

                        # Should fail with 401 (invalid old password)
                        assert response.status_code == 401
                        print(f"Invalid password attempt {i + 1}: {elapsed:.4f}s")

                    # Test with correct passwords (should use timing attack prevention)
                    for i in range(3):
                        start_time = time.time()
                        response = client.put(
                            "/api/users/change-password",
                            headers={"Authorization": "Bearer valid.jwt.token"},
                            json={
                                "old_password": test_data["correct_password"],
                                "new_password": "NewSecure123!Pass",
                            },
                        )
                        elapsed = time.time() - start_time
                        valid_times.append(elapsed)

                        # Should succeed with 200
                        assert response.status_code == 200
                        print(f"Valid password attempt {i + 1}: {elapsed:.4f}s")

        # SECURITY REQUIREMENT: compare BRANCH MEDIANS, not raw min/max over
        # pooled samples (#1698 round 3). constant_time_execute only pads UP
        # to a minimum -- it never caps a maximum -- so a single transient
        # scheduling/GC/allocator spike landing on ANY sample (proven to
        # happen at any position, not just the first) can blow up a raw
        # (max-min)/min ratio regardless of warm-up. The median of 3 samples
        # per branch is immune to one outlier in that branch (the outlier
        # can't move the middle value), and comparing branch medians directly
        # measures the real security property: a systematic timing
        # difference between the valid-password and invalid-password code
        # paths, not incidental jitter within either path.
        MAX_RELATIVE_MEDIAN_DIFFERENCE = 0.5
        median_invalid = statistics.median(invalid_times)
        median_valid = statistics.median(valid_times)
        median_diff = abs(median_valid - median_invalid)
        relative_diff = median_diff / min(median_valid, median_invalid)

        print(f"Invalid times: {[f'{t:.4f}s' for t in invalid_times]}")
        print(f"Valid times: {[f'{t:.4f}s' for t in valid_times]}")
        print(
            f"Median invalid: {median_invalid:.4f}s, Median valid: {median_valid:.4f}s"
        )
        print(
            f"Median timing difference: {relative_diff:.2%} "
            f"(target: <{MAX_RELATIVE_MEDIAN_DIFFERENCE:.0%})"
        )

        # Relative difference between branch medians should be minimal
        assert relative_diff < MAX_RELATIVE_MEDIAN_DIFFERENCE, (
            f"Median timing difference too large: {relative_diff:.2%} "
            f"(median valid={median_valid:.4f}s, median invalid={median_invalid:.4f}s)"
        )

    def test_timing_attack_prevention_unit_level(self):
        """
        UNIT TEST: Test timing attack prevention directly without HTTP overhead.
        """
        from code_indexer.server.auth.timing_attack_prevention import (
            timing_attack_prevention,
        )
        from code_indexer.server.auth.password_manager import PasswordManager

        password_manager = PasswordManager()

        # Create real password hash
        test_password = "RealTestPassword123!"
        wrong_password = "WrongPassword123!"
        password_hash = password_manager.hash_password(test_password)

        # NOTE (#1698): no separate warm-up call is needed here -- unlike
        # the HTTP-level sibling test, hash_password() immediately above
        # already performs a full bcrypt operation, so bcrypt is already
        # warm before the measurement loops below begin. (An earlier round
        # added a warm-up call here defensively; an A/B measurement proved
        # it made no statistically significant difference, so it was
        # removed rather than kept with an inaccurate "needed" rationale.)
        response_times = []

        # Test with wrong passwords (fast bcrypt failure)
        for i in range(5):
            start_time = time.time()
            result = timing_attack_prevention.normalize_password_validation_timing(
                password_manager.verify_password, wrong_password, password_hash
            )
            elapsed = time.time() - start_time
            response_times.append(elapsed)
            assert result is False
            print(f"Wrong password {i + 1}: {elapsed:.4f}s")

        # Test with correct passwords (full bcrypt verification)
        for i in range(5):
            start_time = time.time()
            result = timing_attack_prevention.normalize_password_validation_timing(
                password_manager.verify_password, test_password, password_hash
            )
            elapsed = time.time() - start_time
            response_times.append(elapsed)
            assert result is True
            print(f"Correct password {i + 1}: {elapsed:.4f}s")

        # SECURITY REQUIREMENT: Response time variation should be minimal
        min_time = min(response_times)
        max_time = max(response_times)
        time_variation = (max_time - min_time) / min_time

        print(f"Unit test timing variation: {time_variation:.2%} (target: <50%)")

        # Should have very low timing variation
        assert time_variation < 0.5, (
            f"Unit-level timing variation too large: {time_variation:.2%}"
        )
