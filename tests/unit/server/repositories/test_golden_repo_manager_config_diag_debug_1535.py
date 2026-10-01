"""
Bug #1535 (second half): golden_repo_manager's leftover
"[config-init-diag] ... config.json present" probes log at WARNING on
EVERY successful golden-repo registration -- six such WARNING entries per
registration were observed in a single E2E session, all reporting the
EXPECTED state (config.json present after cidx init / before cidx index).

These probes were originally raised from DEBUG to WARNING (see
tests/e2e/log_audit_gate.py's allowlist comment) to diagnose a specific
config-init race. They must be demoted back to DEBUG for the "present"
case -- the happy-path outcome -- while the "ABSENT" case (a genuine
anomaly worth an operator's attention) is untouched by this bug and stays
at WARNING.

Fix: extract the duplicated "present" logging into one shared helper,
`_log_config_json_present_diagnostic`, that logs at DEBUG.
"""

import logging

from code_indexer.server.repositories.golden_repo_manager import (
    _log_config_json_present_diagnostic,
)


class TestConfigInitDiagPresentIsDebugNotWarning:
    def test_present_probe_logs_at_debug(self, caplog, tmp_path):
        config_json = tmp_path / "config.json"
        config_json.write_text("{}")

        with caplog.at_level(logging.DEBUG):
            _log_config_json_present_diagnostic("post-init", config_json)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warning_records, (
            "Expected no WARNING for the happy-path 'config.json present' "
            f"diagnostic, got: {[r.message for r in warning_records]}"
        )

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "[config-init-diag]" in r.getMessage()
            and "present" in r.getMessage()
        ]
        assert debug_records, "Expected a DEBUG record documenting the probe"

    def test_present_probe_with_cwd_logs_at_debug(self, caplog, tmp_path):
        config_json = tmp_path / "config.json"
        config_json.write_text("{}")

        with caplog.at_level(logging.DEBUG):
            _log_config_json_present_diagnostic(
                "pre-index", config_json, cwd=str(tmp_path)
            )

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warning_records, (
            "Expected no WARNING for the happy-path 'config.json present' "
            f"diagnostic (with cwd), got: {[r.message for r in warning_records]}"
        )
