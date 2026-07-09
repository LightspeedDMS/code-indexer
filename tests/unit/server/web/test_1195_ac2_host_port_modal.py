"""Story #1195 AC2 (FIX 1): Client-side host/port confirmation modal.

The server-side guardrail in update_config_section rejects any changed host/port
without 'confirm_host_port_change'.  Without the client-side modal, an admin
who edits host or port has NO path to ever confirm — the UI is unconditionally
broken.

This file asserts (via real Jinja2 render of the server section) that:
  1. A modal/dialog element exists in the rendered server section.
  2. The 'confirm_host_port_change' field/mechanism is present.
  3. The warning text mentions the HAProxy/firewall port-lock concern (matching
     the server-side rejection message).

Rendering approach: extract only the "Server Settings" section from the template
source (lines up to and including </details> for section-server) and render it
with Jinja2 BaseLoader — mirrors the AC1 pattern to avoid providing a full
config context for 4282-line template.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def _server_section_html() -> str:
    """Return the raw template source for the Server Settings section plus its modal.

    Extracts from the <details id="section-server"> element through the
    host-port-confirm-modal <dialog> and its companion <script> block.
    The modal+script are placed immediately AFTER the closing </details>
    at the top level (matching the golden_repos.html pattern), so we
    capture both the <details> block and the subsequent dialog+script.
    """
    full_source = _TEMPLATE_PATH.read_text()
    # Find the server section start
    server_section_start = full_source.find(
        '<details open class="config-section" id="section-server">'
    )
    assert server_section_start != -1, "Could not find section-server in template"
    # Find the closing </details> for section-server
    details_close = full_source.find("</details>", server_section_start)
    assert details_close != -1, "Could not find closing </details> for section-server"
    end_pos = details_close + len("</details>")

    # If the host/port confirm modal dialog exists after the </details>, include it.
    # Look for the dialog and the closing </script> that follows it.
    dialog_marker = "host-port-confirm-modal"
    dialog_pos = full_source.find(dialog_marker, end_pos)
    if dialog_pos != -1:
        # Find the </script> that closes the companion JS block
        script_close = full_source.find("</script>", dialog_pos)
        if script_close != -1:
            end_pos = script_close + len("</script>")

    return full_source[server_section_start:end_pos]


def _render_server_section() -> str:
    """Render the Server Settings section snippet with minimal Jinja2 context."""
    from jinja2 import BaseLoader, Environment

    snippet = _server_section_html()
    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(snippet)

    config = SimpleNamespace(
        server=SimpleNamespace(
            host="0.0.0.0",
            port=8000,
            workers=1,
            log_level="INFO",
            jwt_expiration_minutes=60,
            service_display_name="Neo",
        )
    )
    return tmpl.render(
        config=config,
        csrf_token="test-csrf",
        restart_required_fields=[],
        validation_errors={},
    )


class TestHostPortModalPresence:
    """FIX 1: config_section.html must include a confirmation modal for host/port."""

    def test_server_section_contains_dialog_or_modal(self) -> None:
        """Rendered server section must contain a <dialog> element for host/port confirm."""
        html = _render_server_section()
        html_lower = html.lower()
        has_dialog = "<dialog" in html_lower
        assert has_dialog, (
            "FIX 1: config_section.html server section must contain a <dialog> element "
            "for the host/port change confirmation. None found in rendered HTML."
        )

    def test_confirm_host_port_change_field_present(self) -> None:
        """Rendered HTML must contain the 'confirm_host_port_change' field name."""
        html = _render_server_section()
        assert "confirm_host_port_change" in html, (
            "FIX 1: rendered config_section.html must contain 'confirm_host_port_change' "
            "— the hidden input that the server-side guardrail checks. "
            "Without it, every host/port change is unconditionally rejected."
        )

    def test_warning_mentions_haproxy_or_firewall(self) -> None:
        """Warning text must mention HAProxy or firewall — the port-lock concern."""
        html = _render_server_section()
        html_lower = html.lower()
        has_warning = "haproxy" in html_lower or "firewall" in html_lower
        assert has_warning, (
            "FIX 1: the host/port confirmation modal must mention 'HAProxy' or 'firewall' "
            "to communicate the port-lock safety concern. Neither found in rendered HTML."
        )

    def test_source_contains_confirm_host_port_change(self) -> None:
        """Source guard: template source must contain 'confirm_host_port_change'."""
        source = _TEMPLATE_PATH.read_text()
        assert "confirm_host_port_change" in source, (
            "FIX 1: config_section.html source must define a 'confirm_host_port_change' "
            "mechanism so the server-side guardrail can be satisfied."
        )

    def test_source_contains_dialog_element(self) -> None:
        """Source guard: template source must contain a <dialog> for host/port confirm."""
        source = _TEMPLATE_PATH.read_text()
        assert "<dialog" in source, (
            "FIX 1: config_section.html must contain a <dialog> element for the "
            "host/port confirmation modal."
        )
