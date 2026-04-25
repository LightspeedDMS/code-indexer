"""
Unit tests for Story #844: Codex CLI Integration section in config_section.html.

Structural content tests of the HTML/JS template source, following the same
pattern as test_auto_discovery_remove_repo_868.py.

5 structural ACs verified (full test file plus helpers):
  AC1: Template contains a <details element with id containing 'section-codex',
       isolated as a bounded block
  AC2: Credential-mode <select name="credential_mode"> contains all 3 option values
  AC3: <input name="api_key"> has type="password"
  AC4: <input name="codex_weight"> has min="0", max="1", and step= on the same element
  AC5: The onchange attribute on the enabled input names a function;
       that named function body (supporting classic and assignment forms) contains
       a disabled mutation
"""

from pathlib import Path


def _read_template() -> str:
    """Read config_section.html template content."""
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    )
    return template_path.read_text()


def _read_codex_section() -> str:
    """Return the isolated codex <details>...</details> block.

    Extracts only the matching <details element whose opening tag contains
    'section-codex', bounded by tag-depth counting on <details>...</details>
    pairs. Raises AssertionError if not found.
    """
    html = _read_template()
    # Find the opening <details tag whose attributes contain 'section-codex'
    pos = html.find("<details")
    section_start = -1
    while pos != -1:
        tag_end = html.find(">", pos)
        if tag_end == -1:
            break
        opening_tag = html[pos : tag_end + 1]
        if "section-codex" in opening_tag:
            section_start = pos
            break
        pos = html.find("<details", pos + 1)
    assert section_start != -1, (
        "No <details element with 'section-codex' in its opening tag found in config_section.html"
    )
    # Walk forward counting <details>...</details> depth to find the closing </details>
    depth = 0
    i = section_start
    while i < len(html):
        if html[i : i + 8] == "<details":
            depth += 1
            i += 8
        elif html[i : i + 10] == "</details>":
            depth -= 1
            if depth == 0:
                return html[section_start : i + 10]
            i += 10
        else:
            i += 1
    raise AssertionError("Unclosed <details section-codex block in config_section.html")


def _extract_select_block(html: str, select_name: str) -> str:
    """Return the text of the <select element whose name matches select_name."""
    search = f'name="{select_name}"'
    pos = html.find(search)
    if pos == -1:
        search = f"name='{select_name}'"
        pos = html.find(search)
    assert pos != -1, f"No <select with name='{select_name}' found"
    select_start = html.rfind("<select", 0, pos)
    assert select_start != -1, f"No opening <select before name='{select_name}'"
    select_end = html.find("</select>", select_start)
    assert select_end != -1, f"No closing </select> after select name='{select_name}'"
    return html[select_start : select_end + len("</select>")]


def _extract_input_element(html: str, input_name: str) -> str:
    """Return the text of the <input element whose name matches input_name."""
    search = f'name="{input_name}"'
    pos = html.find(search)
    if pos == -1:
        search = f"name='{input_name}'"
        pos = html.find(search)
    assert pos != -1, f"No <input with name='{input_name}' found"
    input_start = html.rfind("<input", 0, pos)
    assert input_start != -1, f"No opening <input before name='{input_name}'"
    input_end = html.find(">", input_start)
    assert input_end != -1, f"No closing > after <input name='{input_name}'"
    return html[input_start : input_end + 1]


def _extract_named_function_body(script_block: str, function_name: str) -> str:
    """Return the JS function body for the named function in script_block.

    Supports three common declaration styles:
      1. Classic:    function name(...) { ... }
      2. Assignment: name = function(...) { ... }   (also: name = (...) => { ... })
      3. Const/let:  const name = ... { ... }

    Locates the opening '{' of the function body and walks brace depth to find
    the matching '}'. Returns empty string if the function is not found.
    """
    # Try classic: 'function name(' or 'name =' (for assignments)
    candidates = [
        f"function {function_name}(",
        f"{function_name} =",
        f"{function_name}=",
    ]
    func_start = -1
    for candidate in candidates:
        idx = script_block.find(candidate)
        if idx != -1:
            func_start = idx
            break
    if func_start == -1:
        return ""
    # Find the first '{' from func_start — that is the function body opening
    brace_start = script_block.find("{", func_start)
    if brace_start == -1:
        return ""
    depth = 0
    i = brace_start
    while i < len(script_block):
        ch = script_block[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script_block[func_start : i + 1]
        i += 1
    return script_block[func_start:]


def _extract_onchange_function_name(element: str) -> str:
    """Extract the bare function name from an onchange='funcName()' attribute.

    Returns empty string if onchange is not present or has an unexpected format.
    """
    for attr in ('onchange="', "onchange='"):
        pos = element.find(attr)
        if pos != -1:
            start = pos + len(attr)
            paren = element.find("(", start)
            if paren != -1:
                return element[start:paren].strip()
    return ""


# ---------------------------------------------------------------------------
# AC1: <details element with codex section id (isolated block)
# ---------------------------------------------------------------------------


def test_codex_section_details_element_exists():
    """AC1: Template must contain a <details element with id containing 'section-codex',
    and it must form a well-bounded block."""
    codex_html = _read_codex_section()
    assert len(codex_html) > 0


# ---------------------------------------------------------------------------
# AC2: Credential-mode <select> contains all 3 option values
# ---------------------------------------------------------------------------


def test_credential_mode_select_contains_all_three_options():
    """AC2: The <select name='credential_mode'> must include option values
    'none', 'api_key', and 'subscription' within the same select block."""
    codex_html = _read_codex_section()
    select_block = _extract_select_block(codex_html, "credential_mode")
    assert 'value="none"' in select_block or "value='none'" in select_block, (
        "Select block is missing option value='none'"
    )
    assert 'value="api_key"' in select_block or "value='api_key'" in select_block, (
        "Select block is missing option value='api_key'"
    )
    assert (
        'value="subscription"' in select_block or "value='subscription'" in select_block
    ), "Select block is missing option value='subscription'"


# ---------------------------------------------------------------------------
# AC3: Password-type input with name="codex_api_key"
# ---------------------------------------------------------------------------


def test_codex_api_key_input_is_password_type():
    """AC3: The <input name='api_key'> must have type='password'."""
    codex_html = _read_codex_section()
    element = _extract_input_element(codex_html, "api_key")
    assert 'type="password"' in element or "type='password'" in element, (
        f"Input name='api_key' is not type='password'. Element: {element!r}"
    )


# ---------------------------------------------------------------------------
# AC4: Weight input has min, max, and step all on the same element
# ---------------------------------------------------------------------------


def test_weight_input_has_min_max_step_on_same_element():
    """AC4: The <input name='codex_weight'> must have min='0', max='1', and step=
    all present on the same element."""
    codex_html = _read_codex_section()
    element = _extract_input_element(codex_html, "codex_weight")
    assert 'min="0"' in element or "min='0'" in element, (
        f"Weight input is missing min='0'. Element: {element!r}"
    )
    assert 'max="1"' in element or "max='1'" in element, (
        f"Weight input is missing max='1'. Element: {element!r}"
    )
    assert "step=" in element, (
        f"Weight input is missing step= attribute. Element: {element!r}"
    )


# ---------------------------------------------------------------------------
# AC5: Enable-gate JS: onchange on codex_enabled calls a function that mutates disabled
# ---------------------------------------------------------------------------


def test_enable_gate_js_function_toggles_disabled():
    """AC5: The enabled <select> must have an onchange attribute naming a function,
    and that exact named function body must contain a disabled mutation.

    Approach:
    1. Isolate the codex section block.
    2. Find the <select name='enabled'> element and read its onchange value.
       (Element switched from <input type='checkbox'> to <select> Yes/No
       to match the rest of the template's enable-style toggles and to fix
       a visual checkbox/label overlap issue.)
    3. Locate the named function (classic or assignment style) in the section's script block.
    4. Assert the function body contains a disabled mutation.
    """
    codex_html = _read_codex_section()

    # Step 1: get the enabled <select> element
    enabled_element = _extract_select_block(codex_html, "enabled")

    # Step 2: extract the function name from onchange
    func_name = _extract_onchange_function_name(enabled_element)
    assert func_name, (
        f"No onchange='funcName()' found on enabled <select>. Element: {enabled_element!r}"
    )

    # Step 3: find the script block and extract the named function body
    script_start = codex_html.find("<script")
    assert script_start != -1, "No <script block found in codex section"
    script_end = codex_html.find("</script>", script_start)
    assert script_end != -1, "Unclosed <script in codex section"
    script_block = codex_html[script_start : script_end + len("</script>")]

    handler_body = _extract_named_function_body(script_block, func_name)
    assert handler_body, (
        f"Function '{func_name}' (from enabled onchange) not found in codex section script"
    )

    # Step 4: the named function body must contain a disabled mutation
    has_disabled_mutation = (
        ".disabled" in handler_body
        or "setAttribute('disabled'" in handler_body
        or 'setAttribute("disabled"' in handler_body
        or "removeAttribute('disabled'" in handler_body
        or 'removeAttribute("disabled"' in handler_body
        or "disabled =" in handler_body
        or "disabled=" in handler_body
    )
    assert has_disabled_mutation, (
        f"Function '{func_name}' does not mutate 'disabled'. Body: {handler_body!r}"
    )


# ---------------------------------------------------------------------------
# Finding 1 (CRITICAL): HTML form-field name attributes must match handler keys
# ---------------------------------------------------------------------------


def _extract_edit_form_inputs(html: str) -> list:
    """Extract all name= attribute values from <input> and <select> elements
    inside the codex edit form (id='edit-form-codex-integration').

    Returns a list of (element_type, name_value) tuples, skipping hidden inputs
    (csrf_token) which are not dispatched by the handler.
    """
    form_marker = 'id="edit-form-codex-integration"'
    form_start = html.find(form_marker)
    assert form_start != -1, "No edit-form-codex-integration found in codex section"
    # Find the enclosing <form tag start
    tag_start = html.rfind("<form", 0, form_start + len(form_marker))
    assert tag_start != -1, "No opening <form before edit-form-codex-integration"
    form_end = html.find("</form>", tag_start)
    assert form_end != -1, "No closing </form> for edit-form-codex-integration"
    form_html = html[tag_start : form_end + len("</form>")]

    results = []
    import re

    # Match <input ... name="VALUE" ... > or <select ... name="VALUE" ...>
    pattern = re.compile(
        r'<(input|select)\b[^>]*\bname=["\']([^"\']+)["\'][^>]*>', re.DOTALL
    )
    for m in pattern.finditer(form_html):
        elem_type = m.group(1)
        name_val = m.group(2)
        # Skip the csrf_token hidden field — not dispatched by the codex handler
        if name_val == "csrf_token":
            continue
        results.append((elem_type, name_val))
    return results


def test_form_field_names_match_handler_keys():
    """Finding 1 (CRITICAL): Every <input> and <select> name= attribute in the
    codex edit form must be one of the 6 keys accepted by _update_codex_integration_setting.

    The handler dispatches on these exact keys:
      enabled, credential_mode, api_key, lcp_url, lcp_vendor, codex_weight

    The pre-fix template uses prefixed names (codex_enabled, codex_credential_mode,
    codex_api_key) which the handler rejects with ValueError. This test catches that.
    """
    codex_html = _read_codex_section()
    accepted_keys = {"enabled", "credential_mode", "api_key", "lcp_url", "lcp_vendor", "codex_weight"}
    fields = _extract_edit_form_inputs(codex_html)
    assert fields, "No form fields found in codex edit form"
    bad_names = [
        (elem, name) for elem, name in fields if name not in accepted_keys
    ]
    assert not bad_names, (
        f"Form fields with names NOT accepted by handler: {bad_names}. "
        f"Accepted keys: {sorted(accepted_keys)}"
    )


# ---------------------------------------------------------------------------
# Finding 2 (HIGH): Read-only API key cell must not expose the raw key value
# ---------------------------------------------------------------------------


def test_read_only_api_key_does_not_expose_plaintext():
    """Finding 2 (HIGH): The display-mode table cell for API Key must NOT render
    the literal api_key value. It must use a masked representation (e.g. '***'
    or '********') instead of the Jinja2 expression that echoes the raw key.

    We verify this by asserting the template source does NOT contain a Jinja2
    expression that directly outputs api_key without masking, i.e.:
      {{ config.codex_integration.api_key or '(not set)' }}
    must NOT be present — it must be replaced with a masked variant like:
      {% if config.codex_integration.api_key %}***{% else %}(not set){% endif %}
    """
    codex_html = _read_codex_section()
    # The unmasked expression that must be removed
    unmasked_expr = "{{ config.codex_integration.api_key or '(not set)' }}"
    assert unmasked_expr not in codex_html, (
        "Display-mode API Key cell still uses the unmasked expression "
        f"{unmasked_expr!r}. Replace with a masked variant (e.g. "
        "{% if config.codex_integration.api_key %}***{% else %}(not set){% endif %})."
    )
    # Also assert that a masking sentinel is present (*** or ******** style)
    has_mask = "***" in codex_html
    assert has_mask, (
        "Display-mode API Key cell has no masking sentinel (expected '***' or '********')"
    )


# ---------------------------------------------------------------------------
# Finding 3 (MEDIUM): toggleCodexCredentialFields must be a real implementation
# ---------------------------------------------------------------------------


def test_toggle_credential_fields_is_implemented():
    """Finding 3 (MEDIUM): toggleCodexCredentialFields() must:
    1. Contain conditional checks for both 'api_key' and 'subscription' modes.
    2. Be wired to the credential-mode <select> via onchange.
    3. Reference distinct wrapper element IDs for api_key and lcp field groups.

    The pre-fix stub body is only a comment — this test catches that.
    """
    codex_html = _read_codex_section()

    # 1. Extract the function body
    script_start = codex_html.find("<script")
    assert script_start != -1, "No <script block found in codex section"
    script_end = codex_html.find("</script>", script_start)
    assert script_end != -1, "Unclosed <script in codex section"
    script_block = codex_html[script_start : script_end + len("</script>")]

    func_body = _extract_named_function_body(script_block, "toggleCodexCredentialFields")
    assert func_body, "toggleCodexCredentialFields function not found in codex section script"

    # Must check for api_key mode
    assert "api_key" in func_body, (
        f"toggleCodexCredentialFields body does not check for 'api_key' mode. Body: {func_body!r}"
    )
    # Must check for subscription mode
    assert "subscription" in func_body, (
        f"toggleCodexCredentialFields body does not check for 'subscription' mode. Body: {func_body!r}"
    )

    # 2. The select must have onchange="toggleCodexCredentialFields()"
    select_block = _extract_select_block(codex_html, "credential_mode")
    assert "toggleCodexCredentialFields" in select_block, (
        "credential_mode <select> does not have onchange wired to toggleCodexCredentialFields"
    )

    # 3. The wrapper element IDs referenced in the JS body must exist in the HTML
    import re
    ids_in_func = re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", func_body)
    assert ids_in_func, (
        f"toggleCodexCredentialFields does not call getElementById. Body: {func_body!r}"
    )
    # At least 2 distinct wrapper IDs must exist (one for api_key, one for lcp)
    assert len(set(ids_in_func)) >= 2, (
        f"toggleCodexCredentialFields references fewer than 2 distinct element IDs: {ids_in_func}"
    )
    for elem_id in ids_in_func:
        assert f'id="{elem_id}"' in codex_html or f"id='{elem_id}'" in codex_html, (
            f"Element ID '{elem_id}' referenced in toggleCodexCredentialFields "
            f"does not exist in the codex section HTML"
        )


# ---------------------------------------------------------------------------
# Story #844 Placement: Codex section must be immediately after Claude CLI
# ---------------------------------------------------------------------------

TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "partials"
    / "config_section.html"
)


def test_codex_section_appears_immediately_after_claude_section():
    """Story #844 placement constraint: Codex section MUST appear after the
    Claude CLI Integration section (its conceptual sibling), not at the end of
    the template after wiki/langfuse/etc. Verifies section ordering."""
    text = TEMPLATE_PATH.read_text()
    claude_idx = text.find('id="section-claude-integration"')
    codex_idx = text.find('id="section-codex-integration"')
    wiki_idx = text.find('id="section-wiki"')
    assert claude_idx > 0, "Claude integration section not found"
    assert codex_idx > 0, "Codex integration section not found"
    assert wiki_idx > 0, "Wiki section not found (sanity)"
    # Codex must come AFTER Claude
    assert codex_idx > claude_idx, "Codex must come after Claude in template"
    # Codex must come BEFORE wiki (it's a Claude-sibling, not a tail-section)
    assert codex_idx < wiki_idx, (
        f"Codex section is misplaced: appears after wiki section. "
        f"Claude at {claude_idx}, codex at {codex_idx}, wiki at {wiki_idx}. "
        f"Codex should be right after Claude (Claude-sibling)."
    )
    # Tighter: nothing between Claude's closing </details> and Codex's opening
    # <details> tag except whitespace and the Story #844 comment marker.
    # Note: codex_idx points to the id= attribute (mid-tag), so we anchor on the
    # codex <details> opening tag start (the actual element boundary).
    claude_end = text.find('</details>', claude_idx)
    codex_open = text.rfind('<details', 0, codex_idx)
    between = text[claude_end + len('</details>'):codex_open]
    # Allow whitespace and the Story #844 comment, nothing else
    cleaned = between.strip().replace('<!-- Story #844: Codex CLI Integration -->', '').strip()
    assert cleaned == '', (
        f"Unexpected content between Claude </details> and Codex <details>: {cleaned!r}"
    )


# UX preference: Codex Weight is a numeric input box, not a range slider.
# (Per user feedback during #844 manual review — operators want to type the value.)
def test_codex_weight_input_is_type_number_not_range():
    """Codex Weight must be type='number' (numeric box), NOT type='range' (slider).

    Operators want to type the value directly rather than drag a slider.
    Validation attributes (min='0', max='1', step) are still required so the
    browser enforces the [0.0, 1.0] range.
    """
    codex_html = _read_codex_section()
    name_idx = codex_html.find('name="codex_weight"')
    assert name_idx != -1, "codex_weight input not found in Codex section"
    open_idx = codex_html.rfind('<input', 0, name_idx)
    close_idx = codex_html.find('>', name_idx)
    assert open_idx != -1 and close_idx != -1, "Could not locate codex_weight <input> element bounds"
    weight_element = codex_html[open_idx : close_idx + 1]
    assert 'type="number"' in weight_element, (
        f"Codex Weight must be type='number' (numeric box), not type='range' (slider). "
        f"Element found: {weight_element!r}"
    )
    assert 'type="range"' not in weight_element, (
        f"Codex Weight must NOT be type='range' (slider). Element found: {weight_element!r}"
    )


def _is_row_inside_jinja_guard(html: str, opener: str, row_marker: str) -> bool:
    """Verify ``row_marker`` is contained inside a {% if ... %}...{% endif %} block
    opened by ``opener``. Returns True if and only if the row sits between an
    opener and its matching {% endif %} (i.e., the conditional has not closed
    before the row appears).

    Walks every occurrence of ``opener`` in source order; for each occurrence,
    finds the matching {% endif %} by counting nested {% if %}/{% endif %}
    depth, and checks whether ``row_marker`` falls inside that span.
    """
    pos = 0
    while True:
        opener_idx = html.find(opener, pos)
        if opener_idx == -1:
            return False
        # Walk forward counting Jinja-conditional depth to find this opener's endif
        depth = 1
        scan = opener_idx + len(opener)
        while depth > 0 and scan < len(html):
            next_if = html.find('{% if ', scan)
            next_endif = html.find('{% endif %}', scan)
            if next_endif == -1:
                return False  # malformed template
            if next_if != -1 and next_if < next_endif:
                depth += 1
                scan = next_if + len('{% if ')
            else:
                depth -= 1
                if depth == 0:
                    endif_idx = next_endif
                    break
                scan = next_endif + len('{% endif %}')
        else:
            return False
        # The row is contained if it falls between the opener's end and the matching endif
        block = html[opener_idx + len(opener) : endif_idx]
        if row_marker in block:
            return True
        pos = endif_idx + len('{% endif %}')


# Display mode parity: rows specific to a credential mode appear ONLY when that
# mode is active, mirroring the edit-mode visibility rules.
def test_display_mode_hides_credential_specific_rows_when_mode_does_not_match():
    """Display table must use Jinja {% if credential_mode == 'X' %}...{% endif %}
    guards that *wrap* the rows that are mode-specific (not merely precede them).

    Without these guards, all rows (API Key, LCP URL, LCP Vendor) are visible
    regardless of the active credential_mode — confusing operators because the
    edit form correctly hides those fields based on the mode select.
    """
    codex_html = _read_codex_section()

    api_opener = "{% if config.codex_integration.credential_mode == 'api_key' %}"
    sub_opener = "{% if config.codex_integration.credential_mode == 'subscription' %}"

    assert _is_row_inside_jinja_guard(codex_html, api_opener, ">API Key</td>"), (
        "API Key display row is not contained within a "
        "{% if credential_mode == 'api_key' %}...{% endif %} block."
    )
    assert _is_row_inside_jinja_guard(codex_html, sub_opener, ">LCP URL</td>"), (
        "LCP URL display row is not contained within a "
        "{% if credential_mode == 'subscription' %}...{% endif %} block."
    )
    assert _is_row_inside_jinja_guard(codex_html, sub_opener, ">LCP Vendor</td>"), (
        "LCP Vendor display row is not contained within a "
        "{% if credential_mode == 'subscription' %}...{% endif %} block."
    )


# UX consistency: enabled is a Yes/No <select>, NOT a checkbox.
# Mirrors every other 'enabled'-style toggle in this template (oidc, langfuse,
# claude_cli description_refresh_enabled, dependency_map_enabled, etc.). Prevents
# the visual overlap bug observed when a naked <input type="checkbox"> was used.
def test_enabled_is_yes_no_select_not_checkbox():
    """The 'enabled' field must be a <select> with Yes/No options, not a checkbox.

    Operators expect consistency with the rest of the config screen — every other
    boolean-style toggle (OIDC enabled, Langfuse enabled, Claude refresh enabled, etc.)
    is a <select>, not a checkbox. A checkbox produced visual layout issues with the
    surrounding label text in the form-grid CSS.
    """
    codex_html = _read_codex_section()

    # Find the enabled <select> block — must succeed
    select_block = _extract_select_block(codex_html, "enabled")
    assert select_block, "Could not find <select name='enabled'> in Codex section"
    assert "<select" in select_block.lower(), (
        f"'enabled' element must be a <select>, got: {select_block!r}"
    )

    # The block must contain BOTH option values
    assert 'value="true"' in select_block, (
        f"<select name='enabled'> missing 'true' option. Block: {select_block!r}"
    )
    assert 'value="false"' in select_block, (
        f"<select name='enabled'> missing 'false' option. Block: {select_block!r}"
    )

    # Negative assertion: the Codex section must NOT contain a checkbox-style
    # input named 'enabled' (would indicate regression to the old form).
    assert 'type="checkbox" id="codex-enabled"' not in codex_html, (
        "Regression: Codex 'enabled' field is a <input type='checkbox'> again. "
        "It should be a <select> Yes/No to match the rest of the template."
    )
    assert 'type="checkbox" name="enabled"' not in codex_html, (
        "Regression: Codex 'enabled' field is a checkbox. Expected <select> Yes/No."
    )
