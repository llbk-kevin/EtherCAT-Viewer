import argparse
import json
from pathlib import Path
import statistics
import time

from .hardware import Hardware, adapters

def main():
    parser = argparse.ArgumentParser(description="EtherCAT zero-output soak test")
    parser.add_argument("--seconds", type=int, default=180)
    duration = parser.parse_args().seconds
    if duration < 1:
        parser.error("--seconds must be positive")
    target = next(row for row in adapters()["items"] if row["target"])
    if not target["interface"]:
        raise RuntimeError("Npcap 未枚举「以太网 3」")
    bus = Hardware(target["interface"])
    intervals, roundtrips = [], []
    report = {"target_period_ms": 10, "duration_s": duration, "cycles": 0, "wkc_errors": 0}
    try:
        bus.configure()
        report.update(devices=bus.devices, mapping=bus.mapping, expected_wkc=bus.expected,
                      watchdog_ms=bus.watchdog)
        start = time.perf_counter_ns()
        deadline, previous = start, None
        while time.perf_counter_ns() - start < duration * 1_000_000_000:
            time.sleep(max(0, (deadline - time.perf_counter_ns()) / 1e9))
            now = time.perf_counter_ns()
            if previous is not None:
                intervals.append((now - previous) / 1e6)
            previous = now
            counter = bus.exchange([0.0] * 4)
            roundtrips.append((time.perf_counter_ns() - now) / 1e6)
            report["cycles"] += 1
            if counter != bus.expected:
                report["wkc_errors"] += 1
                raise RuntimeError(f"WKC 异常：{counter}/{bus.expected}")
            if report["cycles"] % 10 == 0 and not bus.check_op():
                raise RuntimeError(f"从站退出 OP：{bus.devices}")
            deadline = max(deadline + 10_000_000, now + 1_000_000)
        report["elapsed_s"] = (time.perf_counter_ns() - start) / 1e9
        report["passed"] = True
    except Exception as exc:
        report.update(passed=False, error=str(exc))
    finally:
        report["zero_ack"] = bus.close()
        for key, series in (("interval_ms", intervals), ("roundtrip_ms", roundtrips)):
            if len(series) > 1:
                report[key] = dict(min=min(series), median=statistics.median(series),
                                   p95=statistics.quantiles(series, n=100)[94], max=max(series))
        folder = Path(__file__).resolve().parent.parent / "logs"
        folder.mkdir(exist_ok=True)
        (folder / "zero-soak.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "mapping"}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
