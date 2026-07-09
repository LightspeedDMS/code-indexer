"""Unit tests for is_immutable_versioned_snapshot and provider_config_digest.

Story #1082: the predicate is the ONLY gate that may route a key to a NO-TTL
cache; the digest must distinguish ALL behavior-affecting provider-config fields
while never embedding the raw API secret.
"""

from code_indexer.server.services.query_path_cache import (
    api_key_fingerprint,
    is_immutable_versioned_snapshot,
    provider_config_digest,
)


# --------------------------- predicate: accepts ---------------------------


def test_predicate_accepts_versioned_snapshot_path():
    assert is_immutable_versioned_snapshot(
        "/data/golden-repos/.versioned/flask/v_1700000000"
    )


def test_predicate_accepts_versioned_snapshot_with_subpath():
    # A path deeper inside the snapshot still resolves as immutable.
    assert is_immutable_versioned_snapshot(
        "/data/golden-repos/.versioned/my-repo/v_42/.code-indexer"
    )


# --------------------------- predicate: rejects ---------------------------


def test_predicate_rejects_mutable_base_clone():
    # Priority-1 mutable base clone (what get_actual_repo_path returns commonly).
    assert not is_immutable_versioned_snapshot("/data/golden-repos/flask")


def test_predicate_rejects_activated_cow_clone():
    assert not is_immutable_versioned_snapshot("/data/activated-repos/alice/flask")


def test_predicate_rejects_arbitrary_path():
    assert not is_immutable_versioned_snapshot("/tmp/some/random/dir")


def test_predicate_rejects_empty_path():
    assert not is_immutable_versioned_snapshot("")


def test_predicate_rejects_traversal_token():
    assert not is_immutable_versioned_snapshot(
        "/data/golden-repos/.versioned/../flask/v_1"
    )


def test_predicate_rejects_versioned_without_version_segment():
    # .versioned/{alias} but no v_* directory underneath.
    assert not is_immutable_versioned_snapshot("/data/golden-repos/.versioned/flask")


def test_predicate_rejects_non_v_version_segment():
    assert not is_immutable_versioned_snapshot(
        "/data/golden-repos/.versioned/flask/snapshot1"
    )


def test_predicate_rejects_v_segment_without_digits():
    assert not is_immutable_versioned_snapshot(
        "/data/golden-repos/.versioned/flask/v_latest"
    )


def test_predicate_rejects_empty_alias_segment():
    assert not is_immutable_versioned_snapshot("/data/golden-repos/.versioned//v_1")


# --------------------------- digest: fingerprint ---------------------------


def test_fingerprint_never_returns_raw_secret():
    secret = "voyage-super-secret-key-abc123"
    fp = api_key_fingerprint(secret)
    assert secret not in fp
    assert fp != secret
    assert len(fp) == 16


def test_fingerprint_stable_for_same_key():
    assert api_key_fingerprint("k") == api_key_fingerprint("k")


def test_fingerprint_distinct_for_different_keys():
    assert api_key_fingerprint("key-a") != api_key_fingerprint("key-b")


def test_fingerprint_none_and_empty_map_to_sentinel():
    assert api_key_fingerprint(None) == "nokey"
    assert api_key_fingerprint("") == "nokey"


# --------------------------- digest: distinctness ---------------------------


def _base_kwargs(**overrides):
    kwargs = dict(
        provider="voyage-ai",
        model="voyage-code-3",
        api_key="secret-key",
        api_endpoint="https://api.voyageai.com/v1/embeddings",
        connect_timeout=5,
        timeout=30,
        max_retries=3,
        retry_delay=1.0,
        exponential_backoff=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_digest_stable_for_identical_inputs():
    assert provider_config_digest(**_base_kwargs()) == provider_config_digest(
        **_base_kwargs()
    )


def test_digest_never_contains_raw_secret():
    secret = "raw-secret-must-not-leak"
    digest = provider_config_digest(**_base_kwargs(api_key=secret))
    assert secret not in digest


def test_digest_distinct_on_endpoint_change():
    a = provider_config_digest(**_base_kwargs())
    b = provider_config_digest(
        **_base_kwargs(api_endpoint="https://proxy.internal/v1/embeddings")
    )
    assert a != b


def test_digest_distinct_on_timeout_change():
    a = provider_config_digest(**_base_kwargs())
    assert a != provider_config_digest(**_base_kwargs(timeout=60))
    assert a != provider_config_digest(**_base_kwargs(connect_timeout=10))


def test_digest_distinct_on_model_change():
    a = provider_config_digest(**_base_kwargs())
    assert a != provider_config_digest(**_base_kwargs(model="voyage-large-2"))


def test_digest_distinct_on_key_change():
    a = provider_config_digest(**_base_kwargs(api_key="key-1"))
    b = provider_config_digest(**_base_kwargs(api_key="key-2"))
    assert a != b


def test_digest_distinct_on_cohere_retry_fields():
    a = provider_config_digest(**_base_kwargs())
    assert a != provider_config_digest(**_base_kwargs(max_retries=5))
    assert a != provider_config_digest(**_base_kwargs(retry_delay=2.0))
    assert a != provider_config_digest(**_base_kwargs(exponential_backoff=False))


def test_digest_same_provider_model_key_different_settings_do_not_share():
    """Two repos: same provider/model/key, different endpoint+timeouts+retries."""
    repo_a = provider_config_digest(
        provider="cohere",
        model="embed-v4.0",
        api_key="shared-key",
        api_endpoint="https://api.cohere.com/v2/embed",
        connect_timeout=5,
        timeout=30,
        max_retries=3,
        retry_delay=1.0,
        exponential_backoff=True,
    )
    repo_b = provider_config_digest(
        provider="cohere",
        model="embed-v4.0",
        api_key="shared-key",
        api_endpoint="https://proxy.example/v2/embed",
        connect_timeout=10,
        timeout=60,
        max_retries=5,
        retry_delay=2.0,
        exponential_backoff=False,
    )
    assert repo_a != repo_b
