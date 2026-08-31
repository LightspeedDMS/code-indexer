"""Story #1488: `cidx index --new-collection-layout` flag wiring.

The flag lets a CLI user (or the server's explicit spawn-site arg) choose
the on-disk chunk-storage layout for BRAND-NEW collections. An existing
collection's committed discriminator always wins (handled elsewhere); this
flag only governs the fresh-build default, mapped to the
FilesystemVectorStore constructor param via BackendFactory.
"""

import click
import pytest

from code_indexer.cli import _resolve_new_collection_layout, index


class TestResolveNewCollectionLayoutHelper:
    @pytest.mark.parametrize(
        "choice, expected",
        [
            (None, None),
            ("chunks_db", True),
            ("sharded_json", False),
        ],
    )
    def test_maps_choice_to_optional_bool(self, choice, expected) -> None:
        assert _resolve_new_collection_layout(choice) is expected


class TestIndexCommandOption:
    def _get_option(self):
        for param in index.params:
            if (
                isinstance(param, click.Option)
                and "--new-collection-layout" in param.opts
            ):
                return param
        return None

    def test_option_is_registered(self) -> None:
        assert self._get_option() is not None

    def test_option_choices_and_default(self) -> None:
        option = self._get_option()
        assert option is not None
        assert isinstance(option.type, click.Choice)
        assert set(option.type.choices) == {"sharded_json", "chunks_db"}
        assert option.default is None
