"""Confirmation tokens for destructive git operations (hard reset, clean,
branch delete), kept in the cluster-shared PayloadCache.

Invariants:
  - Tokens live only in the shared PayloadCache (SQLite in solo mode,
    PostgreSQL in a cluster), never in process memory, so a token issued by
    any worker or node can be redeemed by any other.
  - A token is bound to (username, repository alias, operation, operation
    parameters). The store key is a SHA-256 digest over the token and that
    binding, so redeeming under any other binding finds nothing.
  - A token is redeemed at most once: redemption is one atomic consume on
    the shared store, so of concurrent redemptions exactly one succeeds.
  - A token lives exactly TOKEN_EXPIRY seconds, whatever the cache's
    configured TTL, measured on the store's own clock for both issue and
    redemption.
  - A token carries 128 random bits and is never stored or logged in clear.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from code_indexer.server.cache.payload_cache import PayloadCache

TOKEN_EXPIRY = 300  # seconds a confirmation token stays redeemable
TOKEN_BYTES = 16  # 128 bits of randomness per token
_KEY_PREFIX = "git-confirm:"


@dataclass(frozen=True)
class ConfirmationBinding:
    """What a confirmation token authorizes: one operation, with these
    parameters, on one repository alias, for one user."""

    username: str
    repo_alias: str
    operation: str
    params: Tuple[Tuple[str, str], ...] = ()

    @classmethod
    def of(
        cls,
        operation: str,
        username: Optional[str],
        repo_alias: Optional[str],
        **params: str,
    ) -> "ConfirmationBinding":
        """Build a binding; a token is never issued or redeemed without a
        user and a repository alias."""
        if not username or not repo_alias:
            raise ValueError(
                f"{operation} confirmation requires a username and a repository alias"
            )
        return cls(username, repo_alias, operation, tuple(sorted(params.items())))

    def store_key(self, token: str) -> str:
        material = json.dumps(
            [token, self.username, self.repo_alias, self.operation, self.params],
            separators=(",", ":"),
        )
        return _KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def issue_confirmation_token(
    cache: "PayloadCache", binding: ConfirmationBinding
) -> str:
    """Issue a fresh token bound to `binding`, redeemable for exactly
    TOKEN_EXPIRY seconds. A store failure propagates: no token is returned
    that the store does not hold."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    cache.store_expiring_key(binding.store_key(token), binding.operation, TOKEN_EXPIRY)
    return token


def redeem_confirmation_token(
    cache: "PayloadCache", binding: ConfirmationBinding, token: str
) -> bool:
    """Consume `token` for `binding`. True exactly once per issued token,
    and only for the binding it was issued for and before it expires."""
    return cache.consume_key(binding.store_key(token))
