"""Manifest and contribution validation, and the Feature protocol shape."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest

import cidx_sdk
from cidx_sdk import (
    BaseFeature,
    ConfigKey,
    ConfigSection,
    Feature,
    FeatureManifest,
    HealthReport,
    IndexBuilderContribution,
    IndexBuildOutcome,
    IndexBuildRequest,
    JobTypeContribution,
    Migration,
    SearchAugmenterContribution,
    SearchAugmentRequest,
    ToolContribution,
    WebQueryModeContribution,
    WebQueryOutcome,
    WebQueryRequest,
)
from cidx_sdk.feature import INDEX_BUILD_RUN_LOCALLY, INDEX_BUILD_SKIPPED
from cidx_sdk.capabilities import CONFIG, KNOWN_CAPABILITIES
from cidx_sdk.errors import InvalidManifestError, SeamUnavailableError
from cidx_sdk.seams import require_seam_provider, seam_entry_point_group

_SRC = Path(cidx_sdk.__file__).resolve().parent


def _manifest(**overrides: Any) -> FeatureManifest:
    values: Dict[str, Any] = {
        "name": "example",
        "version": "1.0.0",
        "requires_sdk": ">=0.1,<0.2",
    }
    values.update(overrides)
    return FeatureManifest(**values)


def test_valid_manifest_normalises_sequences_to_tuples() -> None:
    manifest = _manifest(required_capabilities=[CONFIG], depends_on=["base"])
    assert manifest.required_capabilities == (CONFIG,)
    assert manifest.depends_on == ("base",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Bad Name"},
        {"name": ""},
        {"version": "1.0-beta"},
        {"requires_sdk": "any"},
        {"required_capabilities": ["not-a-capability"]},
        {"required_capabilities": "config"},
        {"optional_seams": ["no-such-seam"]},
        {"depends_on": ["example"]},
        {"depends_on": ["dup", "dup"]},
    ],
)
def test_invalid_manifest_rejected(overrides: Dict[str, Any]) -> None:
    with pytest.raises(InvalidManifestError):
        _manifest(**overrides)


def test_base_feature_satisfies_protocol_with_empty_hooks() -> None:
    feature = BaseFeature(_manifest())
    assert isinstance(feature, Feature)
    assert feature.tools() == ()
    assert feature.config_section() is None
    assert feature.health() == ()
    assert feature.job_types() == ()
    assert feature.index_builders() == ()
    assert feature.web_query_modes() == ()
    assert feature.search_augmenters() == ()


def test_object_missing_hooks_is_not_a_feature() -> None:
    class Partial:
        manifest = _manifest()

    assert not isinstance(Partial(), Feature)


def test_contribution_validation() -> None:
    with pytest.raises(InvalidManifestError):
        ToolContribution(name="t", handler=cast(Any, "nope"), doc_markdown="")
    with pytest.raises(InvalidManifestError):
        Migration(version=0, description="x", apply=lambda conn: None)
    with pytest.raises(InvalidManifestError):
        HealthReport(component="c", status="fine")
    with pytest.raises(InvalidManifestError):
        ConfigSection(name="s", keys=[ConfigKey("a", 1), ConfigKey("a", 2)])
    section = ConfigSection(
        name="s", keys=[ConfigKey("boot", 1, bootstrap=True), ConfigKey("rt", 2)]
    )
    assert section.bootstrap_keys == {"boot"}
    assert section.runtime_keys == {"rt"}


def test_config_presentation_fields_default_to_the_names() -> None:
    """SDK 0.7: a label, a restart flag and a section title/description for
    the generic config screen; all optional."""
    plain = ConfigKey("rt", 2)
    assert (plain.label, plain.restart_required) == ("", False)
    key = ConfigKey("rt", 2, label="Run time", restart_required=True)
    assert (key.label, key.restart_required) == ("Run time", True)
    section = ConfigSection(name="s", keys=[key])
    assert (section.title, section.description) == ("", "")
    titled = ConfigSection(name="s", keys=[key], title="S", description="About s.")
    assert (titled.title, titled.description) == ("S", "About s.")
    with pytest.raises(InvalidManifestError):
        ConfigKey("rt", 2, restart_required=cast(Any, "yes"))


def test_tool_dispatch_policy_defaults_to_the_generic_timeout() -> None:
    tool = ToolContribution(name="t", handler=lambda p, u: {}, doc_markdown="")
    assert tool.manages_own_timeout is False
    own = ToolContribution(
        name="t", handler=lambda p, u: {}, doc_markdown="", manages_own_timeout=True
    )
    assert own.manages_own_timeout is True
    with pytest.raises(InvalidManifestError):
        ToolContribution(
            name="t",
            handler=lambda p, u: {},
            doc_markdown="",
            manages_own_timeout=cast(Any, "yes"),
        )


def test_job_type_contribution() -> None:
    job_type = JobTypeContribution("example_search", hidden_from_dashboard=True)
    assert job_type.hidden_from_dashboard and not job_type.repo_alias_optional
    with pytest.raises(InvalidManifestError):
        JobTypeContribution("")
    with pytest.raises(InvalidManifestError):
        JobTypeContribution("x", repo_alias_optional=cast(Any, 1))


def test_index_builder_contribution() -> None:
    def build(request: IndexBuildRequest) -> IndexBuildOutcome:
        return IndexBuildOutcome(INDEX_BUILD_RUN_LOCALLY, ["cidx", "example"])

    builder = IndexBuilderContribution(index_type="example", build=build)
    assert builder.builds_locally is None and builder.index_present is None
    outcome = builder.build(IndexBuildRequest("a", "/r"))
    assert outcome.status == "run_locally" and outcome.command == ("cidx", "example")
    assert IndexBuildOutcome(INDEX_BUILD_SKIPPED, detail="why").detail == "why"
    with pytest.raises(InvalidManifestError):
        IndexBuildOutcome("done")
    with pytest.raises(InvalidManifestError):
        IndexBuildOutcome(INDEX_BUILD_RUN_LOCALLY)
    with pytest.raises(InvalidManifestError):
        IndexBuildOutcome(INDEX_BUILD_SKIPPED, ["cidx"])
    with pytest.raises(InvalidManifestError):
        IndexBuildOutcome(INDEX_BUILD_RUN_LOCALLY, "cidx")
    with pytest.raises(InvalidManifestError):
        IndexBuilderContribution(index_type="Bad Type", build=build)
    with pytest.raises(InvalidManifestError):
        IndexBuilderContribution(index_type="x", build=cast(Any, None))
    with pytest.raises(InvalidManifestError):
        IndexBuilderContribution(
            index_type="x", build=build, index_present=cast(Any, "no")
        )


def test_web_query_mode_contribution() -> None:
    def execute(request: WebQueryRequest) -> WebQueryOutcome:
        return WebQueryOutcome(rows=[{"file_path": request.repository_alias}])

    mode = WebQueryModeContribution(mode="example", execute=execute)
    outcome = mode.execute(WebQueryRequest(object(), "u", "repo", "/r", "q", limit=5))
    assert outcome.rows == ({"file_path": "repo"},) and outcome.error is None
    with pytest.raises(InvalidManifestError):
        WebQueryModeContribution(mode="Bad Mode", execute=execute)
    with pytest.raises(InvalidManifestError):
        WebQueryModeContribution(mode="x", execute=cast(Any, None))


def test_search_augmenter_contribution() -> None:
    def augment(request: SearchAugmentRequest) -> Any:
        return [{"memory_id": request.username, "dims": len(request.query_vector)}]

    augmenter = SearchAugmenterContribution(
        key="relevant_memories",
        search_modes=["semantic", "hybrid"],
        is_active=lambda: True,
        augment=augment,
    )
    assert augmenter.search_modes == ("semantic", "hybrid")
    request = SearchAugmentRequest(
        username="u",
        query_text="q",
        search_mode="semantic",
        limit=5,
        query_vector=[0.1, 0.2],
    )
    assert request.query_vector == (0.1, 0.2)
    assert augmenter.augment(request) == [{"memory_id": "u", "dims": 2}]
    with pytest.raises(InvalidManifestError):
        SearchAugmenterContribution(
            key="Bad Key",
            search_modes=["semantic"],
            is_active=lambda: True,
            augment=augment,
        )
    with pytest.raises(InvalidManifestError):
        SearchAugmenterContribution(
            key="x", search_modes=[], is_active=lambda: True, augment=augment
        )
    with pytest.raises(InvalidManifestError):
        SearchAugmenterContribution(
            key="x",
            search_modes=["semantic"],
            is_active=cast(Any, None),
            augment=augment,
        )


def test_seam_helper_fails_loud_naming_the_seam() -> None:
    assert seam_entry_point_group("git") == "code_indexer.git_backend"
    with pytest.raises(SeamUnavailableError, match="'content'"):
        require_seam_provider("content", "delegated", [])
    with pytest.raises(SeamUnavailableError, match="exactly one"):
        require_seam_provider("git", "local", ["a", "b"])
    assert seam_entry_point_group("clone") == "code_indexer.clone_backend"
    with pytest.raises(SeamUnavailableError):
        seam_entry_point_group("no-such-seam")
    assert require_seam_provider("git", "delegated", ["gitmcp"]) == "gitmcp"


def test_known_capabilities_are_the_documented_set() -> None:
    assert "config" in KNOWN_CAPABILITIES and "payload_cache" in KNOWN_CAPABILITIES


def _imported_top_levels() -> List[str]:
    names: List[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.append(node.module.split(".")[0])
    return names


def test_sdk_imports_only_stdlib_and_itself() -> None:
    """Zero runtime dependencies, and parseable under Python 3.9 grammar."""
    # Python 3.10+ (the SDK's floor); getattr keeps a 3.9 type-check pass quiet.
    allowed = set(getattr(sys, "stdlib_module_names")) | {"cidx_sdk"}
    assert set(_imported_top_levels()) - allowed == set()
