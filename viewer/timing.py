from collections import deque
from statistics import median
import math

WINDOW_NS = 10_000_000_000


def distribution(values):
    if not values:
        return {"count": 0, "latest": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {"count": len(values), "latest": values[-1], "median": median(ordered),
            "p95": ordered[math.ceil(len(ordered) * 0.95) - 1], "max": ordered[-1]}


class TimingWindow:
    def __init__(self):
        self.samples = deque()

    def add(self, samples):
        for sample in samples:
            if sample["roundtrip_ms"] is None:
                continue
            self.samples.append(sample)
            cutoff = sample["time_ns"] - WINDOW_NS
            while self.samples[0]["time_ns"] < cutoff:
                self.samples.popleft()

    def summarize(self):
        intervals = [row["interval_ms"] for row in self.samples if row["interval_ms"] is not None]
        roundtrips = [row["roundtrip_ms"] for row in self.samples]
        total = sum(intervals)
        return {"interval": distribution(intervals), "roundtrip": distribution(roundtrips),
                "update_hz": 1000 * len(intervals) / total if total > 0 else None}
