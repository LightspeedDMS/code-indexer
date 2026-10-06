"""Stored secrets are write-only on the admin configuration page.

Invariant: the admin Config page (and the settings dict that feeds it) never
carries a stored secret value back to the browser. A secret field renders
empty, with a "set / not set" indicator computed server-side, and saving the
form with that field left blank keeps the stored secret.

Covered secrets: the OIDC client secret, the Langfuse tracing secret key and
each Langfuse pull project's secret key.

Rendering uses the established pattern (``routes._get_current_config()``
against a real ConfigService backed by a temp directory + a real Jinja render
of ``partials/config_section.html``).
"""

import json
import unittest.mock as mock

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.web import routes

# Obviously fake sample secrets: distinctive so a substring search is exact.
OIDC_SECRET = "example-oidc-client-secret-not-real-0001"
LANGFUSE_SECRET = "sk-lf-example-tracing-secret-not-real-0002"
PULL_SECRET_A = "sk-lf-example-pull-secret-a-not-real-0003"
PULL_SECRET_B = "sk-lf-example-pull-secret-b-not-real-0004"
PULL_PK_A = "pk-lf-example-project-a"
PULL_PK_B = "pk-lf-example-project-b"

ALL_SECRETS = (OIDC_SECRET, LANGFUSE_SECRET, PULL_SECRET_A, PULL_SECRET_B)


@pytest.fixture
def svc(tmp_path) -> ConfigService:
    """A real ConfigService in a temp dir holding every covered secret."""
    service = ConfigService(server_dir_path=str(tmp_path))
    service.load_config()
    service.update_setting("oidc", "client_secret", OIDC_SECRET)
    service.update_setting("langfuse", "public_key", "pk-lf-example-tracing")
    service.update_setting("langfuse", "secret_key", LANGFUSE_SECRET)
    service.update_setting(
        "langfuse",
        "pull_projects",
        json.dumps(
            [
                {"public_key": PULL_PK_A, "secret_key": PULL_SECRET_A},
                {"public_key": PULL_PK_B, "secret_key": PULL_SECRET_B},
            ]
        ),
    )
    return service


def _get_current_config_with(service: ConfigService) -> dict:
    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=service,
    ):
        return routes._get_current_config()


def _render_config_section(config: dict) -> str:
    template = routes.templates.env.get_template("partials/config_section.html")
    return template.render(
        request=None,
        csrf_token="test_csrf_token",
        config=config,
        validation_errors={},
        restart_required_fields=[],
        api_keys_status={},
        github_token_data=None,
        gitlab_token_data=None,
    )


def _pull_secret(service: ConfigService, public_key: str) -> str:
    config = service.get_config()
    assert config.langfuse_config is not None
    matches = [
        p.secret_key
        for p in config.langfuse_config.pull_projects
        if p.public_key == public_key
    ]
    assert len(matches) == 1, f"expected one project {public_key}, got {matches}"
    return matches[0]


class TestRenderedPageCarriesNoStoredSecret:
    def test_config_section_html_contains_no_stored_secret(self, svc) -> None:
        html = _render_config_section(_get_current_config_with(svc))
        for secret in ALL_SECRETS:
            assert secret not in html, "a stored secret was rendered into the page"

    def test_config_section_html_shows_secret_is_set_indicator(self, svc) -> None:
        html = _render_config_section(_get_current_config_with(svc))
        assert 'id="oidc-client-secret"' in html
        assert 'id="langfuse-secret-key"' in html
        # Pull project public keys still render so the merge-by-public-key
        # save can match each row to its stored secret.
        assert PULL_PK_A in html and PULL_PK_B in html
        assert "A secret is set" in html

    def test_get_all_settings_exposes_only_set_flags(self, svc) -> None:
        settings = svc.get_all_settings()
        serialized = json.dumps(settings, default=str)
        for secret in ALL_SECRETS:
            assert secret not in serialized
        assert "client_secret" not in settings["oidc"]
        assert settings["oidc"]["client_secret_set"] is True
        assert "secret_key" not in settings["langfuse"]
        assert settings["langfuse"]["secret_key_set"] is True
        assert settings["langfuse"]["pull_projects"] == [
            {"public_key": PULL_PK_A, "secret_key_set": True},
            {"public_key": PULL_PK_B, "secret_key_set": True},
        ]

    def test_get_all_settings_reports_unset_secrets_as_not_set(self, tmp_path) -> None:
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        settings = service.get_all_settings()
        assert settings["oidc"]["client_secret_set"] is False
        assert settings["langfuse"]["secret_key_set"] is False
        html = _render_config_section(_get_current_config_with(service))
        assert "No secret is set" in html


def _save(service: ConfigService, updates: list) -> None:
    """Apply a section save exactly as the Web front door does."""
    service.update_settings_audited(updates, actor="example-admin")


class TestSavingWithBlankSecretKeepsStoredSecret:
    def test_oidc_save_with_blank_secret_keeps_stored_secret(self, svc) -> None:
        _save(
            svc,
            [
                ("oidc", "client_id", "example-client-id"),
                ("oidc", "client_secret", ""),
            ],
        )
        oidc = svc.get_config().oidc_provider_config
        assert oidc is not None
        assert oidc.client_id == "example-client-id"
        assert oidc.client_secret == OIDC_SECRET

    def test_oidc_save_with_new_secret_replaces_it(self, svc) -> None:
        _save(svc, [("oidc", "client_secret", "example-rotated-oidc-secret")])
        oidc = svc.get_config().oidc_provider_config
        assert oidc is not None
        assert oidc.client_secret == "example-rotated-oidc-secret"

    def test_langfuse_save_with_blank_secret_keeps_stored_secret(self, svc) -> None:
        _save(
            svc,
            [
                ("langfuse", "public_key", "pk-lf-example-tracing"),
                ("langfuse", "secret_key", ""),
            ],
        )
        langfuse = svc.get_config().langfuse_config
        assert langfuse is not None
        assert langfuse.secret_key == LANGFUSE_SECRET

    def test_langfuse_save_with_new_secret_replaces_it(self, svc) -> None:
        _save(svc, [("langfuse", "secret_key", "sk-lf-example-rotated")])
        langfuse = svc.get_config().langfuse_config
        assert langfuse is not None
        assert langfuse.secret_key == "sk-lf-example-rotated"

    def test_pull_projects_blank_secrets_keep_each_stored_secret(self, svc) -> None:
        projects = [
            {"public_key": PULL_PK_A, "secret_key": ""},
            {"public_key": PULL_PK_B, "secret_key": ""},
        ]
        _save(svc, [("langfuse", "pull_projects", json.dumps(projects))])
        assert _pull_secret(svc, PULL_PK_A) == PULL_SECRET_A
        assert _pull_secret(svc, PULL_PK_B) == PULL_SECRET_B

    def test_pull_projects_new_secret_replaces_only_that_project(self, svc) -> None:
        projects = [
            {"public_key": PULL_PK_A, "secret_key": "sk-lf-example-rotated-a"},
            {"public_key": PULL_PK_B, "secret_key": ""},
        ]
        _save(svc, [("langfuse", "pull_projects", json.dumps(projects))])
        assert _pull_secret(svc, PULL_PK_A) == "sk-lf-example-rotated-a"
        assert _pull_secret(svc, PULL_PK_B) == PULL_SECRET_B

    def test_pull_projects_removed_project_is_removed(self, svc) -> None:
        projects = [{"public_key": PULL_PK_B, "secret_key": ""}]
        _save(svc, [("langfuse", "pull_projects", json.dumps(projects))])
        langfuse = svc.get_config().langfuse_config
        assert langfuse is not None
        assert [p.public_key for p in langfuse.pull_projects] == [PULL_PK_B]
        assert _pull_secret(svc, PULL_PK_B) == PULL_SECRET_B

    def test_pull_projects_new_project_takes_submitted_secret(self, svc) -> None:
        projects = [
            {"public_key": PULL_PK_A, "secret_key": ""},
            {"public_key": "pk-lf-example-new", "secret_key": "sk-lf-example-new"},
        ]
        _save(svc, [("langfuse", "pull_projects", json.dumps(projects))])
        assert _pull_secret(svc, PULL_PK_A) == PULL_SECRET_A
        assert _pull_secret(svc, "pk-lf-example-new") == "sk-lf-example-new"


def _stored_projects(service: ConfigService) -> list:
    langfuse = service.get_config().langfuse_config
    assert langfuse is not None
    return [(p.public_key, p.secret_key) for p in langfuse.pull_projects]


def _assert_save_refused(service: ConfigService, projects: list, match: str) -> None:
    from code_indexer.server.services import config_service as _cs

    before = _stored_projects(service)
    # The dedicated type lets the Web door catch ONLY this rejection.
    invalid = getattr(_cs, "LangfusePullProjectsInvalid", None)
    assert invalid is not None and issubclass(invalid, ValueError)
    with pytest.raises(invalid, match=match) as excinfo:
        _save(service, [("langfuse", "pull_projects", json.dumps(projects))])
    for secret in ALL_SECRETS:
        assert secret not in str(excinfo.value)
    assert _stored_projects(service) == before, "a refused save changed config"


class TestPullProjectSaveIsRefusedWhenAmbiguous:
    def test_new_project_without_secret_is_refused(self, svc) -> None:
        _assert_save_refused(
            svc,
            [
                {"public_key": PULL_PK_A, "secret_key": ""},
                {"public_key": "pk-lf-example-nosecret", "secret_key": ""},
            ],
            match="no secret key.*pk-lf-example-nosecret",
        )

    def test_renamed_public_key_with_blank_secret_is_refused(self, svc) -> None:
        _assert_save_refused(
            svc,
            [
                {"public_key": PULL_PK_A + "-renamed", "secret_key": ""},
                {"public_key": PULL_PK_B, "secret_key": ""},
            ],
            match="no secret key.*" + PULL_PK_A + "-renamed",
        )

    def test_duplicate_public_keys_are_refused(self, svc) -> None:
        _assert_save_refused(
            svc,
            [
                {"public_key": PULL_PK_A, "secret_key": "sk-lf-example-dup-1"},
                {"public_key": PULL_PK_A, "secret_key": "sk-lf-example-dup-2"},
            ],
            match="Duplicate.*" + PULL_PK_A,
        )
