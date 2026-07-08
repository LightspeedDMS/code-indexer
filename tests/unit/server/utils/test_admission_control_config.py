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


def test_per_consumer_defaults_disabled():
    c = AdmissionControlConfig()
    assert c.per_consumer_enabled is False
    assert c.per_consumer_burst == 30
    assert c.per_consumer_refill_per_second == 10.0
    assert c.per_consumer_cleanup_seconds == 3600


def test_parses_per_consumer_values():
    c = AdmissionControlConfig(
        **{
            "per_consumer_enabled": True,
            "per_consumer_burst": 5,
            "per_consumer_refill_per_second": 2.5,
            "per_consumer_cleanup_seconds": 60,
        }
    )
    assert c.per_consumer_enabled is True
    assert c.per_consumer_burst == 5
    assert c.per_consumer_refill_per_second == 2.5
    assert c.per_consumer_cleanup_seconds == 60
