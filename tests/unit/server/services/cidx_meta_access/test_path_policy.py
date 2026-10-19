"""classify(): what a caller may do with one cidx-meta path (deny by default).

Real cidx-meta tree and group store from the shared fixture; no mocks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet, Optional

import pytest

from code_indexer.server.services.cidx_meta_access import (
    DecisionKind,
    MetaVisibility,
    classify,
    domain_participants,
    memory_visibility_fn,
    participants_reader,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    MEMORY_ALPHA,
    MEMORY_CANARY,
    MEMORY_GLOBAL,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

ALLOW = DecisionKind.ALLOW
DESCRIPTION = DecisionKind.DESCRIPTION
TRANSFORM = DecisionKind.TRANSFORM
MEMORY = DecisionKind.MEMORY
DENY = DecisionKind.DENY


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    """Shared, never mutated: read-only classification cases."""
    return build_cidx_meta_fixture(tmp_path_factory.mktemp("path-policy"))


@pytest.fixture
def fresh_fx(tmp_path: Path) -> CidxMetaFixture:
    """Per-test tree for cases that write to it or count one-time warnings."""
    return build_cidx_meta_fixture(tmp_path)


def _restricted_classify(fx: CidxMetaFixture, path: str) -> DecisionKind:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    return classify(
        path,
        vis,
        participants_reader(fx.dep_map_dir),
        memory_visibility_fn(fx.access_service, vis),
    ).kind


@pytest.mark.parametrize(
    "path, expected",
    [
        (f"{ALPHA}.md", DESCRIPTION),
        (f"./{ALPHA}.md", DESCRIPTION),
        (f"{CANARY}.md", DENY),
        ("gamma.md", DENY),
        ("README.md", DENY),
        ("dependency-map/payments.md", TRANSFORM),
        ("dependency-map/ledger.md", TRANSFORM),
        ("dependency-map//payments.md", TRANSFORM),
        (f"dependency-map/{CANARY}.md", DENY),
        ("dependency-map/inventory.md", DENY),
        ("dependency-map/_index.md", TRANSFORM),
        ("dependency-map/_dep_types.md", ALLOW),
        ("dependency-map/_analysis_guidelines.md", ALLOW),
        ("dependency-map/_domains.json", DENY),
        ("dependency-map/_journal.json", DENY),
        ("dependency-map/_activity.md", DENY),
        ("dependency-map/_other.md", DENY),
        ("dependency-map/x.jsonl", DENY),
        ("xray-patterns/__any__/p.yaml", ALLOW),
        (f"xray-patterns/{CANARY}/p.yaml", DENY),
        (f"xray-patterns/{ALPHA}/p.yaml", ALLOW),
        ("xray-patterns/__any__/p.yaml.tmp", DENY),
        (".git/config", DENY),
        (".GIT/config", DENY),
        (".code-indexer/index/meta.json", DENY),
        (".locks/write.lock", DENY),
        (".gitignore", DENY),
        (".self-check-probe", DENY),
        ("dependency-map.staging/payments.md", DENY),
        ("dependency-map.old/payments.md", DENY),
        (f"{ALPHA}.md.tmp", DENY),
        (f"../{ALPHA}.md", DENY),
        (f"/{ALPHA}.md", DENY),
        (f"dependency-map/../{ALPHA}.md", DENY),
        ("", DENY),
        ("alpha\x00.md", DENY),
        (f"{ALPHA}.md\\", DENY),
        ("dependency-map\\payments.md", DENY),
        (f"\\{ALPHA}.md", DENY),
        (f"memories\\{MEMORY_GLOBAL}.md", DENY),
        ("notes/alpha.md", DENY),
        (f"memories/{MEMORY_GLOBAL}.md", MEMORY),
        (f"memories/{MEMORY_ALPHA}.md", MEMORY),
        (f"memories/{MEMORY_CANARY}.md", DENY),
        ("memories/not-a-memory.md", DENY),
        (f"{MEMORY_GLOBAL}.md", DENY),
    ],
)
def test_restricted_user_classification(
    fx: CidxMetaFixture, path: str, expected: DecisionKind
) -> None:
    assert _restricted_classify(fx, path) is expected


def test_classification_uses_full_relative_path(fx: CidxMetaFixture) -> None:
    # A file named after a visible alias, but not at the root, is no description.
    assert _restricted_classify(fx, f"dependency-map/{ALPHA}.md") is DENY
    assert _restricted_classify(fx, f"{ALPHA}.md") is DESCRIPTION


def test_alias_granted_to_no_group_is_denied(fx: CidxMetaFixture) -> None:
    decision = classify(
        "gamma.md",
        resolve_meta_visibility(RESTRICTED_USER, fx.access_service),
        participants_reader(fx.dep_map_dir),
        lambda _memory_id: False,
    )
    assert decision.kind is DENY


def test_decision_names_its_subject(fx: CidxMetaFixture) -> None:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    reader = participants_reader(fx.dep_map_dir)
    assert classify(f"{ALPHA}.md", vis, reader, lambda _m: False).subject == ALPHA
    domain = classify("dependency-map/payments.md", vis, reader, lambda _m: False)
    assert domain.subject == "payments"


def test_admins_group_member_is_unrestricted_without_io(fx: CidxMetaFixture) -> None:
    def _no_io(_domain: str) -> Optional[FrozenSet[str]]:
        raise AssertionError("participants must not be read for an admin")

    def _no_memory(_memory_id: str) -> bool:
        raise AssertionError("memories must not be read for an admin")

    vis = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    for path in (".git/config", f"{CANARY}.md", "dependency-map/inventory.md", ""):
        assert classify(path, vis, _no_io, _no_memory).kind is ALLOW


def test_legacy_domain_is_denied_with_one_warning(
    fresh_fx: CidxMetaFixture, caplog: pytest.LogCaptureFixture
) -> None:
    fx = fresh_fx
    reader = participants_reader(fx.dep_map_dir)
    vis = MetaVisibility.restricted([ALPHA])
    with caplog.at_level(logging.WARNING):
        first = classify("dependency-map/legacy.md", vis, reader, lambda _m: False)
        second = classify("dependency-map/legacy.md", vis, reader, lambda _m: False)
    assert first.kind is DENY and second.kind is DENY
    warnings = [r for r in caplog.records if "legacy" in r.getMessage()]
    assert len(warnings) == 1


def test_memory_visibility_fn_applies_scope_rule_and_fails_closed(
    fresh_fx: CidxMetaFixture,
) -> None:
    fx = fresh_fx
    restricted = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    memory_fn = memory_visibility_fn(fx.access_service, restricted)
    assert memory_fn(MEMORY_CANARY) is False
    assert memory_fn(MEMORY_GLOBAL) is True
    assert memory_fn(MEMORY_ALPHA) is True

    admin = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert memory_visibility_fn(fx.access_service, admin)(MEMORY_CANARY) is True

    no_service = memory_visibility_fn(None, restricted)
    assert [no_service(m) for m in (MEMORY_GLOBAL, MEMORY_ALPHA)] == [False, False]

    missing = "d4" * 16
    assert memory_fn(missing) is False
    unparseable = "e5" * 16
    (fx.root / "memories" / f"{unparseable}.md").write_text(
        "---\nscope: [global\n---\nbody\n", encoding="utf-8"
    )
    assert memory_fn(unparseable) is False


def test_membership_comes_from_frontmatter_then_domains_json(
    fx: CidxMetaFixture,
) -> None:
    # reports.md has unparseable frontmatter membership; _domains.json supplies it.
    assert domain_participants(fx.dep_map_dir, "payments") == {ALPHA, CANARY}
    assert domain_participants(fx.dep_map_dir, "reports") == {ALPHA}
    assert domain_participants(fx.dep_map_dir, "legacy") is None
    assert domain_participants(fx.dep_map_dir, "absent") is None


def test_membership_change_on_disk_is_seen_by_the_next_read(
    fresh_fx: CidxMetaFixture,
) -> None:
    fx = fresh_fx
    reader = participants_reader(fx.dep_map_dir)
    vis = MetaVisibility.restricted([ALPHA])
    assert (
        classify("dependency-map/ledger.md", vis, reader, lambda _m: False).kind
        is TRANSFORM
    )
    ledger = fx.dep_map_dir / "ledger.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"  - {ALPHA}\n", f"  - {CANARY}\n", 1
        ),
        encoding="utf-8",
    )
    assert (
        classify("dependency-map/ledger.md", vis, reader, lambda _m: False).kind is DENY
    )
