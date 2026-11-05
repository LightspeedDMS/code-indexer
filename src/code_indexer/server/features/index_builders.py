"""Consume ``index_builders()``: the index types enabled features build.

Validates every contribution, then installs them in the kernel registry
(``code_indexer.kernel.index_builders``) that the indexing pipeline reads.
Replaced as a whole whenever an app is built, so building an app without a
feature removes its index types: the pipeline then builds nothing for them.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from cidx_sdk import FeatureLoadError, IndexBuilderContribution

from code_indexer.kernel.index_builders import install_index_builders
from code_indexer.server.features.loader import LoadedFeature

#: The index types the core builds itself; a feature cannot contribute one.
CORE_INDEX_TYPES = frozenset({"semantic", "fts", "temporal"})


def install_feature_index_builders(loaded: Sequence[LoadedFeature]) -> List[str]:
    """Replace the installed index builders with those of *loaded*.

    Raises:
        FeatureLoadError: a builder names a core index type, or two features
            contribute the same type. Nothing is installed then.
    """
    owner: Dict[str, str] = {}
    builders: List[IndexBuilderContribution] = []
    for item in loaded:
        feature = item.manifest.name
        for builder in item.contributions.index_builders:
            index_type = builder.index_type
            if index_type in CORE_INDEX_TYPES:
                raise FeatureLoadError(
                    f"feature {feature!r} index type {index_type!r} is a core "
                    "index type"
                )
            if index_type in owner:
                raise FeatureLoadError(
                    f"index type {index_type!r} is contributed by both "
                    f"{owner[index_type]!r} and {feature!r}"
                )
            owner[index_type] = feature
            builders.append(builder)
    install_index_builders(builders)
    return sorted(owner)
