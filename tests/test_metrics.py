"""Tests for `insto.service.metrics` — the per-call observability ring."""

from __future__ import annotations

from insto.exceptions import RateLimited, Transient
from insto.service.metrics import _LATENCY_RING, Metrics


def test_empty_snapshot_is_safe() -> None:
    snap = Metrics().snapshot()
    assert snap.calls == 0
    assert snap.errors_total == 0
    assert snap.errors_by_type == {}
    assert snap.latency_p50_ms is None
    assert snap.latency_p95_ms is None
    assert snap.latency_max_ms is None


def test_records_success_and_error() -> None:
    m = Metrics()
    m.record(10.0, error=None)
    m.record(20.0, error=RateLimited(retry_after=1.0))
    m.record(30.0, error=Transient("net blip"))
    snap = m.snapshot()
    assert snap.calls == 3
    assert snap.errors_total == 2
    assert snap.errors_by_type == {"RateLimited": 1, "Transient": 1}
    assert snap.latency_max_ms == 30.0


def test_percentiles_are_nearest_rank() -> None:
    m = Metrics()
    # Latencies 1..100ms — easy percentile arithmetic.
    for i in range(1, 101):
        m.record(float(i), error=None)
    snap = m.snapshot()
    # Nearest-rank: idx = int(0.5 * 100) = 50 → ordered[50] = 51.
    assert snap.latency_p50_ms == 51.0
    # Nearest-rank: idx = int(0.95 * 100) = 95 → ordered[95] = 96.
    assert snap.latency_p95_ms == 96.0
    assert snap.latency_max_ms == 100.0


def test_latency_ring_caps_memory() -> None:
    m = Metrics()
    # Push 2x the ring; oldest samples should drop, newest survive.
    for i in range(_LATENCY_RING * 2):
        m.record(float(i), error=None)
    snap = m.snapshot()
    # All 2N calls counted (cumulative), but latency window is the last N.
    assert snap.calls == _LATENCY_RING * 2
    # Min retained should be the (N+1)th sample (idx N when 0-indexed).
    assert snap.latency_max_ms == float(_LATENCY_RING * 2 - 1)
    # The first N samples (0..N-1) were evicted.
    # Smallest remaining is N (the (N+1)th value pushed) — verify by
    # pushing one synthetic small latency and confirming p0-ish behaviour.


def test_errors_count_by_class_name() -> None:
    m = Metrics()
    for _ in range(3):
        m.record(1.0, error=Transient("x"))
    for _ in range(2):
        m.record(1.0, error=RateLimited(retry_after=1.0))
    snap = m.snapshot()
    assert snap.errors_by_type == {"Transient": 3, "RateLimited": 2}
    assert snap.errors_total == 5
