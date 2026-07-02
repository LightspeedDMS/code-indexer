"""
Timing attack prevention for password change operations.

Implements constant-time responses to prevent timing-based password guessing.
Following CLAUDE.md principles: NO MOCKS - Real timing attack prevention.
"""

import asyncio
import time
import hashlib
import secrets
from typing import Callable, Any


class TimingAttackPrevention:
    """
    Timing attack prevention for password operations.

    Security requirements:
    - Constant response times regardless of password validity
    - Prevent timing-based password enumeration
    - Minimal performance impact on legitimate operations
    """

    def __init__(self, minimum_response_time_ms: int = 400):
        """
        Initialize timing attack prevention.

        Args:
            minimum_response_time_ms: Minimum response time in milliseconds
        """
        self.minimum_response_time_seconds = minimum_response_time_ms / 1000.0

    def constant_time_execute(self, operation: Callable[[], Any]) -> Any:
        """
        Execute an operation with constant timing.

        Args:
            operation: Function to execute with constant timing

        Returns:
            Result of the operation
        """
        start_time = time.time()

        try:
            result = operation()
        except Exception as e:
            # Even if operation fails, maintain constant timing
            result = e

        # Calculate elapsed time
        elapsed_time = time.time() - start_time

        # Add delay to reach minimum response time.
        # IMPORTANT: time.sleep() is intentional and correct here.
        # All callers of constant_time_execute (login, register, reset_password,
        # change_password) are sync def route handlers that FastAPI runs in a
        # threadpool via run_in_executor. The sleep blocks the threadpool thread
        # (which is already dedicated to this request), NOT the asyncio event loop.
        # Do NOT change this to asyncio.sleep() - that would require converting
        # handlers to async def, which would then make ALL their other sync calls
        # (bcrypt, SQLite, file I/O) block the event loop directly.
        if elapsed_time < self.minimum_response_time_seconds:
            delay = self.minimum_response_time_seconds - elapsed_time
            time.sleep(delay)

        # If result is an exception, re-raise it
        if isinstance(result, Exception):
            raise result

        return result

    async def async_ensure_minimum_time(self, operation: Callable) -> Any:
        """
        Execute an async operation with constant timing using asyncio.sleep.

        This is the async-compatible variant of constant_time_execute().
        Use this from async contexts to avoid blocking the event loop during
        the timing padding delay. The operation can be an async def function.

        Args:
            operation: Async function to execute with minimum timing guarantee

        Returns:
            Result of the operation

        Raises:
            Any exception raised by the operation (after timing is padded)
        """
        start_time = time.time()

        operation_exception = None
        try:
            result = operation()
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            # Even if operation fails, maintain constant timing
            operation_exception = e
            result = None

        # Calculate elapsed time
        elapsed_time = time.time() - start_time

        # Add async delay to reach minimum response time (does NOT block event loop)
        if elapsed_time < self.minimum_response_time_seconds:
            delay = self.minimum_response_time_seconds - elapsed_time
            await asyncio.sleep(delay)

        # Re-raise any operation exception after timing is padded
        if operation_exception is not None:
            raise operation_exception

        return result

    def constant_time_compare(self, a: str, b: str) -> bool:
        """
        Constant-time string comparison to prevent timing attacks.

        Args:
            a: First string to compare
            b: Second string to compare

        Returns:
            True if strings are equal, False otherwise
        """
        # Use secrets.compare_digest for constant-time comparison
        return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    def generate_fake_work(self, work_factor: int = 100) -> None:
        """
        Generate fake computational work to normalize timing.

        Args:
            work_factor: Amount of fake work to perform
        """
        # Perform some meaningless but consistent work
        dummy_data = secrets.token_bytes(32)
        for _ in range(work_factor):
            hashlib.sha256(dummy_data).digest()

    def normalize_password_validation_timing(
        self,
        password_validator: Callable[[str, str], bool],
        plain_password: str,
        hashed_password: str,
    ) -> bool:
        """
        Normalize timing for password validation operations.

        Args:
            password_validator: Function that validates passwords
            plain_password: Plain text password to validate
            hashed_password: Hashed password to validate against

        Returns:
            True if password is valid, False otherwise
        """

        def validation_operation() -> bool:
            # Always perform validation (even if it might fail)
            result: bool = password_validator(plain_password, hashed_password)

            # Perform some fake work to normalize timing
            # This ensures that both success and failure paths take similar time
            self.generate_fake_work(200)

            return result

        validated_result: bool = self.constant_time_execute(validation_operation)
        return validated_result


# Global timing attack prevention instance
timing_attack_prevention = TimingAttackPrevention()
