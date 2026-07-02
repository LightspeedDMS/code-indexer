"""Realistic Python module: async user service with type hints and modern patterns."""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime
from enum import Enum, auto
from typing import Any, AsyncIterator, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

CACHE_TTL_SECONDS = 300
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 0.1
MAX_NAME_LENGTH = 200
MAX_EMAIL_LENGTH = 255
MIN_SEARCH_QUERY_LENGTH = 2
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 100


class UserRole(Enum):
    ADMIN = auto()
    EDITOR = auto()
    VIEWER = auto()


class User:
    """Represents a system user."""

    def __init__(
        self,
        id: int,
        name: str,
        email: str,
        role: UserRole,
        created_at: Optional[datetime] = None,
        enabled: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.email = email
        self.role = role
        self.created_at = created_at or datetime.utcnow()
        self.enabled = enabled
        self.metadata: dict[str, Any] = metadata or {}

    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role.name,
            "created_at": self.created_at.isoformat(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "User":
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            email=str(data["email"]),
            role=UserRole[data.get("role", "VIEWER")],
            enabled=bool(data.get("enabled", True)),
        )


class CreateUserRequest:
    def __init__(self, name: str, email: str, role: UserRole = UserRole.VIEWER) -> None:
        self.name = name
        self.email = email
        self.role = role

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("name must not be blank")
        if "@" not in self.email:
            errors.append("email must contain @")
        if len(self.name) > MAX_NAME_LENGTH:
            errors.append(f"name must not exceed {MAX_NAME_LENGTH} characters")
        if len(self.email) > MAX_EMAIL_LENGTH:
            errors.append(f"email must not exceed {MAX_EMAIL_LENGTH} characters")
        return errors


class UpdateUserRequest:
    def __init__(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        role: Optional[UserRole] = None,
        enabled: Optional[bool] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.email = email
        self.role = role
        self.enabled = enabled
        self.metadata = metadata


class Page:
    def __init__(self, items: list[Any], total: int, page: int, size: int) -> None:
        self.items = items
        self.total = total
        self.page = page
        self.size = size

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.size - 1) // self.size)

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages - 1

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() if hasattr(i, "to_dict") else i for i in self.items],
            "total": self.total,
            "page": self.page,
            "size": self.size,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_prev": self.has_prev,
        }


class UserNotFoundError(Exception):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} not found")
        self.user_id = user_id


class EmailConflictError(Exception):
    def __init__(self, email: str) -> None:
        super().__init__(f"Email already registered: {email}")
        self.email = email


class ValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"Validation failed: {', '.join(errors)}")
        self.errors = errors


def retry(
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay: float = DEFAULT_RETRY_DELAY,
) -> Callable:
    """Decorator factory that retries async functions on transient errors."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception = RuntimeError("retry: no attempts made")
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except (ConnectionError, TimeoutError) as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(delay * (2**attempt))
                    logger.warning(
                        "Attempt %d/%d failed: %s", attempt + 1, max_attempts, exc
                    )
            raise last_exc

        return wrapper

    return decorator


def require_enabled(func: Callable) -> Callable:
    """Decorator that checks user.enabled before proceeding."""

    @functools.wraps(func)
    async def wrapper(self: Any, user_id: int, *args: Any, **kwargs: Any) -> Any:
        user = await self.get_user(user_id)
        if not user.enabled:
            raise PermissionError(f"User {user_id} is disabled")
        return await func(self, user_id, *args, **kwargs)

    return wrapper


class UserService:
    """Async user management service."""

    def __init__(self, db: Any, cache: Any | None = None) -> None:
        self._db = db
        self._cache = cache
        self._hooks: list[Callable[[str, User], None]] = []

    def on_event(self, hook: Callable[[str, User], None]) -> None:
        self._hooks.append(hook)

    def _emit(self, event: str, user: User) -> None:
        for hook in list(self._hooks):
            try:
                hook(event, user)
            except Exception as exc:
                logger.warning("Hook error on %s: %s", event, exc)

    # Applied via direct wrap to avoid decorator glob expansion in test hooks
    async def _get_user_impl(self, user_id: int) -> User:
        if self._cache is not None:
            cached = await self._cache.get(f"user:{user_id}")
            if cached is not None:
                return cached  # type: ignore[no-any-return]

        user = await self._db.find_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id)

        if self._cache is not None:
            await self._cache.set(f"user:{user_id}", user, ttl=CACHE_TTL_SECONDS)

        return user  # type: ignore[no-any-return]

    get_user = retry(max_attempts=DEFAULT_RETRY_ATTEMPTS)(_get_user_impl)

    async def list_users(
        self,
        page: int = 0,
        size: int = DEFAULT_PAGE_SIZE,
        role: Optional[UserRole] = None,
        enabled_only: bool = False,
    ) -> Page:
        effective_size = min(max(1, size), MAX_PAGE_SIZE)
        users, total = await self._db.find_page(
            page, effective_size, role=role, enabled_only=enabled_only
        )
        return Page(items=users, total=total, page=page, size=effective_size)

    async def create_user(self, request: CreateUserRequest) -> User:
        errors = request.validate()
        if errors:
            raise ValidationError(errors)

        existing = await self._db.find_by_email(request.email)
        if existing is not None:
            raise EmailConflictError(request.email)

        user = await self._db.insert(
            User(
                id=0,
                name=request.name.strip(),
                email=request.email.lower().strip(),
                role=request.role,
            )
        )
        self._emit("user.created", user)
        return user  # type: ignore[no-any-return]

    async def update_user(self, user_id: int, request: UpdateUserRequest) -> User:
        user = await self._get_user_impl(user_id)

        if request.email is not None and request.email != user.email:
            existing = await self._db.find_by_email(request.email)
            if existing is not None:
                raise EmailConflictError(request.email)
            user.email = request.email.lower().strip()

        if request.name is not None:
            user.name = request.name.strip()
        if request.role is not None:
            user.role = request.role
        if request.enabled is not None:
            user.enabled = request.enabled
        if request.metadata is not None:
            user.metadata.update(request.metadata)

        updated = await self._db.update(user)
        if self._cache is not None:
            await self._cache.delete(f"user:{user_id}")

        self._emit("user.updated", updated)
        return updated  # type: ignore[no-any-return]

    async def delete_user(self, user_id: int) -> None:
        user = await self._get_user_impl(user_id)
        await self._db.delete(user_id)
        if self._cache is not None:
            await self._cache.delete(f"user:{user_id}")
        self._emit("user.deleted", user)

    async def disable_user(self, user_id: int) -> User:
        return await self.update_user(user_id, UpdateUserRequest(enabled=False))

    async def enable_user(self, user_id: int) -> User:
        return await self.update_user(user_id, UpdateUserRequest(enabled=True))

    async def iter_users(self, role: Optional[UserRole] = None) -> AsyncIterator[User]:
        page = 0
        while True:
            result = await self.list_users(page=page, size=DEFAULT_PAGE_SIZE, role=role)
            for user in result.items:
                yield user
            if not result.has_next:
                break
            page += 1

    async def bulk_create(
        self, requests: list[CreateUserRequest]
    ) -> dict[str, "User | Exception"]:
        tasks = {
            req.email: asyncio.create_task(self.create_user(req)) for req in requests
        }
        results: dict[str, "User | Exception"] = {}
        for email, task in tasks.items():
            try:
                results[email] = await task
            except Exception as exc:
                results[email] = exc
        return results

    async def search_users(
        self, query: str, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> list[User]:
        if len(query) < MIN_SEARCH_QUERY_LENGTH:
            raise ValueError(
                f"Query must be at least {MIN_SEARCH_QUERY_LENGTH} characters"
            )
        limit = min(max(1, limit), MAX_SEARCH_LIMIT)
        return await self._db.full_text_search(query, limit)  # type: ignore[no-any-return]

    async def get_stats(self) -> dict[str, Any]:
        total = await self._db.count()
        by_role = {role.name: await self._db.count(role=role) for role in UserRole}
        enabled_count = await self._db.count(enabled=True)
        return {
            "total": total,
            "by_role": by_role,
            "enabled": enabled_count,
            "disabled": total - enabled_count,
        }
