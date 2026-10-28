from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

if TYPE_CHECKING:
    from code_indexer.config import Config, ConfigManager

OVERRIDE_FILENAME = ".code-indexer-override.yaml"
INIT_VOYAGE_TOKENS_PER_MINUTE = 1_000_000

_DEFAULT_OVERRIDE_CONTENT = """# Code-indexer override file
#
# This file allows you to override file inclusion/exclusion rules
# at the project level, with highest precedence over gitignore and config.
#
# Rules are applied in this order:
# 1. force_exclude_patterns (absolute exclusion, overrides everything)
# 2. force_include_patterns (overrides base exclusion)
# 3. Extension filtering (add_extensions, remove_extensions)
# 4. Directory filtering (add_exclude_dirs, add_include_dirs)
# 5. Base config and gitignore rules (lowest precedence)
#
# Patterns use gitignore syntax:
#   *.log        - matches all .log files
#   temp/        - matches temp directory
#   **/*.cache   - matches .cache files in any subdirectory
#   !important/  - negation (force include)

# Additional file extensions to index (beyond config defaults)
add_extensions: []

# File extensions to exclude (overrides config whitelist)
remove_extensions: []

# Additional directories to exclude from indexing
add_exclude_dirs: []

# Additional directories to force include
add_include_dirs: []

# Force include files matching these patterns (overrides gitignore/config)
force_include_patterns: []

# Force exclude files matching these patterns (absolute exclusion)
force_exclude_patterns: []
"""


@dataclass(frozen=True)
class LocalInitResult:
    config: "Config"
    config_manager: "ConfigManager"
    backend_error: Optional[Exception]
    override_file_created: bool
    language_mappings_created: bool


def create_default_override_file(project_dir: Path, force: bool = False) -> bool:
    override_path = project_dir / OVERRIDE_FILENAME
    if override_path.exists() and not force:
        return False
    override_path.write_text(_DEFAULT_OVERRIDE_CONTENT)
    return True


def _build_config(
    config_manager: "ConfigManager",
    target_dir: Path,
    embedding_provider: str,
    voyage_model: str,
    vector_store: Literal["filesystem"],
    max_file_size: Optional[int],
) -> "Config":
    from code_indexer.config import Config, VectorStoreConfig

    config = Config(codebase_dir=target_dir)
    config_manager._config = config
    updates: Dict[str, Any] = {"embedding_provider": embedding_provider}
    updates["vector_store"] = VectorStoreConfig(provider=vector_store).model_dump()
    if embedding_provider == "voyage-ai":
        voyage_ai_config = config.voyage_ai.model_dump()
        voyage_ai_config["model"] = voyage_model
        voyage_ai_config["tokens_per_minute"] = INIT_VOYAGE_TOKENS_PER_MINUTE
        updates["voyage_ai"] = voyage_ai_config
    if max_file_size is not None:
        indexing_config = config.indexing.model_dump()
        indexing_config["max_file_size"] = max_file_size
        updates["indexing"] = indexing_config
    return config_manager.update_config(**updates)


def _initialize_backend(config: "Config") -> Optional[Exception]:
    from code_indexer.backends.backend_factory import BackendFactory

    backend = BackendFactory.create(config=config, project_root=config.codebase_dir)
    try:
        backend.initialize()
    except Exception as error:
        return error
    return None


def _write_override_file(
    project_root: Path, force: bool, create_override_file: bool, no_override_file: bool
) -> bool:
    if no_override_file:
        return False
    if create_override_file or not (project_root / OVERRIDE_FILENAME).exists():
        return create_default_override_file(project_root, force=force)
    return False


def initialize_local_project(
    target_dir: Path,
    *,
    embedding_provider: str = "voyage-ai",
    voyage_model: str = "voyage-code-3",
    vector_store: Literal["filesystem"] = "filesystem",
    max_file_size: Optional[int] = None,
    force: bool = False,
    create_override_file: bool = False,
    no_override_file: bool = False,
) -> LocalInitResult:
    from code_indexer.config import ConfigManager
    from code_indexer.utils.yaml_utils import create_language_mappings_yaml

    config_manager = ConfigManager(target_dir / ".code-indexer" / "config.json")
    config = _build_config(
        config_manager,
        target_dir,
        embedding_provider,
        voyage_model,
        vector_store,
        max_file_size,
    )
    config_manager.save_with_documentation(config)
    backend_error = _initialize_backend(config)
    if config.vector_store and config.vector_store.provider == "filesystem":
        (config.codebase_dir / ".code-indexer" / "filesystem").mkdir(
            parents=True, exist_ok=True
        )
    override_file_created = _write_override_file(
        config.codebase_dir, force, create_override_file, no_override_file
    )
    language_mappings_created = create_language_mappings_yaml(
        config_manager.config_path.parent, force=force
    )
    return LocalInitResult(
        config=config,
        config_manager=config_manager,
        backend_error=backend_error,
        override_file_created=override_file_created,
        language_mappings_created=language_mappings_created,
    )
