"""Server-spawned indexing and server queries use server-managed provider
endpoints; repository configuration cannot select them.

A repository's own ".code-indexer/config.json" is loaded like any other
part of its configuration for both server-spawned indexing and server-side
query/search. Two fields in that file are excluded from repository control
when the server is the one performing the operation on the repository's
behalf: the embedding-provider endpoint (voyage_ai.api_endpoint /
cohere.api_endpoint) and daemon-mode delegation (daemon.enabled). This
module is the single seam that resets exactly those fields to fixed,
server-managed values on an already-loaded Config -- leaving every other
provider setting (model, timeout, retries, parallelism, ...) exactly as the
repository or the server's own config-seeding overlay set it. It also marks
the Config as server context (``Config.confine_to_codebase_root``), so file
discovery and read-time checks keep every indexed file inside the
repository root; local CLI indexing, which never passes through this seam,
follows symlinks outside the root.

Wired at the "cidx index" CLI entrypoint (cli.py) behind the hidden
--server-managed-provider-settings flag, which every server spawn site
inherits automatically via append_server_layout_args
(index_command_layout.py) -- standalone CLI use (flag absent) is
byte-for-byte unaffected -- and unconditionally at the server's repo-config
load points for queries (search_service.py, multi_search_service.py,
semantic_query_manager.py), which only ever run in server context.
"""

from __future__ import annotations

from code_indexer.config import CohereConfig, Config, VoyageAIConfig


def enforce_server_managed_provider_settings(config: Config) -> None:
    """Reset repo-overridable transport fields on a Config to server-managed
    values, in place.

    Args:
        config: An already-loaded Config (e.g. from
            ConfigManager.load()). Mutated in place; not returned.
    """
    config.voyage_ai.api_endpoint = VoyageAIConfig().api_endpoint
    config.cohere.api_endpoint = CohereConfig().api_endpoint
    if config.daemon is not None:
        config.daemon.enabled = False
    # Server context: indexed and read files stay inside the repository root.
    config.confine_to_codebase_root()
