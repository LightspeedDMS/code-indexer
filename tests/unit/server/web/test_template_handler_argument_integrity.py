"""
Inline event-handler attributes (onclick/onchange) rendered by
`partials/users_list.html` and `partials/repos_list.html` must satisfy
one invariant, for any value the field can legitimately hold:

    The handler's argument equals the value the server rendered.

Both templates pass the value via a `data-*` attribute (HTML-attribute
decoding is a lossless round trip, regardless of content), read at call
time with `this.dataset.*`, rather than embedding it inside a nested
JavaScript string literal.

Foundation #1 compliant: real Jinja2 environment, real template source,
no mocks. Node.js execution proofs FAIL (not skip) when node is
unavailable -- node is present on every host that runs this suite.
"""

from _handler_argument_test_support import (
    ADVERSARIAL_VALUE,
    dataset_from_attrs,
    execute_handler_in_node,
    node_available,
    render,
    tags_with_handler_containing,
)


class TestUsersListHandlerArgumentIntegrity:
    """partials/users_list.html passes the username via data-username."""

    def _render_with_username(self, username):
        return render(
            "partials/users_list.html",
            {
                "users": [
                    {
                        "username": username,
                        "email": None,
                        "role": "normal_user",
                        "created_at": "2026-01-01",
                        "mfa_enabled": False,
                    }
                ],
                "current_username": "admin",
                "csrf_token": "tok",
            },
        )

    def test_edit_role_handler_argument_equals_rendered_value(self):
        html = self._render_with_username(ADVERSARIAL_VALUE)
        matches = tags_with_handler_containing(html, "toggleEditForm")
        assert matches, "expected a toggleEditForm(...) handler"
        attr_name, attrs = matches[0]
        assert attrs.get("data-username") == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in (attrs.get(attr_name) or "")

    def test_delete_handler_argument_equals_rendered_value(self):
        html = self._render_with_username(ADVERSARIAL_VALUE)
        matches = tags_with_handler_containing(html, "confirmDelete")
        assert matches, "expected a confirmDelete(...) handler"
        attr_name, attrs = matches[0]
        assert attrs.get("data-username") == ADVERSARIAL_VALUE
        assert ADVERSARIAL_VALUE not in (attrs.get(attr_name) or "")

    def test_ordinary_value_calls_same_function(self):
        html = self._render_with_username("alice")
        matches = tags_with_handler_containing(html, "toggleEditForm")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-username"] == "alice"
        assert "toggleEditForm(this.dataset.username)" in attrs[attr_name]

    def test_edit_role_handler_executes_with_exact_value_in_node(self):
        assert node_available(), "node.js is required to run this test"
        html = self._render_with_username(ADVERSARIAL_VALUE)
        matches = tags_with_handler_containing(html, "toggleEditForm")
        attr_name, attrs = matches[0]
        dataset = dataset_from_attrs(attrs)
        calls = execute_handler_in_node(attrs[attr_name], dataset)
        assert calls == [{"fn": "toggleEditForm", "args": [ADVERSARIAL_VALUE]}]


class TestReposListHandlerArgumentIntegrity:
    """partials/repos_list.html passes TWO values (username, user_alias)
    to several handlers via data-username / data-user-alias."""

    def _render_with_username(self, username):
        return render(
            "partials/repos_list.html",
            {
                "repos": [
                    {
                        "username": username,
                        "user_alias": "my-repo",
                        "golden_repo_alias": "golden",
                        "category_name": None,
                        "activated_at": "2026-01-01",
                        "status": "active",
                        "wiki_enabled": False,
                    }
                ],
                "csrf_token": "tok",
            },
        )

    def test_toggle_details_handler_argument_equals_rendered_value(self):
        html = self._render_with_username(ADVERSARIAL_VALUE)
        matches = tags_with_handler_containing(html, "toggleDetails")
        assert matches, "expected a toggleDetails(...) handler"
        attr_name, attrs = matches[0]
        assert attrs.get("data-username") == ADVERSARIAL_VALUE
        assert attrs.get("data-user-alias") == "my-repo"
        assert ADVERSARIAL_VALUE not in (attrs.get(attr_name) or "")

    def test_ordinary_values_call_same_function(self):
        html = self._render_with_username("alice")
        matches = tags_with_handler_containing(html, "toggleDetails")
        assert matches
        attr_name, attrs = matches[0]
        assert attrs["data-username"] == "alice"
        assert attrs["data-user-alias"] == "my-repo"
        assert (
            "toggleDetails(this.dataset.username, this.dataset.userAlias)"
            in attrs[attr_name]
        )

    def test_toggle_details_handler_executes_with_exact_value_in_node(self):
        assert node_available(), "node.js is required to run this test"
        html = self._render_with_username(ADVERSARIAL_VALUE)
        matches = tags_with_handler_containing(html, "toggleDetails")
        attr_name, attrs = matches[0]
        dataset = dataset_from_attrs(attrs)
        calls = execute_handler_in_node(attrs[attr_name], dataset)
        assert calls == [
            {"fn": "toggleDetails", "args": [ADVERSARIAL_VALUE, "my-repo"]}
        ]
