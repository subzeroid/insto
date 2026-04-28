"""Per-session backend call metrics for `/health`.

Each backend instance owns one ``Metrics``; every SDK call goes through
the backend's ``_call`` boundary, which records latency + error type.
``/health`` renders the result so an operator can answer "is the
backend slow / erroring more than usual right now?" without leaving
the REPL.

Memory budget: latencies are stored in a fixed-size ring (1000 slots,
~8 KB at 8 bytes per double). Beyond that the oldest sample drops —
percentiles stay representative of the recent past, which is what
matters for "is it acting up *now*". Cumulative counters (calls,
errors) keep going forever.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

_LATENCY_RING: int = 1000


@dataclass(frozen=True)
class MetricsSnapshot:
    """Immutable view rendered by ``/health``. JSON-friendly via dataclasses.asdict."""

    calls: int
    errors_total: int
    errors_by_type: dict[str, int]
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_max_ms: float | None


class Metrics:
    def __init__(self) -> None:
        self._calls: int = 0
        self._latencies_ms: deque[float] = deque(maxlen=_LATENCY_RING)
        self._errors: Counter[str] = Counter()

    def record(self, latency_ms: float, error: BaseException | None) -> None:
        """Append one observation. Cheap; safe to call on hot paths."""

        self._calls += 1
        self._latencies_ms.append(latency_ms)
        if error is not None:
            self._errors[type(error).__name__] += 1

    def snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            calls=self._calls,
            errors_total=sum(self._errors.values()),
            errors_by_type=dict(self._errors),
            latency_p50_ms=_percentile(self._latencies_ms, 0.50),
            latency_p95_ms=_percentile(self._latencies_ms, 0.95),
            latency_max_ms=max(self._latencies_ms) if self._latencies_ms else None,
        )


def _percentile(samples: deque[float], q: float) -> float | None:
    """Nearest-rank percentile. Returns ``None`` for an empty sample.

    Nearest-rank is good enough for an operator dashboard — we don't
    need linear interpolation correctness. For 1000 samples the
    quantization error is at most 0.1%.
    """
    if not samples:
        return None
    ordered = sorted(samples)
    # ceil(q * n) — 1, clamped into [0, n-1]
    idx = max(0, min(len(ordered) - 1, int(q * len(ordered))))
    return ordered[idx]
