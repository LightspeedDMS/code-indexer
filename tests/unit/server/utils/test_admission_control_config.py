"""C7 admission control config: default off, parses from config dict."""

from code_indexer.server.utils.config_manager import (
    AdmissionControlConfig,
    ServerConfig,
)


def test_defaults_disabled_with_sane_cap():
    c = AdmissionControlConfig()
    assert c.enabled is False
    assert c.max_inflight_requests == 100
    assert c.retry_after_seconds == 1


def test_server_config_defaults_the_section():
    sc = ServerConfig(server_dir="/tmp/x")
    assert sc.admission_control_config is not None
    assert sc.admission_control_config.enabled is False


def test_parses_explicit_values():
    c = AdmissionControlConfig(
        **{"enabled": True, "max_inflight_requests": 25, "retry_after_seconds": 5}
    )
    assert c.enabled is True
    assert c.max_inflight_requests == 25
    assert c.retry_after_seconds == 5
