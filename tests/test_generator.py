from queue import Queue
import multiprocessing as mp
import struct
import time

import pytest

from viewer.generator import Wave, build_wave, max_frequency, validated_period
from viewer.pdo import AO, Entry, Mapping
from viewer.timing import TimingWindow
from viewer.worker import Worker


def test_full_scale_all_four_outputs():
    mapping = Mapping([Entry(index, 1, 16, i * 16) for i, index in enumerate(AO)], 8, True)
    assert struct.unpack("<4h", mapping.encode([-10, -5, 5, 10])) == (-32768, -16384, 16384, 32767)


@pytest.mark.parametrize("shape, expected", [
    ("sine", [0, 3, 0, -3]),
    ("triangle", [0, 3, 0, -3]),
    ("square", [3, 3, -3, -3]),
])
def test_wave_quadrants(shape, expected):
    wave = build_wave(shape, 3, 1, 10, 100)
    assert [wave.voltage_at(100 + phase * 250_000_000) for phase in range(4)] == pytest.approx(expected)


@pytest.mark.parametrize("period, limit", [(1, 100), (10, 25), (50, 5)])
def test_wave_frequency_bound(period, limit):
    assert max_frequency(period) == limit
    build_wave("sine", 10, limit, period, 0)
    for frequency in (0, limit + 0.01, float("nan"), True):
        with pytest.raises(ValueError):
            build_wave("sine", 1, frequency, period, 0)


@pytest.mark.parametrize("value", [0, 51, 1.5, True, "10"])
def test_bad_period(value):
    with pytest.raises(ValueError):
        validated_period(value)


def test_live_period_change_keeps_phase_and_rejects_unsupported_frequency():
    worker = Worker(Queue(), Queue(), Queue(), mp.Array("q", 2), mp.Event())
    now = time.perf_counter_ns()
    worker.lease[:] = [1, now]
    worker.owner = 1
    worker.values[3] = 0.5
    wave = worker.wave[1] = Wave("triangle", 1, 10, now)
    command = {"id": "test", "action": "set_period", "owner": 1}
    worker.command({**command, "args": {"period_ms": 20}}, now)
    assert worker.events.get()["ok"] and worker.period_ms == 20
    assert worker.wave[1] is wave and worker.values[3] == 0.5
    worker.command({**command, "args": {"period_ms": 50}}, now)
    assert "AO2" in worker.events.get()["error"]
    assert worker.period_ms == 20 and worker.wave[1] is wave


def test_only_recent_exchange_timing_is_counted():
    timing = TimingWindow()
    timing.add([
        {"time_ns": 1, "roundtrip_ms": 100, "interval_ms": 100},
        {"time_ns": 11_000_000_000, "roundtrip_ms": 2, "interval_ms": None},
        {"time_ns": 11_010_000_000, "roundtrip_ms": 1, "interval_ms": 10},
    ])
    stats = timing.summarize()
    assert stats["interval"]["count"] == 1 and stats["update_hz"] == 100
    assert stats["roundtrip"] == {"count": 2, "latest": 1, "median": 1.5, "p95": 2, "max": 2}
