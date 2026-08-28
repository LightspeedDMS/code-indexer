"""Regression guard for Bug #1721: Story #487 dual-embed family is unwired orphan code.

Bug #1710 removed the orphaned ``dual_embed_enabled`` config field. During its
review, an even wider unwired feature family from Story #487 was found:

- ``Config.secondary_provider`` (config.py) had zero attribute-access readers
  anywhere in src/ or tests/ -- every apparent "usage" was a same-named
  function keyword parameter (``embedder_chain.py``'s ``secondary_provider``
  parameter, ``cli.py``'s ``secondary_provider=secondary_embedder`` call
  sites), never ``config.secondary_provider``.
- ``src/code_indexer/services/dual_vector_calculation_manager.py``
  (``DualVectorCalculationManager``) had zero production consumers -- only
  its own definition, its own dedicated unit test file
  (``tests/unit/services/test_dual_vcm.py``), and a never-taken
  ``except FileNotFoundError`` fallback branch in
  ``tests/unit/server/services/test_provider_concurrency_governor_wiring.py``.

Both were confirmed genuinely abandoned (Messi Rule 12, Anti-Orphan-Code) and
deleted. These tests assert the deletion is real and does not silently
regress back in.
"""

import importlib

import pytest

_DUAL_VCM_MODULE = "code_indexer.services.dual_vector_calculation_manager"


class TestConfigSecondaryProviderFieldRemoved:
    """Config.secondary_provider must be genuinely gone, not just unused."""

    def test_config_has_no_secondary_provider_field(self):
        """Config's pydantic field set must not declare secondary_provider."""
        from code_indexer.config import Config

        assert "secondary_provider" not in Config.model_fields, (
            "Config.secondary_provider must be removed -- it has zero "
            "attribute-access readers anywhere in src/ or tests/ (Bug #1721)"
        )

    def test_config_instance_has_no_secondary_provider_attribute(self):
        """A constructed Config instance must not expose secondary_provider."""
        from code_indexer.config import Config

        cfg = Config()
        assert not hasattr(cfg, "secondary_provider"), (
            "Config() instances must not have a secondary_provider attribute"
        )


class TestDualVectorCalculationManagerRemoved:
    """DualVectorCalculationManager (Story #487) must be genuinely deleted."""

    def test_dual_vector_calculation_manager_module_does_not_exist(self):
        """The dual_vector_calculation_manager module must be gone entirely.

        Asserts the raised ModuleNotFoundError names exactly the target
        module (not some unrelated missing sub-import) so this test cannot
        false-pass.
        """
        with pytest.raises(ModuleNotFoundError) as exc_info:
            importlib.import_module(_DUAL_VCM_MODULE)
        assert exc_info.value.name == _DUAL_VCM_MODULE, (
            f"Expected ModuleNotFoundError for {_DUAL_VCM_MODULE!r} itself, "
            f"got missing module {exc_info.value.name!r} instead"
        )

    def test_dual_vcm_test_file_does_not_exist(self):
        """The dedicated unit test file for the deleted class must be gone."""
        from pathlib import Path

        repo_root = Path(__file__).parent.parent.parent.parent
        dead_test_file = repo_root / "tests" / "unit" / "services" / "test_dual_vcm.py"
        assert not dead_test_file.exists(), (
            "tests/unit/services/test_dual_vcm.py must be deleted alongside "
            "the DualVectorCalculationManager class it tested (Bug #1721)"
        )
