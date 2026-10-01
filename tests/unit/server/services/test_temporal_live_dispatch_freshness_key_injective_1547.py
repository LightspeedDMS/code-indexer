"""Bug #1547 round-2 hardening FIX 2 (RED): _freshness_cache_key must be
INJECTIVE over (username, repository_alias).

The pre-fix implementation was `f"{username}:{repository_alias}"`.
validate_username (models/auth.py) and validate_user_alias (models/repos.py)
reject only empty/whitespace -- neither forbids a literal colon -- so two
DIFFERENT (username, repository_alias) pairs can collide on the same
encoded string: username="a", repository_alias="b:c" and
username="a:b", repository_alias="c" both format to "a:b:c". Since these
two pairs resolve DIFFERENT temporal roots, a collision means one repo's
freshness signal (and therefore its TemporalFreshnessSignalCache entry) can
be served for a completely different repo/user pair.

Written BEFORE the fix: this test must genuinely fail against the
unmodified _freshness_cache_key (both pairs produce the identical string
"a:b:c").
"""

import types

from code_indexer.server.services.temporal_live_dispatch import (
    _freshness_cache_key,
)


def _fake_worker_input(username: str, repository_alias: str):
    """A minimal duck-typed stand-in for TemporalWorkerInput -- _freshness_
    cache_key only reads .username and .repository_alias, so a real
    TemporalWorkerInput (with its many other required fields) is
    unnecessary noise for this test."""
    return types.SimpleNamespace(username=username, repository_alias=repository_alias)


class TestFreshnessCacheKeyIsInjective:
    def test_ambiguous_colon_split_pairs_produce_different_keys(self):
        wi1 = _fake_worker_input(username="a", repository_alias="b:c")
        wi2 = _fake_worker_input(username="a:b", repository_alias="c")

        key1 = _freshness_cache_key(wi1)
        key2 = _freshness_cache_key(wi2)

        assert key1 != key2, (
            "Bug #1547 FIX 2: _freshness_cache_key must be injective -- "
            "username='a', repository_alias='b:c' and username='a:b', "
            "repository_alias='c' resolve DIFFERENT temporal roots and "
            f"must never produce the same cache key (both gave {key1!r})"
        )
