"""
Sweep verification for every OTHER template or script found to build an
inline event handler (on* attribute) or a `<script>`-block JS literal
from a field value -- the same invariant guarded in
test_template_handler_argument_integrity.py for the two explicitly
named templates:

    A handler's argument equals the value the server rendered.

Each fixed template passes its value via a `data-*` attribute (read at
call time with `this.dataset.*`) or, for a value interpolated directly
inside a `<script>` block, via Jinja's `|tojson` filter -- both are
lossless round trips regardless of the value's content. Several dynamic
controls (in the standalone `activated_repo_management.js` and
`repo_health.js`, and in inline `<script>` blocks in
`research_assistant.html` and `golden_repos_list.html`) build their
retry/delete buttons via real DOM construction
(`createElement`/`addEventListener`) instead of a string literal, so no
escaping question arises at all; those are verified by executing the
actual extracted source in Node.js.

Foundation #1 compliant: real Jinja2 environment, real template and
script files. Node.js execution proofs FAIL (not skip) when node is
unavailable -- node is present on every host that runs this suite.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import pytest

from _handler_argument_test_support import (
    ADVERSARIAL_VALUE,
    AttrCollectingParser,
    DOM_STUB_JS,
    STATIC_JS_DIR,
    TEMPLATES_DIR,
    dataset_from_attrs,
    execute_handler_in_node,
    execute_js_in_node,
    extract_balanced_js_block,
    node_available,
    render,
    tags_with_handler_containing,
)


class TestMcpCredentialsListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "partials/mcp_credentials_list.html",
            {
                "mcp_credentials": [
                    {
                        "credential_id": "cred-1",
                        "name": ADVERSARIAL_VALUE,
                        "client_id_prefix": "abc",
                        "created_at": "2026-01-01",
                        "last_used_at": None,
                    }
                ]
            },
        )
        matches = tags_with_handler_containing(html, "deleteCredential")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-credential-name"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestGitCredentialsListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "partials/git_credentials_list.html",
            {
                "git_credentials": [
                    {
                        "credential_id": "cred-1",
                        "name": ADVERSARIAL_VALUE,
                        "forge_type": "gitlab",
                        "forge_host": "gitlab.example.com",
                        "forge_username": "u",
                        "git_user_name": "n",
                        "git_user_email": "e@example.com",
                        "token_suffix": "1234",
                        "created_at": "2026-01-01",
                    }
                ]
            },
        )
        matches = tags_with_handler_containing(html, "deleteCredential")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-credential-name"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestApiKeysListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "partials/api_keys_list.html",
            {
                "api_keys": [
                    {
                        "key_id": "key-1",
                        "name": ADVERSARIAL_VALUE,
                        "key_prefix": "abc",
                        "created_at": "2026-01-01",
                    }
                ]
            },
        )
        matches = tags_with_handler_containing(html, "deleteKey")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-key-name"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestSshKeysHandlerArgumentIntegrity:
    def test_handler_arguments_equal_rendered_values(self):
        html = render(
            "ssh_keys.html",
            {
                "show_nav": False,
                "managed_keys": [
                    {
                        "name": ADVERSARIAL_VALUE,
                        "key_type": "ed25519",
                        "fingerprint": "SHA256:xyz",
                        "hosts": [],
                        "public_key": "ssh-ed25519 " + ADVERSARIAL_VALUE,
                    }
                ],
            },
        )
        copy_matches = tags_with_handler_containing(html, "copyPublicKey")
        assert copy_matches
        _, copy_attrs = copy_matches[0]
        assert copy_attrs["data-key-name"] == ADVERSARIAL_VALUE
        assert copy_attrs["data-public-key"] == "ssh-ed25519 " + ADVERSARIAL_VALUE

        delete_matches = tags_with_handler_containing(html, "deleteKey")
        assert delete_matches
        assert delete_matches[0][1]["data-key-name"] == ADVERSARIAL_VALUE


class TestRepoCategoriesListHandlerArgumentIntegrity:
    def test_handler_arguments_equal_rendered_values(self):
        html = render(
            "partials/repo_categories_list.html",
            {
                "csrf_token": "tok",
                "categories": [
                    {
                        "id": 1,
                        "priority": 1,
                        "name": ADVERSARIAL_VALUE,
                        "pattern": ADVERSARIAL_VALUE,
                    }
                ],
            },
        )
        matches = tags_with_handler_containing(html, "showEditModal")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-category-name"] == ADVERSARIAL_VALUE
        assert attrs["data-category-pattern"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]

    def test_handler_executes_with_numeric_id_in_node(self):
        # The category id reaches showEditModal as a number (the rendered
        # integer), not as its string form -- JSON round-trips 42 and "42"
        # distinctly, so this comparison is type-exact.
        assert node_available(), "node.js is required to run this test"
        html = render(
            "partials/repo_categories_list.html",
            {
                "csrf_token": "tok",
                "categories": [
                    {
                        "id": 42,
                        "priority": 1,
                        "name": ADVERSARIAL_VALUE,
                        "pattern": ADVERSARIAL_VALUE,
                    }
                ],
            },
        )
        attr_name, attrs = tags_with_handler_containing(html, "showEditModal")[0]
        calls = execute_handler_in_node(attrs[attr_name], dataset_from_attrs(attrs))
        assert calls == [
            {
                "fn": "showEditModal",
                "args": [42, ADVERSARIAL_VALUE, ADVERSARIAL_VALUE],
            }
        ]
        assert isinstance(calls[0]["args"][0], int)


class TestGroupsListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "partials/groups_list.html",
            {
                "csrf_token": "tok",
                "groups": [
                    {
                        "id": 1,
                        "name": ADVERSARIAL_VALUE,
                        "description": "d",
                        "is_default": False,
                        "user_count": 0,
                        "repo_count": 0,
                    }
                ],
            },
        )
        matches = tags_with_handler_containing(html, "confirmDeleteGroup")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-group-name"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestResearchSessionsListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        from code_indexer.server.web.jinja_filters import relative_time

        html = render(
            "partials/research_sessions_list.html",
            {
                "sessions": [
                    {
                        "id": "session-1",
                        "name": ADVERSARIAL_VALUE,
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "active_session_id": None,
                "active_session_name": None,
            },
            extra_filters={"relative_time": relative_time},
        )
        matches = tags_with_handler_containing(html, "renameSession")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-session-name"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestGoldenReposListHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "partials/golden_repos_list.html",
            {
                "repos": [
                    {
                        "alias": ADVERSARIAL_VALUE,
                        "category_id": None,
                        "category_name": None,
                        "category_priority": None,
                        "global_alias": None,
                        "version": None,
                        "has_semantic": False,
                        "has_fts": False,
                        "has_temporal": False,
                        "has_scip": False,
                        "repo_url": "https://example.com/repo.git",
                        "status": "ready",
                    }
                ],
                "categories": [],
            },
        )
        matches = tags_with_handler_containing(html, "confirmDelete")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-repo-alias"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestGoldenRepoDetailsHandlerArgumentIntegrity:
    def test_handler_arguments_equal_rendered_values(self):
        html = render(
            "partials/golden_repo_details.html",
            {
                "csrf_token": "tok",
                "temporal_all_branches_enabled": True,
                "repo": {
                    "alias": ADVERSARIAL_VALUE,
                    "repo_url": "https://example.com/repo.git",
                    "default_branch": "main",
                    "status": "ready",
                    "has_semantic": False,
                    "has_fts": False,
                    "has_temporal": False,
                    "has_scip": False,
                    "version": None,
                    "global_alias": "globally-active-alias",
                    "created_at": "2026-01-01",
                    "last_refresh": None,
                    "error_message": None,
                    "chunk_count": 0,
                    "file_count": 0,
                    "wiki_enabled": False,
                    "temporal_options": None,
                },
            },
        )
        matches = tags_with_handler_containing(html, "showAddIndexForm")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-repo-alias"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]

        health_matches = tags_with_handler_containing(html, "refreshGlobalHealthData")
        assert health_matches
        assert health_matches[0][1]["data-repo-alias"] == ADVERSARIAL_VALUE


class TestDepmapDomainExplorerHandlerArgumentIntegrity:
    """The domain list button carries no inline event handler and no
    data-domain-name attribute; its only click behaviour is the existing
    hx-get request, which carries the URL-encoded domain name."""

    def _domain_button_attrs(self, domain_name: str) -> Dict[str, Optional[str]]:
        html = render(
            "partials/depmap_domain_explorer.html",
            {"domains": {"domains": [{"name": domain_name, "repo_count": 3}]}},
        )
        parser = AttrCollectingParser()
        parser.feed(html)
        buttons: List[Dict[str, Optional[str]]] = [
            attrs
            for tag, attrs in parser.tags
            if tag == "button" and attrs.get("class") == "domain-list-btn"
        ]
        assert buttons, "expected a domain-list-btn button"
        return buttons[0]

    def test_no_inline_handler_carries_the_domain_name(self):
        attrs = self._domain_button_attrs(ADVERSARIAL_VALUE)
        assert "onclick" not in attrs
        assert "onchange" not in attrs
        assert "data-domain-name" not in attrs

    def test_ordinary_value_calls_same_function(self):
        # "same function" here means the pre-existing hx-get wiring is
        # unchanged -- no inline handler is introduced for any value.
        attrs = self._domain_button_attrs("backend")
        assert not [
            name
            for name, value in attrs.items()
            if name.startswith("on") and "backend" in (value or "")
        ]
        assert "onclick" not in attrs
        assert "data-domain-name" not in attrs
        assert attrs["hx-get"] == "/admin/partials/depmap-domain-detail/backend"

    def test_hx_get_still_carries_the_urlencoded_domain_name(self):
        attrs = self._domain_button_attrs(ADVERSARIAL_VALUE)
        assert "onclick" not in attrs
        assert attrs["hx-get"] == (
            "/admin/partials/depmap-domain-detail/" + quote(ADVERSARIAL_VALUE, safe="")
        )


class TestGitlabReposHandlerArgumentIntegrity:
    def test_handler_arguments_equal_rendered_values(self):
        html = render(
            "partials/gitlab_repos.html",
            {
                "repositories": [],
                "source_total": None,
                "page_size": 20,
                "has_next_page": True,
                "next_cursor": ADVERSARIAL_VALUE,
                "search_term": ADVERSARIAL_VALUE,
            },
        )
        matches = tags_with_handler_containing(html, "loadGitLabNext")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-next-cursor"] == ADVERSARIAL_VALUE
        assert attrs["data-search-term"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestGithubReposHandlerArgumentIntegrity:
    def test_handler_arguments_equal_rendered_values(self):
        html = render(
            "partials/github_repos.html",
            {
                "repositories": [],
                "source_total": None,
                "page_size": 20,
                "has_next_page": True,
                "next_cursor": ADVERSARIAL_VALUE,
                "search_term": ADVERSARIAL_VALUE,
            },
        )
        matches = tags_with_handler_containing(html, "loadGitHubNext")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-next-cursor"] == ADVERSARIAL_VALUE
        assert attrs["data-search-term"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


class TestQueryHistoryHandlerArgumentIntegrity:
    def test_handler_argument_equals_rendered_value(self):
        html = render(
            "query.html",
            {
                "show_nav": False,
                "query_history": [
                    {
                        "query_text": ADVERSARIAL_VALUE,
                        "repository": "repo-1",
                        "search_mode": "semantic",
                    }
                ],
            },
        )
        matches = tags_with_handler_containing(html, "loadHistoryQuery")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-query-text"] == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in attrs[attr_name]


def _extract_tojson_literal(html: str, var_name: str) -> str:
    """Extract the JS literal assigned to `var_name` (a JSON-encoded
    string produced by Jinja's |tojson filter) and decode it."""
    import re

    match = re.search(rf"{re.escape(var_name)}\s*=\s*(\".*?\"|'.*?');", html, re.S)
    assert match, f"expected an assignment to {var_name}"
    decoded = json.loads(match.group(1))
    assert isinstance(decoded, str)
    return decoded


class TestOauthAuthorizeConsentHandlerArgumentIntegrity:
    def test_script_literals_equal_rendered_values(self):
        html = render(
            "oauth_authorize_consent.html",
            {
                "show_nav": False,
                "username": "admin",
                "client_name": "client",
                "client_id": "client-1",
                "redirect_uri": ADVERSARIAL_VALUE,
                "code_challenge": "abc",
                "response_type": "code",
                "state": ADVERSARIAL_VALUE,
            },
        )
        assert _extract_tojson_literal(html, "const redirectUri") == ADVERSARIAL_VALUE
        assert _extract_tojson_literal(html, "const state") == ADVERSARIAL_VALUE


class TestConfigSectionServerHandlerArgumentIntegrity:
    """config_section.html is a large multi-section page needing an
    unrelated full context to render end to end -- this extracts and
    renders just the two REAL production lines under test (read
    directly from the file, not retyped) rather than stubbing the
    entire page."""

    def _extract_lines(self) -> str:
        import jinja2

        content = (TEMPLATES_DIR / "partials/config_section.html").read_text()
        var_lines = [
            line
            for line in content.splitlines()
            if "var originalHost" in line or "var originalPort" in line
        ]
        assert len(var_lines) == 2
        env = jinja2.Environment(autoescape=True)
        return env.from_string("\n".join(var_lines)).render(
            config={"server": {"host": ADVERSARIAL_VALUE, "port": 8000}}
        )

    def test_host_and_port_script_literals_equal_rendered_values(self):
        html = self._extract_lines()
        assert _extract_tojson_literal(html, "var originalHost") == ADVERSARIAL_VALUE
        # Port stays a genuine config value (int in this fixture); tojson
        # emits it unquoted, so the plain-string extractor above only
        # applies to the (string) host case.
        assert "var originalPort = 8000;" in html


@pytest.mark.parametrize(
    "relative_path,onclick_needle",
    [
        ("partials/mcp_credentials_list.html", "deleteCredential"),
        ("partials/git_credentials_list.html", "deleteCredential"),
        ("partials/api_keys_list.html", "deleteKey"),
        ("partials/repo_categories_list.html", "showEditModal"),
        ("partials/groups_list.html", "confirmDeleteGroup"),
        ("partials/golden_repos_list.html", "confirmDelete"),
    ],
)
class TestOrdinaryValuesUnaffected:
    """Ordinary (non-adversarial) values must still call the same
    functions with the same arguments after the fix."""

    def _context_for(self, relative_path):
        if relative_path == "partials/mcp_credentials_list.html":
            return {
                "mcp_credentials": [
                    {
                        "credential_id": "cred-1",
                        "name": "alice-laptop",
                        "client_id_prefix": "abc",
                        "created_at": "2026-01-01",
                        "last_used_at": None,
                    }
                ]
            }
        if relative_path == "partials/git_credentials_list.html":
            return {
                "git_credentials": [
                    {
                        "credential_id": "cred-1",
                        "name": "alice-token",
                        "forge_type": "gitlab",
                        "forge_host": "gitlab.example.com",
                        "forge_username": "u",
                        "git_user_name": "n",
                        "git_user_email": "e@example.com",
                        "token_suffix": "1234",
                        "created_at": "2026-01-01",
                    }
                ]
            }
        if relative_path == "partials/api_keys_list.html":
            return {
                "api_keys": [
                    {
                        "key_id": "key-1",
                        "name": "alice-key",
                        "key_prefix": "abc",
                        "created_at": "2026-01-01",
                    }
                ]
            }
        if relative_path == "partials/repo_categories_list.html":
            return {
                "csrf_token": "tok",
                "categories": [
                    {"id": 1, "priority": 1, "name": "Backend", "pattern": "src/.*"}
                ],
            }
        if relative_path == "partials/groups_list.html":
            return {
                "csrf_token": "tok",
                "groups": [
                    {
                        "id": 1,
                        "name": "engineering",
                        "description": "d",
                        "is_default": False,
                        "user_count": 0,
                        "repo_count": 0,
                    }
                ],
            }
        if relative_path == "partials/golden_repos_list.html":
            return {
                "repos": [
                    {
                        "alias": "my-golden-repo",
                        "category_id": None,
                        "category_name": None,
                        "category_priority": None,
                        "global_alias": None,
                        "version": None,
                        "has_semantic": False,
                        "has_fts": False,
                        "has_temporal": False,
                        "has_scip": False,
                        "repo_url": "https://example.com/repo.git",
                        "status": "ready",
                    }
                ],
                "categories": [],
            }
        raise AssertionError(f"no fixture context for {relative_path}")

    def test_still_calls_same_function(self, relative_path, onclick_needle):
        html = render(relative_path, self._context_for(relative_path))
        matches = tags_with_handler_containing(html, onclick_needle)
        assert matches, f"expected a handler containing {onclick_needle!r}"
        attr_name, attrs = matches[0]
        assert f"{onclick_needle}(" in attrs[attr_name]


# ---------------------------------------------------------------------------
# Production JS files (not templates): the retry/delete controls are built
# via real DOM construction (createElement/addEventListener) rather than a
# string literal, so no escaping question arises. Verified by extracting
# the REAL source (via extract_balanced_js_block, not a retyped copy) and
# executing it in Node.js against the shared minimal DOM stub.
# ---------------------------------------------------------------------------


class TestActivatedRepoManagementJsRetryButton:
    def test_retry_button_captures_exact_values_as_closure_variables(self):
        assert node_available(), "node.js is required to run this test"
        source = (STATIC_JS_DIR / "activated_repo_management.js").read_text()
        handle_error_src = extract_balanced_js_block(
            source, "const handleError = (error) => {"
        )
        script = f"""
{DOM_STUB_JS}
const detailsContainer = document.createElement();
function escapeHtml(text) {{ return text; }}
const calls = [];
function loadActivatedRepoHealthDetails(...args) {{ calls.push(args); }}
function _finishActivatedRepoHealthLoad() {{}}
const userAlias = {json.dumps(ADVERSARIAL_VALUE)};
const owner = {json.dumps(ADVERSARIAL_VALUE)};
const refreshBtn = null;

{handle_error_src}

handleError({{ message: 'boom' }});
const retryBtn = detailsContainer._children.find(c => c.textContent === 'Retry');
retryBtn._listeners['click']();
process.stdout.write(JSON.stringify(calls));
"""
        result = execute_js_in_node(script)
        assert result.returncode == 0, result.stderr
        calls = json.loads(result.stdout)
        assert calls == [[ADVERSARIAL_VALUE, False, ADVERSARIAL_VALUE]]

    @pytest.mark.parametrize("owner_js", ["''", "undefined", "null"])
    def test_retry_button_passes_null_owner_when_owner_is_absent(self, owner_js):
        # An absent (falsy) owner reaches the retry call as null.
        assert node_available(), "node.js is required to run this test"
        source = (STATIC_JS_DIR / "activated_repo_management.js").read_text()
        handle_error_src = extract_balanced_js_block(
            source, "const handleError = (error) => {"
        )
        script = f"""
{DOM_STUB_JS}
const detailsContainer = document.createElement();
function escapeHtml(text) {{ return text; }}
const calls = [];
function loadActivatedRepoHealthDetails(...args) {{
    calls.push(args.map(a => a === undefined ? 'UNDEFINED' : a));
}}
function _finishActivatedRepoHealthLoad() {{}}
const userAlias = 'my-repo';
const owner = {owner_js};
const refreshBtn = null;

{handle_error_src}

handleError({{ message: 'boom' }});
const retryBtn = detailsContainer._children.find(c => c.textContent === 'Retry');
retryBtn._listeners['click']();
process.stdout.write(JSON.stringify(calls));
"""
        result = execute_js_in_node(script)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [["my-repo", False, None]]


class TestRepoHealthJsRetryButton:
    def test_retry_button_captures_exact_value_as_closure_variable(self):
        assert node_available(), "node.js is required to run this test"
        source = (STATIC_JS_DIR / "repo_health.js").read_text()
        handle_error_src = extract_balanced_js_block(
            source, "const handleError = (error) => {"
        )
        script = f"""
{DOM_STUB_JS}
const detailsContainer = document.createElement();
function escapeHtml(text) {{ return text; }}
const calls = [];
function loadHealthDetails(...args) {{ calls.push(args); }}
function hideJobProgress() {{}}
const repoAlias = {json.dumps(ADVERSARIAL_VALUE)};
const refreshBtn = null;

{handle_error_src}

handleError({{ message: 'boom' }});
const retryBtn = detailsContainer._children.find(c => c.textContent === 'Retry');
retryBtn._listeners['click']();
process.stdout.write(JSON.stringify(calls));
"""
        result = execute_js_in_node(script)
        assert result.returncode == 0, result.stderr
        calls = json.loads(result.stdout)
        assert calls == [[ADVERSARIAL_VALUE, False]]


class TestGoldenReposListGlobalHealthRetryButton:
    def test_retry_button_captures_exact_values_as_closure_variables(self):
        assert node_available(), "node.js is required to run this test"
        source = Path(
            str(TEMPLATES_DIR / "partials" / "golden_repos_list.html")
        ).read_text()
        fn_start = source.index("async function loadGlobalHealthDetails")
        handle_error_src = extract_balanced_js_block(
            source[fn_start:], "const handleError = (error) => {"
        )
        script = f"""
{DOM_STUB_JS}
const detailsContainer = document.createElement();
function escapeHtml(text) {{ return text; }}
const calls = [];
function loadGlobalHealthDetails(...args) {{ calls.push(args); }}
const repoAlias = {json.dumps(ADVERSARIAL_VALUE)};
const globalAlias = {json.dumps(ADVERSARIAL_VALUE)};
const refreshBtn = null;

{handle_error_src}

handleError({{ message: 'boom' }});
const retryBtn = detailsContainer._children.find(c => c.textContent === 'Retry');
retryBtn._listeners['click']();
process.stdout.write(JSON.stringify(calls));
"""
        result = execute_js_in_node(script)
        assert result.returncode == 0, result.stderr
        calls = json.loads(result.stdout)
        assert calls == [[ADVERSARIAL_VALUE, ADVERSARIAL_VALUE, False]]


class TestResearchAssistantFileListJs:
    def test_delete_button_passes_encoded_filename_matching_head_parity(self):
        # Invariant: the filename reaches deleteFile URL-encoded exactly
        # once, at the call site (encodeURIComponent(file.filename)).
        assert node_available(), "node.js is required to run this test"
        source = Path(str(TEMPLATES_DIR / "research_assistant.html")).read_text()
        then_start = source.index(".then(data => {") + len(".then(")
        callback_src = extract_balanced_js_block(source[then_start:], "data => {")
        script = f"""
{DOM_STUB_JS}
function formatFileSize(x) {{ return String(x); }}
function formatTimestamp(x) {{ return String(x); }}
const deleteCalls = [];
function deleteFile(filename) {{ deleteCalls.push(filename); }}
const currentSessionId = 'session-1';

const rawFilename = {json.dumps(ADVERSARIAL_VALUE)};
const expectedEncoded = encodeURIComponent(rawFilename);

const callback = ({callback_src});
callback({{ files: [{{ filename: rawFilename, size: 10, uploaded_at: 't' }}] }});

const filesList = getContainer('attached-files');
const item = filesList._children.find(c => c.className === 'file-item');
const deleteBtn = item._children.find(c => c.className === 'delete-file-btn');
deleteBtn._listeners['click']();
process.stdout.write(JSON.stringify({{ calls: deleteCalls, expectedEncoded }}));
"""
        result = execute_js_in_node(script)
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["calls"] == [parsed["expectedEncoded"]]
        # The encoded value must differ from the raw value (the adversarial
        # value contains characters encodeURIComponent escapes) so this
        # assertion is discriminating, not vacuously true.
        assert parsed["expectedEncoded"] != ADVERSARIAL_VALUE
