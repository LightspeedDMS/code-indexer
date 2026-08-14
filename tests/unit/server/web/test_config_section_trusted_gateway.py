"""EVO-66121: structural tests for the trusted_gateway config UI section.

MR !49/!51 review: the config screen must actually expose trusted_gateway (display +
edit form posting to /admin/config/trusted_gateway), mirroring the OIDC section -- not
only the config API.
"""

from pathlib import Path


def _read_template() -> str:
    return (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    ).read_text()


def _extract_section(html: str) -> str:
    start = html.find('id="section-trusted_gateway"')
    assert start != -1, "Missing trusted_gateway <details> section"
    section_start = html.rfind("<details", 0, start)
    end = html.find("</details>", start)
    assert section_start != -1 and end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_trusted_gateway_section():
    section = _extract_section(_read_template())
    assert "trusted gateway" in section.lower()
    # display + edit toggles are present (mirrors OIDC)
    assert 'id="display-trusted_gateway"' in section
    assert 'id="edit-form-trusted_gateway"' in section


def test_form_posts_to_admin_config_trusted_gateway():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/trusted_gateway"' in section
    assert 'name="csrf_token"' in section


def test_template_contains_all_editable_field_inputs():
    section = _extract_section(_read_template())
    for field_name in (
        "enabled",
        "issuer",
        "jwks_uri",
        "audience",
        "email_claim",
        "username_claim",
        "groups_claim",
        "group_mappings",
        "require_email_verification",
        "enable_jit_provisioning",
        "default_role",
        "leeway_seconds",
    ):
        assert f'name="{field_name}"' in section, f"Missing input for {field_name}"
