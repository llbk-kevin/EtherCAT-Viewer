from queue import Empty, Full
import time

from .console import ConsoleStop, console_message, log_stop
from .generator import DEFAULT_PERIOD_MS, TableWave, build_table, build_wave, max_frequency, validated_period
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
    def __init__(self, commands, events, telemetry, lease, stop, source=None, emergency=None):
        self.commands, self.events, self.telemetry = commands, events, telemetry
        self.lease, self.stop = lease, stop
        self.source, self.emergency = source, emergency
        self.control_mode = "WEB"
        self.bridge_cards, self.bridge_generation = {}, 0
        self.channels = {"ai": [], "ao": []}
        self.source_drops = 0
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
        if reason:
            for ident in self.pending_acks:
                self.ack(ident, error=reason)
            self.pending_acks.clear()
        self.owner = 0
        self.values = [0.0] * len(self.values)
        self.wave.clear()
        self.bridge_cards = {}
        self.bridge_generation = 0
        self.control_mode = "WEB"
        self.reason = reason

    def close_bus(self):
        confirmed = None
        if self.bus:
            bus, self.bus = self.bus, None
            try:
                confirmed = bus.close()
                self.zero = "零值已收到 WKC 确认" if confirmed else "输出状态未知"
            except Exception:
                confirmed = False
                self.zero = "输出状态未知"
            self.devices = bus.devices
        self.expected = self.wkc = self.last_exchange = None
        self.good_since = 0
        return confirmed

    def status(self):
        return dict(mode="LIVE", connection=self.connection, armed=bool(self.owner) or any(c["armed"] for c in self.bridge_cards.values()),
                    control_mode=self.control_mode, bridge_cards={k: dict(v) for k, v in self.bridge_cards.items()},
                    channels=self.channels, fingerprint=self.mapping.get("fingerprint", ""),
                    source_drops=self.source_drops,
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
            self.channels = self.bus.channels
            self.values = [0.0] * len(self.channels["ao"])
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
            if action.startswith("bridge_"):
                return self.bridge_command(ident, action, args, generation, now)
            if self.control_mode == "TCP" and action not in ("zero", "disconnect"):
                raise ValueError("TCP 已接管，请先清零并收回控制权")
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
                if type(channel) is not int or not 1 <= channel <= len(self.values):
                    raise ValueError("输出通道编号无效")
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

    def zero_channels(self, channels):
        for index in channels:
            self.values[index] = 0.0
            self.wave.pop(index, None)

    def bridge_command(self, ident, action, args, generation, now):
        if action == "bridge_setup":
            if self.control_mode != "WEB" or not self.status()["ready"]:
                raise ValueError("请先停止发布，并等待实机稳定")
            cards, used = {}, set()
            for card in args["cards"]:
                outputs = card["outputs"]
                if (not isinstance(card["id"], str) or card["id"] in cards or
                        len(outputs) != len(set(outputs)) or
                        any(type(i) is not int or not 0 <= i < len(self.values) or i in used for i in outputs)):
                    raise ValueError("虚拟卡输出占用无效或重复")
                used.update(outputs)
                cards[card["id"]] = dict(outputs=outputs, token=None, armed=False)
            self.lock()
            self.bridge_cards = cards
            self.control_mode, self.bridge_generation = "TCP", generation
        else:
            if self.control_mode != "TCP" or self.bridge_generation != generation:
                raise ValueError("TCP 控制代次已失效")
            card = self.bridge_cards.get(args.get("card"))
            if card is None:
                raise ValueError("虚拟卡未登记")
            if action == "bridge_authorize":
                if card["token"]:
                    raise ValueError("请先断开该卡的控制客户端")
                self.zero_channels(card["outputs"])
                card["armed"] = True
            elif action == "bridge_claim":
                if not card["armed"] or card["token"] is not None:
                    raise ValueError("请在网页授权该卡接管，或释放现有控制连接")
                card["token"] = args["token"]
            else:
                if not card["token"] or card["token"] != args.get("token"):
                    raise ValueError("该连接没有虚拟卡控制权")
                if action == "bridge_release":
                    self.zero_channels(card["outputs"])
                    card.update(token=None, armed=False)
                elif action in ("bridge_output", "bridge_table"):
                    if not self.status()["ready"]:
                        raise ValueError("实机未就绪")
                    changes = args["changes"]
                    prepared = []
                    for item in changes:
                        index = item["index"]
                        if type(index) is not int or index not in card["outputs"]:
                            raise ValueError("超出该虚拟卡的输出范围")
                        value = (build_table(item["table"], item["rate"], self.period_ms, now)
                                 if action == "bridge_table" else validated_voltage(item["voltage"]))
                        prepared.append((index, value))
                    for index, value in prepared:
                        if action == "bridge_table":
                            self.wave[index] = value
                        else:
                            self.wave.pop(index, None)
                            self.values[index] = value
                elif action == "bridge_rate":
                    build_table([0.0], args["rate"], self.period_ms, now)
                    prepared = {}
                    for index in card["outputs"]:
                        wave = self.wave.get(index)
                        if isinstance(wave, TableWave):
                            checked = build_table(wave.table, args["rate"], self.period_ms, now)
                            prepared[index] = wave.reclock(checked.rate, now)
                    self.wave.update(prepared)
                else:
                    raise ValueError("未知桥接命令")
        self.pending_acks.append(ident)

    def cycle(self, now):
        if self.owner and not self.lease_valid(self.owner, now):
            self.lock("控制会话失联，已撤销输出")
        if self.control_mode == "TCP" and not self.lease_valid(self.bridge_generation, now):
            self.lock("桥接服务租约失效，已撤销全部输出")
        count = len(self.channels["ai"]) if self.channels["ai"] or self.mapping else 4
        raw, flags, valid = [None] * count, [None] * count, [False] * count
        confirmed = [None] * len(self.values)
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
                    raw=raw, flags=flags, valid=valid, ao_valid=self.bus is not None and error is None,
                    host_send_ns=getattr(self.bus, "last_send_ns", None),
                    host_return_ns=getattr(self.bus, "last_receive_ns", None),
                    ai=[volts(x) if ok else None for x, ok in zip(raw, valid)],
                    ao=confirmed, wkc=self.wkc, expected_wkc=self.expected,
                    interval_ms=interval, roundtrip_ms=roundtrip, period_ms=self.period_ms)

    def run(self):
        deadline = time.perf_counter_ns()
        publish_at = deadline
        batch = []
        while not self.stop.is_set():
            if self.emergency is not None and self.emergency.is_set():
                self.lock("优先清零已执行")
                self.emergency.clear()
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
                if self.source is not None and not emit(self.source, dict(status=self.status(), samples=batch)):
                    self.source_drops += len(batch)
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


def run_worker(commands, events, telemetry, lease, stop, cleanup_done, source=None, emergency=None):
    worker = Worker(commands, events, telemetry, lease, stop, source, emergency)
    with ConsoleStop(lambda source: worker.stop.set()):
        try:
            worker.run()
        except KeyboardInterrupt:
            pass
        finally:
            worker.lock()
            confirmed = worker.close_bus()
            zero = "not_connected" if confirmed is None else "confirmed" if confirmed else "unknown"
            log_stop("worker_stop", zero=zero)
            cleanup_done.set()
            if confirmed is not None:
                console_message(f"EtherCAT 停止结果：{worker.zero}")
