"""
Shared support for the template/JS handler-argument tests: renders a
template through a real Jinja2 environment, decodes HTML attributes the
way a browser does, and (when Node.js is available) executes an
extracted inline event-handler for real to prove the invariant these
tests exist to guard:

    A handler's argument equals the value the server rendered, for any
    value the field can legitimately hold.

Not a test module itself (pytest only collects `test_*.py`).
"""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import jinja2

REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "src/code_indexer/server/web/templates"
STATIC_JS_DIR = REPO_ROOT / "src/code_indexer/server/web/static/js"

# A single value combining every character class a handler argument must
# carry through unchanged: an apostrophe, a double quote, an angle
# bracket, a backslash, and an embedded newline.
ADVERSARIAL_VALUE = 'o\'brien "jane" <tag> back\\slash\nline2'


def render(
    relative_path: str, context: Dict, extra_filters: Optional[Dict] = None
) -> str:
    """Render a template from the real templates directory through a
    real Jinja2 environment (autoescape on, matching production)."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True
    )
    if extra_filters:
        env.filters.update(extra_filters)
    template = env.get_template(relative_path)
    return template.render(**context)


class AttrCollectingParser(HTMLParser):
    """Collects every tag's attributes, in document order. HTMLParser
    performs HTML-entity decoding on attribute values -- exactly what a
    browser does before handing an inline handler's text to the
    JavaScript engine."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, Optional[str]]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.tags.append((tag, dict(attrs)))


def tags_with_handler_containing(
    html: str, needle: str, attr_names: Tuple[str, ...] = ("onclick", "onchange")
) -> List[Tuple[str, Dict[str, Optional[str]]]]:
    """Return (attr_name, attrs) for every tag whose onclick/onchange
    text contains `needle`."""
    parser = AttrCollectingParser()
    parser.feed(html)
    matches = []
    for _tag, attrs in parser.tags:
        for attr_name in attr_names:
            value = attrs.get(attr_name)
            if value is not None and needle in value:
                matches.append((attr_name, attrs))
                break
    return matches


def dataset_from_attrs(attrs: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Convert every data-* HTML attribute on a decoded attrs dict into
    the camelCase keys a real browser exposes on `element.dataset`."""
    result: Dict[str, str] = {}
    for key, value in attrs.items():
        if key.startswith("data-") and value is not None:
            parts = key[len("data-") :].split("-")
            camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
            result[camel] = value
    return result


def node_available() -> bool:
    return shutil.which("node") is not None


_NODE_SCRIPT_TEMPLATE = r"""
const dataset = __DATASET_JSON__;
const handlerSrc = __HANDLER_JSON__;
const calls = [];
const proxy = new Proxy({}, {
  // Real JS built-ins (Number, String, ...) resolve normally; every other
  // bare identifier is a page function and is recorded instead.
  has(target, prop) { return prop === 'event' || !(prop in globalThis); },
  get(target, prop) {
    if (prop === 'event') {
      return { stopPropagation() {} };
    }
    if (typeof prop === 'symbol') {
      return undefined;
    }
    return function(...args) {
      calls.push({ fn: prop, args: args });
      return undefined;
    };
  }
});
const thisObj = { dataset: dataset };
// `with(proxy)` intercepts EVERY bare identifier lookup inside it,
// including plumbing names like `proxy`/`thisObj` themselves -- so the
// `with` block wraps ONLY the handler source, and `.call(thisObj)`
// happens from OUTSIDE it. `this` is a keyword, not a bare identifier,
// so `with` never affects it: `this.dataset.x` inside the handler
// resolves against the real bound `thisObj`.
const runnerBody = "with (proxy) {" + handlerSrc + "}";
const runner = new Function('proxy', runnerBody);
runner.call(thisObj, proxy);
process.stdout.write(JSON.stringify(calls));
"""


def execute_handler_in_node(
    handler_src: str, dataset: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Execute `handler_src` (an onclick/onchange attribute's decoded
    text) in a real Node.js process with `this.dataset` bound to
    `dataset`. Every bare-identifier function call the handler makes
    (e.g. `toggleEditForm(...)`) is intercepted by a `with`-scoped Proxy
    and recorded instead of executed for real -- there is no such
    global function in this process. `this.dataset.x` property access
    is untouched by the proxy (only bare identifiers are intercepted),
    so it resolves against the real bound `dataset`.

    Returns the list of {"fn": name, "args": [...]} calls, in order.
    """
    script = _NODE_SCRIPT_TEMPLATE.replace(
        "__DATASET_JSON__", json.dumps(dataset)
    ).replace("__HANDLER_JSON__", json.dumps(handler_src))
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"node execution of handler failed: {result.stderr}\nhandler: {handler_src!r}"
    )
    return cast(List[Dict[str, Any]], json.loads(result.stdout))


def extract_balanced_js_block(source: str, start_marker: str) -> str:
    """Extract a JS statement's REAL source text, from `start_marker`
    through its matching closing `}` (found by character-level brace
    depth counting, including template-literal `${...}` interpolation
    braces, which nest and match the same way at the character level).

    Used to execute the exact production code a test is verifying,
    rather than a hand-retyped copy that could silently drift from it.
    """
    start = source.index(start_marker)
    depth = 0
    started = False
    i = start
    while i < len(source):
        c = source[i]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"no balanced closing brace found for {start_marker!r}")


# Shared minimal DOM stub for tests that execute real production JS
# (not templates) which builds controls via createElement/appendChild/
# addEventListener rather than an HTML string. Factored out because three
# Node-execution tests needed the identical stub (Anti-Duplication rule).
DOM_STUB_JS = r"""
function makeElement() {
    return {
        className: '', textContent: '', title: '', href: '', target: '',
        style: {},
        _children: [], _listeners: {},
        appendChild(child) { this._children.push(child); },
        addEventListener(evt, fn) { this._listeners[evt] = fn; },
    };
}
const containers = {};
function getContainer(id) {
    if (!containers[id]) { containers[id] = makeElement(); }
    return containers[id];
}
const document = { createElement: makeElement, getElementById: getContainer };
"""


def execute_js_in_node(script: str, timeout: float = 15) -> subprocess.CompletedProcess:
    """Run an arbitrary Node.js script (a string passed to `node -e`)
    and return the completed process (stdout/stderr/returncode) for the
    caller to assert on."""
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
