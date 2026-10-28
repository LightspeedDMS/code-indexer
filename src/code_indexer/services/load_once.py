import threading
from typing import Callable, Dict, TypeVar

K = TypeVar("K")
V = TypeVar("V")


def load_once(
    cache: Dict[K, V], lock: threading.Lock, key: K, loader: Callable[[K], V]
) -> V:
    try:
        return cache[key]
    except KeyError:
        pass
    with lock:
        try:
            return cache[key]
        except KeyError:
            pass
        # ^ invariant cache written only after loader returns; a raising loader is retried next call
        value = loader(key)
        cache[key] = value
        return value
