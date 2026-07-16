"""
Bug #1399 MODERATE item 6: extend RESTART_REQUIRED_FIELDS (web/routes.py) to
cover every remaining MODERATE-classified field so the operator is at least
told a restart is needed, until/unless each is individually converted to
true hot-reload.

Per the issue, these fields are captured ONCE at server-startup construction
time (singleton init / module globals / middleware wiring) and never
re-consulted afterward -- changing them via the Web UI silently has no
effect until the next restart, with (before this fix) no hint in the UI.

Explicitly EXCLUDED per the issue's own scoping (already correctly handled
or genuinely live -- do NOT add):
  - background_jobs.max_concurrent_background_jobs (already has a hint)
  - server.host/port/workers/log_level (already have hints)
  - telemetry_enabled / langfuse_enabled (top-level kill-switches, already
    hinted; only the SUB-fields are newly added here)
  - langfuse pull-sync fields (pull_enabled, pull_host, pull_trace_age_days,
    pull_max_concurrent_observations, pull_projects) -- correctly re-read
    live every sync cycle
  - codex_integration.enabled / codex_weight -- correctly read live per
    dispatched job
  - multi_search_max_workers / scip_multi_max_workers -- already have hints
  - golden_repos.analysis_model's *interactive* Research Assistant path is
    live; only the scheduler path is MODERATE (single field, no split
    possible in this flat list)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class TestRestartRequiredFieldsPreExistingEntriesPreserved:
    """Guards against accidentally removing entries that already work."""

    def test_pre_existing_entries_still_present(self):
        from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS

        pre_existing = [
            "host",
            "port",
            "telemetry_enabled",
            "langfuse_enabled",
            "max_concurrent_claude_cli",
            "multi_search_max_workers",
            "scip_multi_max_workers",
            "max_concurrent_background_jobs",
            "subprocess_max_workers",
            "dependency_map_enabled",
            "workers",
            "log_level",
        ]
        for field in pre_existing:
            assert field in RESTART_REQUIRED_FIELDS, (
                f"Pre-existing RESTART_REQUIRED_FIELDS entry {field!r} must "
                "not be removed."
            )


class TestRestartRequiredFieldsModerateExtension:
    """New MODERATE-classified fields must be added (Bug #1399 item 6)."""

    def test_all_new_moderate_fields_present(self):
        from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS

        new_moderate_fields = [
            # password_security.*
            "min_length",
            "max_length",
            "required_char_classes",
            "min_entropy_bits",
            # server.jwt_expiration_minutes
            "jwt_expiration_minutes",
            # health.* (5 fields)
            "memory_warning_threshold_percent",
            "memory_critical_threshold_percent",
            "disk_warning_threshold_percent",
            "disk_critical_threshold_percent",
            "cpu_sustained_threshold_percent",
            # error_handling.*
            "max_retry_attempts",
            "base_retry_delay_seconds",
            "max_retry_delay_seconds",
            # telemetry.* sub-fields (NOT telemetry_enabled)
            "collector_endpoint",
            "collector_protocol",
            "service_name",
            "export_traces",
            "export_metrics",
            "machine_metrics_enabled",
            "machine_metrics_interval_seconds",
            "deployment_environment",
            # langfuse.* tracing-credential fields (NOT pull-sync fields)
            "public_key",
            "secret_key",
            "auto_trace_enabled",
            # codex_integration.* provisioning fields (NOT enabled/codex_weight)
            "credential_mode",
            "api_key",
            "lcp_url",
            "lcp_vendor",
            # scip_cleanup.scip_workspace_retention_days
            "scip_workspace_retention_days",
            # server.coalesce_k_min/coalesce_k_max
            "coalesce_k_min",
            "coalesce_k_max",
            # golden_repos.analysis_model (scheduler path only)
            "analysis_model",
            # multi_search timeout fields (NOT *_max_workers)
            "multi_search_timeout_seconds",
            "scip_multi_timeout_seconds",
            # scip.* limit/depth fields
            "scip_reference_limit",
            "scip_dependency_depth",
            "scip_callchain_max_depth",
            "scip_callchain_limit",
            # claude_cli.scheduled_catchup_*
            "scheduled_catchup_enabled",
            "scheduled_catchup_interval_minutes",
            # cache payload_* fields
            "payload_preview_size_chars",
            "payload_max_fetch_size_chars",
            "payload_cache_ttl_seconds",
            "payload_cleanup_interval_seconds",
        ]
        missing = [f for f in new_moderate_fields if f not in RESTART_REQUIRED_FIELDS]
        assert not missing, (
            "Bug #1399 item 6: RESTART_REQUIRED_FIELDS is missing these "
            f"MODERATE-classified fields: {missing!r}"
        )


# ---------------------------------------------------------------------------
# Rendering tests (code-review remediation): list membership alone gives no
# operator-visible hint -- the template must ALSO carry the per-field Jinja
# conditional, mirroring the pre-existing 'log_level'/'host'/'port'/'workers'
# pattern (see test_1195_ac1_log_level_restart_badge.py). A representative
# sample spanning distinct config sections is exercised here (not all 44).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TEMPLATE_PATH = (
    _REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "partials"
    / "config_section.html"
)


def _extract_tr_for_label(html: str, label_text: str) -> str:
    idx = html.find(label_text)
    assert idx != -1, f"Label {label_text!r} not found in template"
    tr_start = html.rfind("<tr>", 0, idx)
    tr_end = html.find("</tr>", idx)
    assert tr_start != -1 and tr_end != -1
    return html[tr_start : tr_end + len("</tr>")]


def _template_html() -> str:
    return _TEMPLATE_PATH.read_text()


# (field, label, section_key, section_values) -- one representative field per
# distinct config section, spanning the newly-added MODERATE fields.
_RENDERING_SAMPLE = [
    ("min_length", "Minimum Length", "password_security", {"min_length": 12}),
    (
        "jwt_expiration_minutes",
        "JWT Expiration (minutes)",
        "server",
        {"jwt_expiration_minutes": 10},
    ),
    (
        "memory_warning_threshold_percent",
        "Memory Warning Threshold (%)",
        "health",
        {"memory_warning_threshold_percent": 80},
    ),
    (
        "collector_endpoint",
        "Collector Endpoint",
        "telemetry",
        {"collector_endpoint": "http://localhost:4317"},
    ),
    ("public_key", "Public Key", "langfuse", {"public_key": "pk-lf-x"}),
    (
        "credential_mode",
        "Credential Mode",
        "codex_integration",
        {"credential_mode": "api_key"},
    ),
    ("analysis_model", "Analysis Model", "golden_repos", {"analysis_model": "opus"}),
    (
        "multi_search_timeout_seconds",
        "Multi-Search Timeout (seconds)",
        "multi_search",
        {"multi_search_timeout_seconds": 30},
    ),
    (
        "payload_preview_size_chars",
        "Payload Preview Size (chars)",
        "cache",
        {"payload_preview_size_chars": 500},
    ),
]


class TestRestartRequiredFieldsRenderingSample:
    """Representative rendering proof across distinct config sections."""

    @pytest.mark.parametrize(
        "field,label,section_key,section_values", _RENDERING_SAMPLE
    )
    def test_field_row_renders_restart_hint(
        self, field, label, section_key, section_values
    ):
        from jinja2 import BaseLoader, Environment

        tr = _extract_tr_for_label(_template_html(), label)
        assert field in tr and "restart_required_fields" in tr, (
            f"{label!r} row must have a restart_required_fields conditional "
            f"referencing {field!r}"
        )
        assert "restart-note" in tr, (
            f"{label!r} row must contain a restart-note element"
        )

        config_ns = SimpleNamespace(**{section_key: SimpleNamespace(**section_values)})
        env = Environment(loader=BaseLoader())
        tmpl = env.from_string(tr)

        rendered_with = tmpl.render(restart_required_fields=[field], config=config_ns)
        assert "Requires server restart" in rendered_with, (
            f"restart note must appear when {field!r} is in restart_required_fields"
        )

        rendered_without = tmpl.render(restart_required_fields=[], config=config_ns)
        assert "Requires server restart" not in rendered_without, (
            f"restart note must NOT appear when {field!r} is not in restart_required_fields"
        )
