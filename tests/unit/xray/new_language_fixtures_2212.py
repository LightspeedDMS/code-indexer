"""Bug #2212 fixtures: one small file per extension newly accepted by search."""

from pathlib import Path
from typing import Dict, Tuple

MARKER = "example_marker"

# extension -> (file name, content, expected language label)
NEW_LANGUAGE_FILES: Dict[str, Tuple[str, str, str]] = {
    "jsx": (
        "view.jsx",
        f'const Example = () => <div className="{MARKER}">{{value}}</div>;\n',
        "javascript",
    ),
    "tf": (
        "main.tf",
        f'resource "example_resource" "{MARKER}" {{\n  count = 2\n}}\n',
        "terraform",
    ),
    "hcl": ("settings.hcl", f"{MARKER} {{\n  enabled = true\n}}\n", "hcl"),
    "yml": ("list.yml", f"{MARKER}:\n  - one\n  - two\n", "yaml"),
    "yaml": (
        "service.yaml",
        f"service:\n  name: {MARKER}\n  ports: [80, 443]\n",
        "yaml",
    ),
    "sql": ("query.sql", f"SELECT id FROM {MARKER} WHERE id = 1;\n", "sql"),
    "xml": (
        "data.xml",
        f'<?xml version="1.0"?>\n<root><item name="{MARKER}"/></root>\n',
        "xml",
    ),
    "groovy": (
        "script.groovy",
        f'def {MARKER}() {{\n    println "hello"\n}}\n',
        "groovy",
    ),
    "gradle": (
        "build.gradle",
        f'task {MARKER} {{\n    doLast {{ println "hello" }}\n}}\n',
        "groovy",
    ),
    "vue": (
        "Card.vue",
        f'<template>\n  <div class="{MARKER}">{{{{ title }}}}</div>\n</template>\n\n'
        "<script>\nexport default { data() { return { title: 'hi' } } }\n</script>\n\n"
        "<style>\n.card { margin: 0; }\n</style>\n",
        "vue",
    ),
}

PYTHON_PARSEABLE = {"jsx", "tf", "hcl", "yml", "yaml"}
# ^ why vue refused, not parsed as HTML; python HTML root = fragment != engine root = document; #2211
PYTHON_REFUSED = {"sql", "xml", "groovy", "gradle", "vue"}


def write_new_language_repo(directory: Path) -> Path:
    repo = directory / "repo"
    repo.mkdir()
    for name, content, _ in NEW_LANGUAGE_FILES.values():
        (repo / name).write_text(content, encoding="utf-8")
    return repo
