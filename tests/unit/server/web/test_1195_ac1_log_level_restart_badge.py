"""Story #1195 AC1 / MINOR-7: log_level restart badge.

AC1-A: RESTART_REQUIRED_FIELDS must include 'log_level'.
MINOR-7: Template Log Level row must render the restart-note conditional,
         mirroring the existing 'workers' row pattern.

Tests are purely structural (source scan + Jinja2 render). No TestClient.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROUTES_PATH = _REPO_ROOT / "src" / "code_indexer" / "server" / "web" / "routes.py"
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


# ---------------------------------------------------------------------------
# AC1-A: module-level list
# ---------------------------------------------------------------------------


class TestAC1LogLevelRestartRequired:
    def test_log_level_in_restart_required_fields_import(self) -> None:
        """Imported RESTART_REQUIRED_FIELDS must contain 'log_level'."""
        from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS

        assert "log_level" in RESTART_REQUIRED_FIELDS, (
            "AC1: 'log_level' missing from RESTART_REQUIRED_FIELDS"
        )

    def test_log_level_in_source_definition(self) -> None:
        """Source guard: 'log_level' must appear inside the list literal."""
        source = _ROUTES_PATH.read_text()
        block_start = source.find("RESTART_REQUIRED_FIELDS")
        assert block_start != -1
        # Story #1400: widened from 1000 -> 1500 chars. The new
        # "temporal_lane_concurrency" entry (added directly above
        # "log_level") pushed log_level's offset past the old window.
        block = source[block_start : block_start + 1500]
        has_it = '"log_level"' in block or "'log_level'" in block
        assert has_it, (
            "AC1: 'log_level' must be present inside the RESTART_REQUIRED_FIELDS literal"
        )


# ---------------------------------------------------------------------------
# MINOR-7: template conditional
# ---------------------------------------------------------------------------


class TestAC1TemplateRestartNote:
    def _template_html(self) -> str:
        return _TEMPLATE_PATH.read_text()

    def test_log_level_row_has_restart_required_conditional(self) -> None:
        """Log Level <tr> must reference both log_level and restart_required_fields."""
        tr = _extract_tr_for_label(self._template_html(), "Log Level")
        assert "log_level" in tr and "restart_required_fields" in tr, (
            "MINOR-7: Log Level row must have restart_required_fields conditional"
        )

    def test_log_level_row_has_restart_note_class(self) -> None:
        """Log Level <tr> must contain an element with class 'restart-note'."""
        tr = _extract_tr_for_label(self._template_html(), "Log Level")
        assert "restart-note" in tr, (
            "MINOR-7: Log Level row must have a restart-note element"
        )

    def test_log_level_row_no_longer_empty_note_td(self) -> None:
        """Empty <td class='config-note'></td> must be replaced."""
        tr = _extract_tr_for_label(self._template_html(), "Log Level")
        assert '<td class="config-note"></td>' not in tr, (
            "MINOR-7: Log Level row still has an empty config-note td"
        )

    def test_jinja2_renders_restart_note_when_in_fields(self) -> None:
        """Jinja2 rendering: restart note appears when log_level is in fields."""
        from jinja2 import BaseLoader, Environment

        tr_html = _extract_tr_for_label(self._template_html(), "Log Level")
        env = Environment(loader=BaseLoader())
        tmpl = env.from_string(tr_html)
        ctx = SimpleNamespace(server=SimpleNamespace(log_level="INFO"))

        rendered_with = tmpl.render(restart_required_fields=["log_level"], config=ctx)
        assert "Requires server restart" in rendered_with, (
            "MINOR-7: restart note must appear when log_level in restart_required_fields"
        )

        rendered_without = tmpl.render(restart_required_fields=[], config=ctx)
        assert "Requires server restart" not in rendered_without, (
            "MINOR-7: restart note must NOT appear when log_level not in restart_required_fields"
        )
