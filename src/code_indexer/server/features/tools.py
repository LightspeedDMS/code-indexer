"""Consume ``tools()``: feature MCP tools in the core's tool registry.

Each ``ToolContribution`` becomes, exactly like a core tool:

* a ``TOOL_REGISTRY`` entry built from its doc (``mcp/tools.py``), so
  ``tools/list`` shows it to callers holding its ``required_permission`` and
  ``tools/call`` authorizes it with the same checks (role, group grants,
  repository pre-check);
* a ``HANDLER_REGISTRY`` entry (``mcp/handlers``), dispatched by
  ``mcp/protocol.py`` on a worker thread with the core's timeout;
* a doc in the tool-doc loader, so ``cidx_quick_reference`` can show it.

The doc ships inside the feature's wheel (``ToolContribution.doc_markdown``)
and is parsed with the core's own tool-doc parser, so the CI gate
``tools/verify_tool_docs.py`` (core docs) is untouched.

Installed tools are process wiring: the same on every node of a deployment
(they follow ``enabled_features``), replaced as a whole whenever an app is
built, so building an app without a feature removes that feature's tools.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from cidx_sdk import FeatureLoadError, ToolContribution

from code_indexer.server.features.loader import LoadedFeature

_lock = threading.Lock()
#: tool name -> feature name, for the tools this process installed.
_installed: Dict[str, str] = {}


def installed_feature_tools() -> Dict[str, str]:
    """Tool name -> feature name for every installed feature tool."""
    with _lock:
        return dict(_installed)


def _doc_source(feature: str, tool: str) -> Path:
    """A label for the doc in messages; never opened (the doc is in memory)."""
    return Path(f"<feature {feature}>") / f"{tool}.md"


def _prepare(
    feature: str, contribution: ToolContribution, core_tools: Any
) -> Tuple[Any, Dict[str, Any]]:
    from code_indexer.server.mcp.tool_doc_loader import (
        FrontmatterValidationError,
        parse_tool_doc,
        tool_definition,
    )

    name = contribution.name
    try:
        doc = parse_tool_doc(contribution.doc_markdown, _doc_source(feature, name))
    except FrontmatterValidationError as exc:
        raise FeatureLoadError(f"feature {feature!r} tool {name!r}: {exc}") from exc
    if doc.name != name:
        raise FeatureLoadError(
            f"feature {feature!r} tool {name!r}: its doc is named {doc.name!r}"
        )
    if doc.inputSchema is None:
        raise FeatureLoadError(
            f"feature {feature!r} tool {name!r}: its doc has no inputSchema"
        )
    wanted = contribution.required_permission
    if wanted is not None and wanted != doc.required_permission:
        raise FeatureLoadError(
            f"feature {feature!r} tool {name!r}: required_permission {wanted!r} "
            f"differs from its doc's {doc.required_permission!r}"
        )
    if name in core_tools:
        raise FeatureLoadError(
            f"feature {feature!r} tool {name!r} collides with a core MCP tool"
        )
    return doc, tool_definition(doc)


def _uninstall_locked(tool_registry: Any, handlers: Any, docs: Any) -> None:
    for name in list(_installed):
        tool_registry.pop(name, None)
        handlers.pop(name, None)
        docs.unregister_doc(name)
        del _installed[name]


def install_feature_tools(loaded: Sequence[LoadedFeature]) -> List[str]:
    """Replace the installed feature tools with those of *loaded*.

    All tools are validated before any is installed; a refusal installs
    nothing new (the previous feature tools are already removed).
    Blocking (parses docs; may load the core tool docs): call it while the
    app is built, never on the event loop. Returns the installed names.

    Raises:
        FeatureLoadError: a doc is invalid, names another tool, has no
            inputSchema or disagrees on the permission, or a tool collides
            with a core tool.
    """
    wanted = [
        (item.manifest.name, tool)
        for item in loaded
        for tool in item.contributions.tools
    ]
    with _lock:
        if not wanted and not _installed:
            return []
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        docs = _get_tool_doc_loader()
        _uninstall_locked(TOOL_REGISTRY, HANDLER_REGISTRY, docs)
        core_tools = set(TOOL_REGISTRY) | set(HANDLER_REGISTRY)
        prepared = [
            (feature, tool, *_prepare(feature, tool, core_tools))
            for feature, tool in wanted
        ]
        for feature, tool, doc, definition in prepared:
            docs.register_doc(doc)
            TOOL_REGISTRY[tool.name] = definition
            HANDLER_REGISTRY[tool.name] = tool.handler
            _installed[tool.name] = feature
        return [tool.name for _feature, tool, _doc, _def in prepared]
