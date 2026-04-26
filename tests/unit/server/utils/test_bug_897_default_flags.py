"""
Regression tests for Bug #897 default flag values.

Since v9.23.3 both glibc arena-fragmentation mitigations ship enabled by
default so fresh installs automatically inherit the protections.  Operators
can still disable either flag by setting it to false in ~/.cidx-server/config.json.
"""

import pytest


@pytest.mark.parametrize("flag_name", ["enable_malloc_trim", "enable_malloc_arena_max"])
def test_bug_897_flags_default_to_true(tmp_path, flag_name):
    """Both glibc mitigation flags must default to True since v9.23.3 (Bug #897)."""
    from code_indexer.server.utils.config_manager import ServerConfig

    config = ServerConfig(server_dir=str(tmp_path))
    assert getattr(config, flag_name) is True, (
        f"ServerConfig.{flag_name} must default to True since v9.23.3 (Bug #897). "
        f"Operators who want to disable this mitigation must explicitly set {flag_name}=false "
        f"in ~/.cidx-server/config.json."
    )
