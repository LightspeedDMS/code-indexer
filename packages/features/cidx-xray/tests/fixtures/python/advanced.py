"""Advanced Python: walrus operator, type aliases, comprehensions, generators."""

from __future__ import annotations

from typing import Generator, List

# Type aliases (Python 3.9 compatible)
Vector = List[float]
Matrix = List[List[float]]


def dot_product(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b))


def matrix_multiply(a: Matrix, b: Matrix) -> Matrix:
    cols_b = list(zip(*b))
    return [[dot_product(row_a, col_b) for col_b in cols_b] for row_a in a]  # type: ignore[arg-type]


# Walrus operator
def first_long_word(words: List[str], min_len: int = 5) -> "str | None":
    return next(
        (word for word in words if (_n := len(word)) >= min_len),  # noqa: F841
        None,
    )


def parse_chunks(data: bytes, chunk_size: int = 1024) -> List[bytes]:
    chunks = []
    offset = 0
    while chunk := data[offset : offset + chunk_size]:
        chunks.append(chunk)
        offset += chunk_size
    return chunks


# HTTP status classifier (if/elif chain — Python 3.9 compatible)
def classify_http_status(status: int) -> str:
    if status == 200:
        return "OK"
    elif status == 201:
        return "Created"
    elif status == 204:
        return "No Content"
    elif status == 400:
        return "Bad Request"
    elif status == 401:
        return "Unauthorized"
    elif status == 403:
        return "Forbidden"
    elif status == 404:
        return "Not Found"
    elif status == 422:
        return "Unprocessable Entity"
    elif status == 429:
        return "Too Many Requests"
    elif status == 500:
        return "Internal Server Error"
    elif 200 <= status < 300:
        return "2xx Success"
    elif 300 <= status < 400:
        return "3xx Redirect"
    elif 400 <= status < 500:
        return "4xx Client Error"
    elif 500 <= status < 600:
        return "5xx Server Error"
    else:
        return "Unknown"


def process_command(cmd: dict) -> str:
    action = cmd.get("action")
    if action == "create" and "name" in cmd:
        return f"creating {cmd['name']}"
    elif action == "update" and "id" in cmd and "fields" in cmd:
        return f"updating {cmd['id']} with {len(cmd['fields'])} fields"
    elif action == "delete" and "id" in cmd:
        return f"deleting {cmd['id']}"
    elif isinstance(action, str):
        return f"unknown action: {action}"
    else:
        return "invalid command"


# Complex comprehensions
def build_lookup(items: List[tuple]) -> dict:
    lookup: dict = {}
    for key, value in items:
        lookup.setdefault(key, []).append(value)
    return {k: sorted(v) for k, v in lookup.items()}


def flatten_nested(data: list, depth: int = -1) -> list:
    result = []
    for item in data:
        if isinstance(item, list) and depth != 0:
            result.extend(flatten_nested(item, depth - 1))
        else:
            result.append(item)
    return result


# Generator with send protocol
def accumulator() -> Generator[float, float, str]:
    total = 0.0
    count = 0
    while True:
        value = yield total
        if value is None:
            break
        total += value
        count += 1
    return f"sum={total} count={count}"


# Context manager protocol
class Timer:
    def __init__(self, name: str = "timer") -> None:
        self.name = name
        self.elapsed: float = 0.0

    def __enter__(self) -> "Timer":
        import time

        self._start = time.monotonic()
        return self

    def __exit__(self, *_: object) -> None:
        import time

        self.elapsed = time.monotonic() - self._start


# f-string with complex expressions
def format_stats(data: List[int]) -> str:
    if not data:
        return "empty"
    total = sum(data)
    mean = total / len(data)
    sorted_data = sorted(data)
    median = sorted_data[len(data) // 2]
    return (
        f"n={len(data)} sum={total} mean={mean:.2f} "
        f"median={median} min={min(data)} max={max(data)}"
    )


# Class with rich protocol
class Point:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y

    def __repr__(self) -> str:
        return f"Point(x={self.x}, y={self.y})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Point):
            return NotImplemented
        return self.x == other.x and self.y == other.y


def describe_point(p: Point) -> str:
    if p.x == 0 and p.y == 0:
        return "origin"
    elif p.x == 0:
        return f"on y-axis at {p.y}"
    elif p.y == 0:
        return f"on x-axis at {p.x}"
    elif p.x == p.y:
        return f"on diagonal at {p.x}"
    else:
        return f"at ({p.x}, {p.y})"
