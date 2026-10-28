import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .client import AbortScenarioError, RequestFailedError, TransportError, scrub

MIN_REQUESTS_PER_WORKER = 4
MAX_ERROR_SAMPLES = 3
MAX_WRONG_SAMPLES = 5
METRICS = (
    "requests",
    "served",
    "rps",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "errors",
    "wrong",
)

# ^ invariant item returns None = expected hit present, str = wrong_result detail
Item = Callable[[], Optional[str]]


class ScenarioAbortedError(Exception):
    pass


@dataclass
class RepStats:
    requests: int
    served: int
    rps: Optional[float]
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    errors: int
    wrong: int
    error_samples: List[str] = field(default_factory=list)
    wrong_samples: List[str] = field(default_factory=list)
    error_classes: Dict[str, int] = field(default_factory=dict)
    error_class_samples: Dict[str, str] = field(default_factory=dict)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of an empty sample")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: Sequence[float]) -> float:
    center = median(values)
    return median([abs(v - center) for v in values])


def _keep(samples: List[str], value: str, cap: int) -> None:
    if value not in samples and len(samples) < cap:
        samples.append(value)


class _Outcomes:
    def __init__(self, clean: Callable[[str], str] = scrub) -> None:
        # ^ security every stored text passes clean; the CLI passes PerfClient.sanitize
        self.clean = clean
        self.lock = threading.Lock()
        self.attempts = 0
        self.latencies_ms: List[float] = []
        self.errors = 0
        self.wrong = 0
        self.samples: List[str] = []
        self.wrong_samples: List[str] = []
        self.classes: Dict[str, int] = {}
        self.class_samples: Dict[str, str] = {}
        self.abort: Optional[str] = None

    def record(self, item: Item) -> None:
        started = time.perf_counter()
        error: Optional[str] = None
        kind = ""
        miss: Optional[str] = None
        try:
            miss = item()
        except AbortScenarioError as exc:
            with self.lock:
                self.abort = self.abort or self.clean(str(exc))
            return
        except (RequestFailedError, TransportError) as exc:
            error, kind = self.clean(str(exc)), exc.kind
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self.lock:
            self.attempts += 1
            if error is not None:
                self.errors += 1
                self.classes[kind] = self.classes.get(kind, 0) + 1
                self.class_samples.setdefault(kind, error)
                _keep(self.samples, error, MAX_ERROR_SAMPLES)
                return
            # ^ invariant only a served response is timed; a failure must never look fast
            self.latencies_ms.append(elapsed_ms)
            if miss is not None:
                self.wrong += 1
                _keep(self.wrong_samples, self.clean(miss), MAX_WRONG_SAMPLES)


def _stats(outcomes: _Outcomes, wall_s: float) -> RepStats:
    served = outcomes.latencies_ms
    return RepStats(
        requests=outcomes.attempts,
        served=len(served),
        rps=len(served) / wall_s if served else None,
        p50_ms=percentile(served, 50) if served else None,
        p95_ms=percentile(served, 95) if served else None,
        p99_ms=percentile(served, 99) if served else None,
        errors=outcomes.errors,
        wrong=outcomes.wrong,
        error_samples=outcomes.samples,
        wrong_samples=outcomes.wrong_samples,
        error_classes=outcomes.classes,
        error_class_samples=outcomes.class_samples,
    )


def run_closed_loop(
    items: Sequence[Item], concurrency: int, clean: Callable[[str], str] = scrub
) -> RepStats:
    if not items or concurrency < 1:
        raise ValueError("closed loop needs items and concurrency >= 1")
    total = max(len(items), concurrency * MIN_REQUESTS_PER_WORKER)
    outcomes = _Outcomes(clean)
    next_index = [0]

    def worker() -> None:
        while True:
            with outcomes.lock:
                index = next_index[0]
                if index >= total or outcomes.abort is not None:
                    return
                next_index[0] += 1
            outcomes.record(items[index % len(items)])

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        for future in futures:
            future.result()
    wall_s = time.perf_counter() - started
    if outcomes.abort is not None:
        raise ScenarioAbortedError(outcomes.abort)
    return _stats(outcomes, wall_s)


def _warmup(items: Sequence[Item], clean: Callable[[str], str]) -> _Outcomes:
    outcomes = _Outcomes(clean)
    for item in items:
        outcomes.record(item)
        if outcomes.abort is not None:
            raise ScenarioAbortedError(outcomes.abort)
    return outcomes


def _middle(values: List[Optional[float]], spread: bool) -> Optional[float]:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return mad(present) if spread else median(present)


def _errors_by_class(warm: _Outcomes, reps: List[RepStats]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    sources = [(warm.classes, warm.class_samples)] + [
        (r.error_classes, r.error_class_samples) for r in reps
    ]
    for counts, samples in sources:
        for kind, count in counts.items():
            entry = merged.setdefault(kind, {"count": 0, "sample": samples[kind]})
            entry["count"] += count
    return merged


def not_run(concurrency: int, reason: str) -> Dict[str, Any]:
    result = _empty(concurrency)
    result["aborted"] = reason
    return result


def _empty(concurrency: int) -> Dict[str, Any]:
    return {
        "concurrency": concurrency,
        "queries": 0,
        "warmup_ms": [],
        "warmup_errors": 0,
        "warmup_wrong": 0,
        "warmup_error_samples": [],
        "wrong_samples": [],
        "errors_by_class": {},
        "repetitions": [],
        "median": None,
        "mad": None,
        "aborted": None,
    }


def run_scenario(
    items: Sequence[Item],
    concurrency: int,
    repetitions: int,
    clean: Callable[[str], str] = scrub,
) -> Dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    result = _empty(concurrency)
    result["queries"] = len(items)
    try:
        warm = _warmup(items, clean)
        result.update(
            warmup_ms=warm.latencies_ms,
            warmup_errors=warm.errors,
            warmup_wrong=warm.wrong,
            warmup_error_samples=warm.samples,
        )
        reps = [run_closed_loop(items, concurrency, clean) for _ in range(repetitions)]
    except ScenarioAbortedError as exc:
        result["aborted"] = clean(str(exc))
        return result
    wrong_samples: List[str] = []
    for sample in warm.wrong_samples + [s for r in reps for s in r.wrong_samples]:
        _keep(wrong_samples, sample, MAX_WRONG_SAMPLES)
    result["wrong_samples"] = wrong_samples
    result["errors_by_class"] = _errors_by_class(warm, reps)
    result["repetitions"] = [asdict(r) for r in reps]
    result["median"] = {
        m: _middle([getattr(r, m) for r in reps], False) for m in METRICS
    }
    result["mad"] = {m: _middle([getattr(r, m) for r in reps], True) for m in METRICS}
    return result
