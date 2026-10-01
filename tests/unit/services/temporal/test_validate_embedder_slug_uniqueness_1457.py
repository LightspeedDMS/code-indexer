"""validate_embedder_slug_uniqueness (Story #1457 AC6, round-13 Codex N13-1).

sanitize_model_name() is NOT injective -- e.g. 'foo-bar' and 'foo/bar' both
sanitize to 'foo_bar' -- and configuration only dedups EXACT string matches,
so two DIFFERENTLY-NAMED but IDENTICALLY-SANITIZING embedders can both be
configured. For the same repo and quarter they would then produce IDENTICAL
physical_name/pointer_namespace/sister .versioned namespace/resolved path,
silently overwriting each other -- directly violating AC6's Fix-3
"coexisting per-embedder shards never collide" guarantee.

validate_embedder_slug_uniqueness(embedders) is the shared, single-source-of-
truth guard: computes sanitize_model_name(name) for every configured
embedder, groups by resulting slug, and raises ValueError naming the
colliding embedder names AND the shared slug if any slug maps to more than
one configured name.
"""

from __future__ import annotations

import pytest

from code_indexer.services.temporal.temporal_collection_naming import (
    validate_embedder_slug_uniqueness,
)


@pytest.mark.parametrize(
    "embedders",
    [
        pytest.param([], id="empty_list"),
        pytest.param(["voyage-code-3"], id="single_embedder"),
        pytest.param(["voyage-code-3", "embed-v4.0"], id="distinct_slugs"),
    ],
)
def test_non_colliding_inputs_are_a_no_op(embedders):
    validate_embedder_slug_uniqueness(embedders)  # must not raise


def test_two_way_collision_raises_value_error_naming_embedders_and_shared_slug():
    with pytest.raises(ValueError) as exc_info:
        validate_embedder_slug_uniqueness(["foo-bar", "foo/bar"])

    message = str(exc_info.value)
    assert "foo-bar" in message
    assert "foo/bar" in message
    assert "foo_bar" in message  # the shared slug both collapse to


def test_three_way_collision_with_a_distinct_bystander_names_only_the_colliding_trio():
    with pytest.raises(ValueError) as exc_info:
        validate_embedder_slug_uniqueness(["a.b", "a-b", "a_b", "totally-different"])

    message = str(exc_info.value)
    assert "a.b" in message
    assert "a-b" in message
    assert "a_b" in message
