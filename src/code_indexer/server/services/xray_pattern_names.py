# ^ invariant the one rule for X-Ray pattern names and scopes; xray_pattern_service and the audit field type use it
from typing import Any

_PATH_TRAVERSAL_PARTS = ("/", "\\", "..")
_MAX_FILE_NAME_BYTES = 255
# ^ coupled with global_repos/catalog_staged_write.py:publish_catalog_file mkstemp prefix "." + name, suffix ".tmp"
# ^ external CPython tempfile names carry 8 random characters
_STAGED_NAME_OVERHEAD = len(".") + len(".yaml") + len(".") + 8 + len(".tmp")


def has_path_traversal(value: str) -> bool:
    return any(part in value for part in _PATH_TRAVERSAL_PARTS)


def is_storable_pattern_name(name: Any) -> bool:
    if not isinstance(name, str) or not name or has_path_traversal(name):
        return False
    encoded = name.encode("utf-8", "surrogatepass")
    return len(encoded) + _STAGED_NAME_OVERHEAD <= _MAX_FILE_NAME_BYTES
