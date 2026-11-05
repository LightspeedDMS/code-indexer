"""Pathological Python: complex patterns for AST parsing stress testing."""

from typing import Any

MIN_STRIP_LENGTH = 2
VERY_NEGATIVE_THRESHOLD = -1000
NEGATIVE_THRESHOLD = -100
SMALL_POSITIVE_THRESHOLD = 10
MEDIUM_POSITIVE_THRESHOLD = 100
LARGE_POSITIVE_THRESHOLD = 1000
LOOKUP_RANGE_SIZE = 20
MIN_UNPACK_SIZE = 2


def deep_chain(data: list[list[list[str]]]) -> str:
    """Flatten, filter, and join deeply nested string lists."""
    return ", ".join(
        sorted(
            set(
                s.strip().lower().replace(" ", "_")
                for outer in data
                for inner in outer
                for s in inner
                if s and s.strip() and len(s.strip()) >= MIN_STRIP_LENGTH
            )
        )
    )


def classify(x: int) -> str:
    """Classify integer into named range using chained elif."""
    if x < VERY_NEGATIVE_THRESHOLD:
        return "very negative"
    elif x < NEGATIVE_THRESHOLD:
        return "negative"
    elif x < 0:
        return "slightly negative"
    elif x == 0:
        return "zero"
    elif x < SMALL_POSITIVE_THRESHOLD:
        return "tiny positive"
    elif x < MEDIUM_POSITIVE_THRESHOLD:
        return "small positive"
    elif x < LARGE_POSITIVE_THRESHOLD:
        return "large positive"
    else:
        return "huge positive"


def complex_comprehension(items: list[dict[str, Any]]) -> list[str]:
    """Filter and format items with complex predicate."""
    return [
        f"{item['name']}:{item['value']}"
        for item in items
        if isinstance(item, dict)
        and "name" in item
        and "value" in item
        and isinstance(item["name"], str)
        and item["name"].strip()
        and item["value"] is not None
        and str(item["value"]).strip()
    ]


def pivot_table(
    rows: list[dict[str, Any]], key_col: str, val_col: str
) -> dict[str, list[Any]]:
    """Build a pivot table grouping values by key column."""
    return {
        k: [row[val_col] for row in rows if row.get(key_col) == k]  # type: ignore[misc]
        for k in {row.get(key_col) for row in rows if key_col in row}
    }


def apply_transform(f: Any, g: Any, x: Any) -> Any:
    """Apply f composed with g to x (named function instead of nested lambda)."""
    return f(g(x))


def double(x: int) -> int:
    """Double an integer."""
    return x * 2


def stats(nums: list[float]) -> tuple[float, float, float]:
    """Return mean, variance, std_dev. Returns (0.0, 0.0, 0.0) for empty input."""
    filtered = [n for n in nums if isinstance(n, (int, float)) and not (n != n)]
    n = len(filtered)
    if n == 0:
        # Documented fallback: empty or all-NaN input produces zero statistics
        return 0.0, 0.0, 0.0
    mean = sum(filtered) / n
    variance = sum((x - mean) ** 2 for x in filtered) / n
    std_dev = variance**0.5
    return mean, variance, std_dev


def unpack_demo(data: list[Any]) -> tuple[Any, list[Any], Any]:
    """Unpack first, middle, last elements. Requires at least 2 elements."""
    if len(data) < MIN_UNPACK_SIZE:
        raise ValueError(f"data must have at least {MIN_UNPACK_SIZE} elements")
    first, *middle, last = data
    return first, middle, last


def merge_dicts(*dicts: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge multiple dicts, concatenating lists and recursing into dicts."""
    result: dict[str, Any] = {}
    for d in dicts:
        for k, v in d.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = merge_dicts(result[k], v)
            elif k in result and isinstance(result[k], list) and isinstance(v, list):
                result[k] = result[k] + v
            else:
                result[k] = v
    return result


LOOKUP: dict[str, Any] = {
    f"key_{i}": {"index": i, "square": i * i, "cube": i**3, "even": i % 2 == 0}
    for i in range(LOOKUP_RANGE_SIZE)
}
