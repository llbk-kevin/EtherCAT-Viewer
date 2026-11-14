from queue import Empty, Full
import time

from .generator import DEFAULT_PERIOD_MS, build_wave, max_frequency, validated_period
from .hardware import Hardware, adapters
from .pdo import raw_voltage, validated_voltage, volts

LEASE_NS = 2_000_000_000
PUBLISH_NS = 50_000_000


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
        self.connection = "DISCONNECTED"
        self.reason = self.last_error = ""
        self.zero = "等待连接"
        self.seq = self.session = self.owner = 0
        self.watchdog = self.wkc = self.expected = None
        self.devices, self.mapping = [], {}
        self.values = [0.0] * 4
        self.wave = {}
        self.pending_acks = []
        self.good_since = self.next_state_check = 0
        self.errors = self.dropped = self.missed = self.lost_results = 0
        self.last_exchange = None
        self.period_ms = DEFAULT_PERIOD_MS
        self.period_revision = 0

    @property
    def period_ns(self):
        return self.period_ms * 1_000_000

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
            self.devices = bus.devices
        self.expected = self.wkc = self.last_exchange = None
        self.good_since = 0

    def status(self):
        return dict(mode="LIVE", connection=self.connection, armed=bool(self.owner),
                    reason=self.reason, last_error=self.last_error, zero=self.zero,
                    devices=self.devices, mapping=self.mapping, wkc=self.wkc,
                    expected_wkc=self.expected, watchdog_ms=self.watchdog, errors=self.errors,
                    dropped=self.dropped, missed=self.missed, lost_results=self.lost_results,
                    period_ms=self.period_ms, session=self.session,
                    ready=self.connection == "OP" and self.good_since != 0
                    and time.perf_counter_ns() - self.good_since >= 1_000_000_000,
                    ao=list(self.values), wave=list(self.wave))

    def lease_valid(self, generation, now):
        with self.lease.get_lock():
            owner, heartbeat = self.lease[:]
        return generation != 0 and generation == owner and now - heartbeat < LEASE_NS

    def ack(self, ident, result=None, error=None):
        payload = dict(kind="result", id=ident, ok=error is None, result=result, error=error)
        if not emit(self.events, payload):
            self.lost_results += 1

    def connect(self, interface):
        if self.bus:
            raise ValueError("请先断开")
        self.lock()
        self.connection = "CONNECTING"
        self.devices, self.mapping = [], {}
        self.watchdog, self.zero = None, "输出状态未知"
        try:
            self.bus = Hardware(interface)
            self.bus.configure()
            self.devices, self.mapping = self.bus.devices, self.bus.mapping
            self.watchdog, self.expected = self.bus.watchdog, self.bus.expected
            self.session += 1
            self.errors = self.dropped = self.missed = self.lost_results = 0
            self.last_exchange = None
            self.next_state_check = 0
            self.connection = "OP"
        except Exception as exc:
            if self.bus:
                self.mapping = self.bus.mapping
            self.close_bus()
            self.errors += 1
            self.connection, self.reason, self.last_error = "FAULT", str(exc), str(exc)
            raise

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
                self.connect(args["interface"])
            elif action == "disconnect":
                self.lock()
                self.close_bus()
                self.connection = "DISCONNECTED"
            elif action == "set_period":
                period = validated_period(args.get("period_ms"))
                limit = max_frequency(period)
                for channel, wave in self.wave.items():
                    if wave.frequency > limit:
                        raise ValueError(f"AO{channel + 1} 正在运行 {wave.frequency:g} Hz，新周期上限为 {limit:g} Hz")
                if period != self.period_ms:
                    self.period_ms = period
                    self.period_revision += 1
            elif action == "zero":
                self.lock()
                if not self.bus:
                    raise ValueError("当前掉线，无法确认零输出")
                self.pending_acks.append(ident)
                return
            elif action == "arm":
                if not self.status()["ready"]:
                    raise ValueError("等待通信连续正常 1 秒")
                if not self.owner:
                    self.lock()
                    self.owner = generation
                elif self.owner != generation:
                    raise ValueError("上一控制会话尚未释放")
            elif action in ("set_output", "wave", "stop_wave"):
                if self.owner != generation or not self.status()["ready"]:
                    raise ValueError("请先解锁输出")
                channel = args.get("channel")
                if type(channel) is not int or channel not in (1, 2, 3, 4):
                    raise ValueError("输出通道必须为 AO1–AO4")
                index = channel - 1
                if action == "wave":
                    self.wave[index] = build_wave(args.get("shape"), args.get("peak"),
                                                  args.get("frequency"), self.period_ms, now)
                else:
                    voltage = 0.0 if action == "stop_wave" else validated_voltage(args.get("voltage"))
                    self.wave.pop(index, None)
                    self.values[index] = voltage
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
        raw, flags, valid = [None] * 4, [None] * 4, [False] * 4
        confirmed = [None] * 4
        error = None
        interval = roundtrip = None
        if self.bus:
            for channel, wave in self.wave.items():
                self.values[channel] = wave.voltage_at(now)
            try:
                self.wkc = self.bus.exchange(self.values)
                start_ns = self.bus.last_send_ns
                if self.last_exchange is not None:
                    interval = (start_ns - self.last_exchange) / 1e6
                self.last_exchange = start_ns
                roundtrip = (self.bus.last_receive_ns - start_ns) / 1e6
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
                confirmed = [volts(raw_voltage(value)) for value in self.values]
                self.zero = "有非零指令" if any(confirmed) else "零值已收到 WKC 确认"
                self.good_since = self.good_since or now
            except Exception as exc:
                error = str(exc)
                self.last_error = error
                self.errors += 1
                actual_wkc, expected = self.wkc, self.expected
                self.lock(error)
                self.close_bus()
                self.wkc, self.expected = actual_wkc, expected
                self.connection = "FAULT"
        for ident in self.pending_acks:
            self.ack(ident, self.status(), error=error)
        self.pending_acks.clear()
        self.seq += 1
        return dict(seq=self.seq, time_ns=now, mode="LIVE", session=self.session,
                    raw=raw, flags=flags, valid=valid, ao_valid=confirmed[0] is not None,
                    ai=[volts(x) if ok else None for x, ok in zip(raw, valid)],
                    ao=confirmed, wkc=self.wkc, expected_wkc=self.expected,
                    interval_ms=interval, roundtrip_ms=roundtrip, period_ms=self.period_ms)

    def run(self):
        deadline = time.perf_counter_ns()
        publish_at = deadline
        batch = []
        while not self.stop.is_set():
            revision = self.period_revision
            session = self.session
            try:
                command = self.commands.get_nowait()
            except Empty:
                command = None
            if command:
                self.command(command, time.perf_counter_ns())
            online = self.bus is not None
            now = time.perf_counter_ns()
            if online:
                batch.append(self.cycle(now))
            if not online and self.owner:
                self.lock()
            if now >= publish_at or (online and self.bus is None):
                if not emit(self.telemetry, dict(status=self.status(), samples=batch)):
                    self.dropped += len(batch)
                batch = []
                publish_at = now + PUBLISH_NS
            step = self.period_ns if self.bus else PUBLISH_NS
            if revision != self.period_revision or session != self.session or not online:
                deadline = now
            deadline += step
            end = time.perf_counter_ns()
            if deadline <= end:
                # Skip elapsed slots; never emit catch-up bursts.
                skipped = (end - deadline) // step + 1
                if self.bus:
                    self.missed += skipped
                deadline += skipped * step
            time.sleep(max(0, (deadline - time.perf_counter_ns()) / 1e9))


def run_worker(*args):
    worker = Worker(*args)
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.lock()
        worker.close_bus()
