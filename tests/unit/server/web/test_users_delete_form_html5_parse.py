"""
Test that the delete form in users_list.html partial correctly nests the
csrf_token hidden input inside the <form> element after HTML5 parsing.

HTML5 in-table insertion mode pops bare <form> elements off the open elements
stack immediately, orphaning any children. This test catches that regression by
rendering the template and checking parsed DOM structure with BeautifulSoup
using the html5lib parser (which strictly follows HTML5 parsing rules).

NOTE: html.parser and lxml are lenient about bare forms in tables and do NOT
reproduce the browser bug. Only html5lib correctly models the orphaning.
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)

FAKE_USERS = [
    {
        "username": "alice",
        "email": "a@x.com",
        "role": "admin",
        "created_at": "2026-01-01 12:00",
        "mfa_enabled": False,
    },
    {
        "username": "bob",
        "email": None,
        "role": "normal_user",
        "created_at": "2026-01-02 13:00",
        "mfa_enabled": True,
    },
]

CSRF_TOKEN = "TEST_TOKEN_XYZ"
CURRENT_USERNAME = "admin"


def render_partial() -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("partials/users_list.html")
    return template.render(
        users=FAKE_USERS,
        current_username=CURRENT_USERNAME,
        csrf_token=CSRF_TOKEN,
    )


@pytest.mark.parametrize("username", ["alice", "bob"])
def test_delete_form_contains_csrf_token(username: str) -> None:
    """
    The <form id='delete-form-<username>'> must contain the csrf_token hidden
    input as a descendant after HTML5 parsing. Without the <tr><td> wrapper,
    HTML5 in-table insertion mode orphans the form and the input is NOT
    a descendant of the form element.
    """
    html = render_partial()
    # html5lib strictly follows HTML5 parsing rules including in-table
    # insertion mode, which orphans bare <form> elements inside <tbody>.
    # html.parser and lxml are lenient and do NOT reproduce the browser bug.
    soup = BeautifulSoup(html, "html5lib")

    form_id = f"delete-form-{username}"
    form = soup.find(id=form_id)
    assert form is not None, f"Form with id='{form_id}' not found in rendered HTML"
    assert form.name == "form", (
        f"Element with id='{form_id}' should be a <form>, got <{form.name}>"
    )

    csrf_input = form.find("input", {"name": "csrf_token"})
    assert csrf_input is not None, (
        f"<input name='csrf_token'> is NOT a descendant of form#'{form_id}' "
        f"after HTML5 parsing. This means the form is being orphaned by "
        f"HTML5 in-table insertion mode."
    )
    assert csrf_input.get("value") == CSRF_TOKEN, (
        f"csrf_token input value is '{csrf_input.get('value')}', "
        f"expected '{CSRF_TOKEN}'"
    )
