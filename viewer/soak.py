import argparse
import json
import statistics
import time

from .hardware import Hardware, adapter_guid
from .paths import data_root
from .settings import Settings

def main():
    parser = argparse.ArgumentParser(description="EtherCAT 零输出长测")
    parser.add_argument("--adapter-guid", required=True, help="所选物理有线网卡的 InterfaceGuid")
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    duration = args.seconds
    if duration < 1:
        parser.error("--seconds 必须为正整数")
    try:
        guid = adapter_guid(args.adapter_guid)
    except ValueError as exc:
        parser.error(str(exc))
    bus = Hardware(fr"\Device\NPF_{{{guid}}}")
    intervals, roundtrips = [], []
    report = {"target_period_ms": 10, "duration_s": duration, "cycles": 0, "wkc_errors": 0}
    try:
        configuration = Settings().data
        configuration["hardware"]["period_ms"] = 10
        bus.configure(configuration)
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
            counter = bus.exchange(bus.zero_values)
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
        folder = data_root() / "logs"
        folder.mkdir(exist_ok=True)
        (folder / "zero-soak.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "mapping"}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
