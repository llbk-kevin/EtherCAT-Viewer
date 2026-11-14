import math
from queue import Empty, Full
import time

from .hardware import Hardware, adapters
from .pdo import raw_voltage, validated_voltage, volts

PERIOD_NS = 10_000_000
LEASE_NS = 2_000_000_000

def emit(queue, message):
    try:
        queue.put_nowait(message)
        return True
    except Full:
        return False

class Worker:
    def __init__(self, commands, events, telemetry, lease, stop):
        self.commands, self.events, self.telemetry = commands, events, telemetry
        self.lease, self.stop = lease, stop
        self.bus = None
        self.mode = "SIMULATION"
        self.connection = "RUNNING"
        self.reason = ""
        self.zero = "模拟零值"
        self.seq = 0
        self.owner = 0
        self.watchdog = None
        self.wkc = self.expected = None
        self.devices, self.mapping = [], {}
        self.values = [0.0] * 4
        self.wave = {}
        self.pending_acks = []
        self.good_since = 0
        self.errors = self.dropped = self.max_interval = self.max_roundtrip = 0
        self.next_state_check = 0
        self.max_roundtrip = 0

    def lock(self, reason=""):
        self.owner = 0
        self.values = [0.0] * 4
        self.wave.clear()
        self.reason = reason

    def close_bus(self):
        if self.bus:
            bus, self.bus = self.bus, None
            try:
                self.zero = "零值已收到 WKC 确认" if bus.close() else "输出状态未知"
            except Exception:
                self.zero = "输出状态未知"
        self.expected = self.wkc = None
        self.good_since = 0

    def status(self):
        return dict(mode=self.mode, connection=self.connection, armed=bool(self.owner), reason=self.reason,
                    zero=self.zero, devices=self.devices, mapping=self.mapping, wkc=self.wkc,
                    expected_wkc=self.expected, watchdog_ms=self.watchdog, errors=self.errors,
                    dropped=self.dropped, max_interval_ms=self.max_interval / 1e6,
                    max_roundtrip_ms=self.max_roundtrip / 1e6,
                    ready=self.connection in ("RUNNING", "OP") and self.good_since != 0
                    and time.perf_counter_ns() - self.good_since >= 1_000_000_000,
                    ao=list(self.values), wave=list(self.wave))

    def lease_valid(self, generation, now):
        with self.lease.get_lock():
            owner, heartbeat = self.lease[:]
        return generation != 0 and generation == owner and now - heartbeat < LEASE_NS

    def ack(self, ident, result=None, error=None):
        payload = dict(kind="result", id=ident, ok=error is None, result=result, error=error)
        if not emit(self.events, payload):
            self.dropped += 1

    def command(self, command, now):
        ident, action, args, generation = (command[key] for key in ("id", "action", "args", "owner"))
        try:
            if action == "adapters":
                if self.bus:
                    raise ValueError("请断开实时通信后刷新网卡")
                return self.ack(ident, adapters())
            if not self.lease_valid(generation, now):
                raise ValueError("控制会话已失效")
            if action == "connect":
                if self.bus:
                    raise ValueError("请先断开")
                self.lock()
                self.mode, self.connection = "LIVE", "CONNECTING"
                self.devices, self.mapping = [], {}
                self.watchdog, self.zero = None, "输出状态未知"
                self.errors = self.dropped = self.max_interval = self.max_roundtrip = 0
                try:
                    self.bus = Hardware(args["interface"])
                    self.bus.configure()
                    self.devices, self.mapping = self.bus.devices, self.bus.mapping
                    self.watchdog, self.expected = self.bus.watchdog, self.bus.expected
                    self.connection = "OP"
                except Exception as exc:
                    if self.bus:
                        self.devices, self.mapping = self.bus.devices, self.bus.mapping
                    self.close_bus()
                    self.connection, self.reason = "FAULT", str(exc)
                    raise
            elif action in ("disconnect", "simulate"):
                self.lock()
                self.close_bus()
                self.connection = "DISCONNECTED"
                if action == "simulate":
                    self.mode, self.connection = "SIMULATION", "RUNNING"
                    self.devices, self.mapping = [], {}
                    self.errors = self.dropped = self.max_interval = self.max_roundtrip = 0
                    self.watchdog, self.zero = None, "模拟零值"
            elif action == "zero":
                self.lock()
                if self.connection in ("OP", "RUNNING"):
                    self.pending_acks.append(ident)
                    return
            elif action == "arm":
                if not self.status()["ready"]:
                    raise ValueError("等待通信连续正常 1 秒")
                self.lock()
                self.owner = generation
            elif action in ("set_output", "wave"):
                if self.owner != generation or not self.status()["ready"]:
                    raise ValueError("请先解锁输出")
                channel = args.get("channel")
                if type(channel) is not int or channel not in (1, 4):
                    raise ValueError("仅支持 AO1 / AO4")
                value = validated_voltage(args.get("voltage"))
                index = channel - 1
                if action == "set_output":
                    self.wave.pop(index, None)
                    self.values[index] = value
                else:
                    self.wave[index] = (abs(value), now)
                self.pending_acks.append(ident)
                return
            else:
                raise ValueError("未知命令")
            self.ack(ident, self.status())
        except Exception as exc:
            self.ack(ident, error=str(exc))

    def cycle(self, now):
        if self.owner and not self.lease_valid(self.owner, now):
            self.lock("控制会话失联，已撤销输出")
        for channel, (amplitude, start) in self.wave.items():
            self.values[channel] = amplitude * math.sin(2 * math.pi * 0.2 * (now - start) / 1e9)
        raw, flags, valid = [None] * 4, [None] * 4, [False] * 4
        error = None
        if self.bus:
            try:
                start_ns = time.perf_counter_ns()
                self.wkc = self.bus.exchange(self.values)
                self.max_roundtrip = max(self.max_roundtrip, time.perf_counter_ns() - start_ns)
                if self.wkc != self.expected:
                    raise RuntimeError(f"WKC 异常：{self.wkc}/{self.expected}")
                if now >= self.next_state_check:
                    self.next_state_check = now + 100_000_000
                    if not self.bus.check_op():
                        raise RuntimeError("从站退出 OP")
                    self.devices = self.bus.devices
                raw, flags = self.bus.read()
                valid = [not any((f or {}).get(key) for key in
                         ("underrange", "overrange", "error", "sync_error", "invalid"))
                         and not ((f or {}).get("status", 0) & 0x6043) for f in flags]
                self.zero = ("有非零指令" if any(self.values)
                             else "零值已收到 WKC 确认")
                self.good_since = self.good_since or now
            except Exception as exc:
                error = str(exc)
                self.errors += 1
                actual_wkc = self.wkc
                expected = self.expected
                bus = self.bus
                self.lock(error)
                self.close_bus()
                self.devices = bus.devices
                self.wkc, self.expected = actual_wkc, expected
                self.connection = "FAULT"
        elif self.mode == "SIMULATION" and self.connection == "RUNNING":
            raw = [raw_voltage(value) for value in self.values]
            raw[1] = round(75 * math.sin(now / 1e9 * 3))
            raw[2] = round(95 * math.cos(now / 1e9 * 4))
            valid = [True] * 4
            self.good_since = self.good_since or now
            self.zero = "有模拟输出" if any(self.values) else "模拟零值"
        for ident in self.pending_acks:
            self.ack(ident, self.status(), error=error)
        self.pending_acks.clear()
        self.seq += 1
        return dict(seq=self.seq, time_ns=now, mode=self.mode, raw=raw, flags=flags, valid=valid,
                    ai=[volts(x) if ok else None for x, ok in zip(raw, valid)],
                    ao=list(self.values), wkc=self.wkc, expected_wkc=self.expected)

    def run(self):
        deadline = time.perf_counter_ns()
        last = 0
        batch = []
        try:
            while not self.stop.is_set():
                try:
                    command = self.commands.get_nowait()
                except Empty:
                    command = None
                if command:
                    self.command(command, time.perf_counter_ns())
                    if command["action"] in ("connect", "simulate", "disconnect"):
                        batch.clear()
                        last = 0
                now = time.perf_counter_ns()
                if self.bus and last:
                    self.max_interval = max(self.max_interval, now - last)
                last = now
                batch.append(self.cycle(now))
                if len(batch) == 5:
                    if not emit(self.telemetry, dict(kind="samples", status=self.status(), samples=batch)):
                        self.dropped += len(batch)
                    batch = []
                deadline += PERIOD_NS
                if deadline <= now:
                    deadline = now + PERIOD_NS
                time.sleep(max(0, (deadline - time.perf_counter_ns()) / 1e9))
        finally:
            self.lock()
            self.close_bus()

def run_worker(*args):
    try:
        Worker(*args).run()
    except KeyboardInterrupt:
        pass
