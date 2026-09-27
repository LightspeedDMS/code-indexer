"""
_validate_config_section("server", ...) must reject an invalid `host`
value with a descriptive error string (matching the file's existing
"return an error string" idiom), not just check for an empty string
after strip().

This is the guard update_config_section relies on: a non-None return value
here causes a 4xx response via _create_config_page_response with nothing
persisted.
"""

from code_indexer.server.web.routes import _validate_config_section

_MULTILINE_HOST = "example.com\nExecStartPre=+/bin/true"


class TestValidateConfigSectionRejectsInvalidHost:
    def test_multiline_host_is_rejected(self):
        result = _validate_config_section("server", {"host": _MULTILINE_HOST})
        assert result is not None, (
            "_validate_config_section must reject a multi-line host value, "
            "returning a descriptive error string"
        )

    def test_legitimate_hosts_still_accepted(self):
        for host in ("0.0.0.0", "127.0.0.1", "localhost", "example.com", "::"):
            result = _validate_config_section("server", {"host": host})
            assert result is None, f"expected {host!r} to be accepted, got {result!r}"

    def test_host_with_space_is_rejected(self):
        result = _validate_config_section("server", {"host": "example.com other"})
        assert result is not None

    def test_host_with_percent_zone_id_is_rejected(self):
        result = _validate_config_section("server", {"host": "fe80::1%eth0"})
        assert result is not None
