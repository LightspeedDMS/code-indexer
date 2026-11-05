"""X-Ray's server side: what the ``xray`` feature contributes to the core.

==========================  ===================================================
Module                      Contribution
==========================  ===================================================
``mcp``                     MCP tools ``xray_search``, ``xray_explore``,
                            ``xray_dump_ast``, ``store_xray_pattern``,
                            ``xray_search_batch``, ``analyze_graph``
                            (``tools()``; docs in ``tool_docs/``)
``routes``                  ``POST /api/xray/search``,
                            ``POST /api/xray/search/batch`` (``routers()``)
``pattern_service``         the X-Ray pattern library in cidx-meta
``graph_governor``          graph-build memory admission, wire-file cache, K
                            calibration (its table: ``migrations``)
``backend_wiring``,         where evaluators execute (``xray_backend``) and the
``backend_plugin``          delegated backend plugin
``lifecycle``, ``runtime``  ``on_startup``/``on_shutdown`` and what they wire
==========================  ===================================================

Imported only by the server (through the feature's hooks): the CLI and the
engine never load it.
"""
