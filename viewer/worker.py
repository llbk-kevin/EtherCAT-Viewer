from dataclasses import replace
from queue import Empty, Full
import time

from .calibration import calibrated, drive_voltage, engineering_value
from .console import ConsoleStop, console_message, log_stop
from .generator import DEFAULT_PERIOD_MS, TableWave, build_table, build_wave, max_frequency, validated_period
from .hardware import Hardware, adapters
from .pdo import raw_voltage, validated_voltage, volts
from .settings import defaults, validate

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
        self.lease, self.stop, self.source, self.emergency = lease, stop, source, emergency
        self.settings = defaults()
        self.control_mode = "WEB"
        self.bridge_cards, self.bridge_generation = {}, 0
        self.web_channels = set()
        self.channels = {"ai": [], "ao": []}
        self.source_drops = 0
        self.bus = None
        self.connection, self.health = "DISCONNECTED", "NORMAL"
        self.reason = self.last_error = ""
        self.zero = "等待连接"
        self.seq = self.session = self.owner = 0
        self.watchdog = self.wkc = self.expected = None
        self.devices, self.mapping = [], {}
        self.values = [0.0] * 4
        self.confirmed = list(self.values)
        self.wave = {}
        self.pending_acks = []
        self.pending_snapshots = {}
        self.protected_zero = set()
        self.good_since = self.next_state_check = 0
        self.next_diagnostic = 0
        self.diagnostic = {}
        self.errors = self.dropped = self.missed = self.lost_results = 0
        self.connection_errors = 0
        self.last_exchange = None
        self.period_ms = DEFAULT_PERIOD_MS
        self.period_revision = 0
        self.bad_count = self.bad_since = 0
        self.last_good_ns = 0
        self.interface = ""
        self.desired = False
        self.reconnect_at = self.retry_index = 0
        self.web_lost_at = None

    @property
    def period_ns(self):
        return self.period_ms * 1_000_000

    def event(self, message, category="runtime", severity="info", **details):
        emit(self.events, dict(kind="diagnostic", category=category, message=message, severity=severity,
                               details=dict(session=self.session, wkc=self.wkc, expected_wkc=self.expected,
                                            devices=self.devices, diagnostic=self.diagnostic, **details)))

    def profile(self, kind, index):
        rows = self.channels[kind]
        return self.settings["channels"].get(rows[index]["id"], {}) if index < len(rows) else {}

    def check_drive(self, index, value):
        return drive_voltage(value, self.profile("ao", index))

    def lock(self, reason=""):
        if reason:
            for ident in self.pending_acks:
                self.ack(ident, error=reason)
            self.pending_acks.clear()
        self.owner = 0
        self.web_channels.clear()
        self.values = [0.0] * len(self.values)
        self.protected_zero = set(range(len(self.values)))
        self.pending_snapshots.clear()
        self.wave.clear()
        self.bridge_cards = {}
        self.bridge_generation = 0
        self.control_mode = "WEB"
        self.reason = reason

    def close_bus(self, safe=True):
        confirmed = None
        if self.bus:
            bus, self.bus = self.bus, None
            try:
                confirmed = bus.close() if safe else bus.close(safe=False)
                if safe:
                    self.zero = "零目标已收到 WKC 确认" if confirmed else "输出状态无法确认"
            except Exception:
                confirmed = False
                self.zero = "输出状态无法确认"
            self.devices = bus.devices
        self.last_exchange = None
        self.good_since = 0
        self.dc_error_ns = None
        return confirmed

    def owners(self):
        result = ["web" if i in self.web_channels else "locked" for i in range(len(self.values))]
        for ident, card in self.bridge_cards.items():
            if card["armed"]:
                for i in card["outputs"]:
                    result[i] = ident
        return result

    def status(self):
        return dict(mode="LIVE", connection=self.connection, health=self.health,
                    armed=bool(self.owner) or any(c["armed"] for c in self.bridge_cards.values()),
                    control_mode=self.control_mode, bridge_cards={k: dict(v, token=bool(v["token"])) for k,v in self.bridge_cards.items()},
                    channels=self.channels, fingerprint=self.mapping.get("fingerprint", ""),
                    output_owners=self.owners(), web_channels=sorted(self.web_channels), interface=self.interface,
                    source_drops=self.source_drops, reason=self.reason, last_error=self.last_error, zero=self.zero,
                    devices=self.devices, mapping=self.mapping, wkc=self.wkc, expected_wkc=self.expected,
                    watchdog_ms=self.watchdog, errors=self.errors, connection_errors=self.connection_errors, dropped=self.dropped, missed=self.missed,
                    lost_results=self.lost_results, period_ms=self.period_ms, session=self.session,
                    ready=self.connection == "OP" and self.health == "NORMAL" and self.good_since != 0
                    and time.perf_counter_ns()-self.good_since >= 1_000_000_000,
                    ao=list(self.confirmed), targets=list(self.values), wave=list(self.wave),
                    wave_tasks={str(i): dict(shape=getattr(w,"shape","table"), frequency=w.frequency,
                                            peak=getattr(w,"peak",None)) for i,w in self.wave.items()},
                    bad_count=self.bad_count, bad_duration_ms=(time.perf_counter_ns()-self.bad_since)/1e6 if self.bad_since else 0,
                    reconnect_at_ns=self.reconnect_at, desired_connected=self.desired,
                    diagnostic=self.diagnostic, hardware_details=getattr(self.bus,"hardware_details",[]) if self.bus else [],
                    dc_error_ns=getattr(self, "dc_error_ns", None), diagnostic_ms=getattr(self,"diagnostic_ms",None))

    def lease_valid(self, generation, now):
        with self.lease.get_lock():
            owner, heartbeat = self.lease[:]
        return generation != 0 and generation == owner and now-heartbeat < LEASE_NS

    def ack(self, ident, result=None, error=None):
        if not emit(self.events, dict(kind="result", id=ident, ok=error is None, result=result, error=error)):
            self.lost_results += 1

    def configure(self, data):
        if self.bus:
            raise ValueError("请先停止任务并断开接口，再应用硬件、校准和故障配置")
        self.settings = validate(data)
        self.period_ms = self.settings["hardware"]["period_ms"]
        self.period_revision += 1

    def connect(self, interface):
        if self.bus:
            raise ValueError("请先断开")
        self.interface, self.desired = interface, True
        self.lock()
        self.connection, self.health = "CONNECTING", "RECOVERING"
        self.devices, self.mapping = [], {}
        self.watchdog, self.zero = None, "输出状态无法确认"
        self.bad_count = self.bad_since = 0
        try:
            self.bus = Hardware(interface)
            expected_topology = self.settings["topology"]["fingerprint"]
            if expected_topology and expected_topology != self.bus.fingerprint and (self.settings["startup"]["unattended"] or any(p.get("calibration",{}).get("enabled") for p in self.settings["channels"].values())):
                raise ValueError("拓扑与保存配置不符，已禁止自动映射及校准，请人工核对")
            self.bus.configure(self.settings)
            self.devices, self.mapping, self.channels = self.bus.devices, self.bus.mapping, self.bus.channels
            self.values = [0.0] * len(self.channels["ao"])
            self.confirmed = list(self.values)
            self.watchdog, self.expected = self.bus.watchdog, self.bus.expected
            self.session += 1
            self.last_exchange = None
            self.dc_error_ns = None
            self.last_good_ns = 0
            self.good_since = 0
            self.next_state_check = self.next_diagnostic = 0
            self.retry_index = self.reconnect_at = 0
            self.connection, self.health = "OP", "NORMAL"
            self.event("实机已连接，等待零输出稳定", "connection")
        except Exception as exc:
            if self.bus:
                self.mapping = self.bus.mapping
            self.close_bus()
            self.connection_errors += 1
            self.connection, self.health, self.reason, self.last_error = "FAULT", "FAULT", str(exc), str(exc)
            self.schedule_reconnect()
            self.event(f"连接失败：{exc}", "connection", "error")
            raise

    def schedule_reconnect(self):
        if self.desired and self.settings["faults"]["auto_reconnect"]:
            delay = (1,2,5,10,30)[min(self.retry_index,4)]
            self.retry_index += 1
            self.reconnect_at = time.perf_counter_ns()+delay*1_000_000_000

    def policy(self, kind, indices):
        for index in indices:
            ident = self.channels["ao"][index]["id"] if index < len(self.channels["ao"]) else str(index)
            default = "continue" if kind == "web" else "zero"
            rule = self.settings["faults"]["policies"][kind].get(ident, {"action": default})
            if rule["action"] == "continue":
                continue
            self.wave.pop(index, None)
            self.values[index] = (self.confirmed[index] if rule["action"] == "hold" else
                                  rule.get("voltage",0) if rule["action"] == "value" else 0.0)
            self.web_channels.discard(index)
        self.owner = self.owner if self.web_channels else 0

    def command(self, command, now):
        ident, action, args, generation = (command[key] for key in ("id","action","args","owner"))
        try:
            if action == "adapters":
                return self.ack(ident, adapters())
            if not self.lease_valid(generation, now):
                raise ValueError("后端服务租约已失效")
            if action.startswith("bridge_"):
                return self.bridge_command(ident, action, args, generation, now)
            if action == "configure":
                self.configure(args["config"])
            elif action in ("link_fault", "runtime_fault"):
                self.fault(args.get("reason", "链路故障"))
            elif action == "web_presence":
                self.web_lost_at = None if args["present"] else self.web_lost_at or now
            elif action == "connect":
                self.connect(args["interface"])
            elif action == "disconnect":
                self.desired = False
                self.reconnect_at = 0
                self.lock()
                self.close_bus()
                self.connection, self.health = "DISCONNECTED", "NORMAL"
            elif action == "set_period":
                period = validated_period(args.get("period_ms"))
                if self.bus and getattr(self.bus,"dc_enabled",False):
                    raise ValueError("DC 模式修改周期请先断开并保存硬件设置")
                for channel,wave in self.wave.items():
                    if wave.frequency > max_frequency(period):
                        raise ValueError(f"AO{channel+1} 正在运行 {wave.frequency:g} Hz，新周期上限为 {max_frequency(period):g} Hz")
                self.period_ms = period
                self.settings["hardware"]["period_ms"] = period
                self.period_revision += 1
            elif action == "zero":
                self.lock()
                if not self.bus:
                    self.zero = "输出状态无法确认；再次连接后先执行零目标"
                    raise ValueError(self.zero)
                self.pending_acks.append(ident)
                return
            elif action in ("arm", "lock_channel"):
                if not self.status()["ready"]:
                    raise ValueError("等待通信连续正常 1 秒")
                channels = args.get("channels", list(range(1,len(self.values)+1)))
                indices = self.validate_channels(channels)
                if action == "arm":
                    occupied = {i for c in self.bridge_cards.values() if c["armed"] for i in c["outputs"]}
                    if any(i in occupied for i in indices):
                        raise ValueError("通道正由 TCP 控制，请先停止对应虚拟卡")
                    self.zero_channels([i for i in indices if i not in self.web_channels])
                    self.web_channels.update(indices)
                    self.owner = generation
                else:
                    if any(i not in self.web_channels for i in indices):
                        raise ValueError("只能锁定网页拥有的通道")
                    self.zero_channels(indices)
                    self.web_channels.difference_update(indices)
                    self.owner = generation if self.web_channels else 0
                self.pending_acks.append(ident)
                return
            elif action in ("set_output","wave","stop_wave"):
                index = self.validate_channels([args.get("channel")])[0]
                if self.owner != generation or index not in self.web_channels or not self.status()["ready"]:
                    raise ValueError("请先允许网页控制此通道")
                if action in ("set_output", "wave"):
                    self.pending_snapshots[ident] = dict(self.wave)
                if action == "wave":
                    wave = build_wave(args.get("shape"),args.get("peak"),args.get("frequency"),self.period_ms,now)
                    for value in (-wave.peak, wave.peak):
                        self.check_drive(index,value)
                    self.wave[index] = wave
                else:
                    if action == "stop_wave":
                        self.protected_zero.add(index)
                    voltage = 0.0 if action == "stop_wave" else validated_voltage(args.get("voltage"))
                    self.check_drive(index,voltage)
                    self.wave.pop(index,None)
                    self.values[index] = voltage
                self.pending_acks.append(ident)
                return
            else:
                raise ValueError("未知命令")
            self.ack(ident,self.status())
        except Exception as exc:
            self.pending_snapshots.pop(ident,None)
            self.ack(ident,error=str(exc))

    def validate_channels(self, channels):
        if not isinstance(channels,list) or not channels or any(type(c) is not int or not 1 <= c <= len(self.values) for c in channels):
            raise ValueError("输出通道编号无效")
        if len(channels) != len(set(channels)):
            raise ValueError("输出通道重复")
        return [c-1 for c in channels]

    def zero_channels(self, channels):
        for index in channels:
            self.values[index] = 0.0
            self.protected_zero.add(index)
            self.wave.pop(index,None)

    def bridge_command(self, ident, action, args, generation, now):
        if action == "bridge_setup":
            if self.bridge_cards or not self.status()["ready"]:
                raise ValueError("请先停止发布，并等待实机稳定")
            cards, used = {}, set()
            for card in args["cards"]:
                outputs = card["outputs"]
                if (not isinstance(card["id"],str) or card["id"] in cards or len(outputs)!=len(set(outputs)) or
                        any(type(i) is not int or not 0 <= i < len(self.values) or i in used for i in outputs)):
                    raise ValueError("虚拟卡可写输出占用无效或重复")
                used.update(outputs)
                cards[card["id"]] = dict(outputs=outputs,token=None,armed=False)
            self.bridge_cards = cards
            self.control_mode,self.bridge_generation = "TCP",generation
        elif action == "bridge_add":
            card = args["definition"]
            used = {i for c in self.bridge_cards.values() for i in c["outputs"]}
            if card["id"] in self.bridge_cards or any(i in used or not 0 <= i < len(self.values) for i in card["outputs"]):
                raise ValueError("虚拟卡 ID 或可写通道重复")
            self.bridge_cards[card["id"]] = dict(outputs=card["outputs"], token=None, armed=False)
            self.control_mode, self.bridge_generation = "TCP", generation
        elif action == "bridge_disable":
            card = self.bridge_cards.pop(args["card"], None)
            if card:
                self.zero_channels(card["outputs"])
            if not self.bridge_cards:
                self.control_mode, self.bridge_generation = "WEB", 0
        elif action == "bridge_stop":
            for card in self.bridge_cards.values():
                self.zero_channels(card["outputs"])
            self.bridge_cards = {}
            self.control_mode,self.bridge_generation = "WEB",0
        else:
            if self.bridge_generation != generation:
                raise ValueError("TCP 控制代次已失效")
            card = self.bridge_cards.get(args.get("card"))
            if card is None:
                raise ValueError("虚拟卡未登记")
            if action == "bridge_authorize":
                if card["token"]:
                    raise ValueError("请先断开该卡的控制客户端")
                self.zero_channels(card["outputs"])
                self.web_channels.difference_update(card["outputs"])
                self.owner = generation if self.web_channels else 0
                card["armed"] = True
            elif action == "bridge_claim":
                if not card["armed"] or card["token"] is not None or not self.status()["ready"]:
                    raise ValueError("虚拟卡未授权、已有控制连接或实机未就绪")
                card["token"] = args["token"]
            else:
                if not card["token"] or card["token"] != args.get("token"):
                    raise ValueError("该连接没有虚拟卡控制权")
                if action == "bridge_release":
                    if args.get("safe"):
                        self.zero_channels(card["outputs"])
                    else:
                        self.policy("tcp",card["outputs"])
                    card.update(token=None,armed=False)
                elif action in ("bridge_output","bridge_table"):
                    if not self.status()["ready"]:
                        raise ValueError("实机未就绪")
                    prepared = []
                    for item in args["changes"]:
                        index = item["index"]
                        if type(index) is not int or index not in card["outputs"]:
                            raise ValueError("超出该虚拟卡的可写输出范围")
                        if action == "bridge_table":
                            value = build_table(item["table"],item["rate"],self.period_ms,now)
                            for v in value.table:
                                self.check_drive(index,v)
                        else:
                            value = validated_voltage(item["voltage"])
                            self.check_drive(index,value)
                        prepared.append((index,value))
                    self.pending_snapshots[ident] = dict(self.wave)
                    for index,value in prepared:
                        if action == "bridge_table":
                            self.wave[index] = value
                        else:
                            self.wave.pop(index,None)
                            self.values[index] = value
                elif action == "bridge_rate":
                    build_table([0.0],args["rate"],self.period_ms,now)
                    prepared = {}
                    for index in card["outputs"]:
                        wave = self.wave.get(index)
                        if isinstance(wave,TableWave):
                            checked = build_table(wave.table,args["rate"],self.period_ms,now)
                            prepared[index] = wave.reclock(checked.rate,now)
                    self.pending_snapshots[ident] = dict(self.wave)
                    self.wave.update(prepared)
                else:
                    raise ValueError("未知桥接命令")
        self.pending_acks.append(ident)

    def fault(self, error):
        if isinstance(self.bus, Hardware):
            self.diagnostic = self.bus.diagnostic_snapshot()
        self.last_error,self.reason = error,error
        owners = self.owners()
        timing = dict(send_ns=getattr(self.bus,"last_send_ns",None),return_ns=getattr(self.bus,"last_receive_ns",None),
                      interval_ms=getattr(self,"last_interval_ms",None),roundtrip_ms=getattr(self,"last_roundtrip_ms",None),period_ms=self.period_ms)
        self.policy("bus",range(len(self.values)))
        for i in self.protected_zero:
            self.values[i] = 0.0
        policy_wkc = None
        if self.bus:
            try:
                policy_wkc = self.bus.exchange([self.check_drive(i,v) for i,v in enumerate(self.values)])
            except Exception:
                pass
        self.event(error,"runtime","error", outputs=self.confirmed,owners=owners,timing=timing,
                   policy=self.settings["faults"]["policies"]["bus"],requested=list(self.values),
                   policy_wkc=policy_wkc,policy_confirmed=policy_wkc==self.expected if policy_wkc is not None else False)
        self.wave.clear()
        self.web_channels.clear()
        self.owner = 0
        self.bridge_cards = {}
        self.bridge_generation = 0
        self.control_mode = "WEB"
        self.zero = "故障策略已请求；输出状态无法确认"
        self.close_bus(safe=False)
        self.connection,self.health = "FAULT","FAULT"
        self.schedule_reconnect()

    def cycle(self, now):
        if ((self.owner and not self.lease_valid(self.owner,now)) or
                (self.bridge_generation and not self.lease_valid(self.bridge_generation,now))):
            self.fault("后端服务租约失效，执行总线故障策略")
        if self.web_lost_at is not None and now-self.web_lost_at >= self.settings["faults"]["web_wait_ms"]*1000000:
            self.policy("web",list(self.web_channels))
            self.web_lost_at = None
        count = len(self.channels["ai"]) if self.channels["ai"] or self.mapping else 4
        raw,flags,valid = [None]*count,[None]*count,[False]*count
        confirmed = [None]*len(self.values)
        error = None
        interval = roundtrip = None
        start_ns = return_ns = None
        if self.bus:
            if not self.bad_count:
                for channel,wave in self.wave.items():
                    self.values[channel] = wave.voltage_at(now)
            try:
                commands = [self.check_drive(i,v) for i,v in enumerate(self.values)]
                self.wkc = self.bus.exchange(commands)
                start_ns,return_ns = self.bus.last_send_ns,self.bus.last_receive_ns
                interval = (start_ns-self.last_exchange)/1e6 if self.last_exchange else None
                self.last_exchange = start_ns
                roundtrip = (return_ns-start_ns)/1e6
                self.last_interval_ms,self.last_roundtrip_ms = interval,roundtrip
                if self.wkc != self.expected:
                    self.errors += 1
                    self.bad_count += 1
                    self.bad_since = self.bad_since or now
                    self.values = [0.0 if i in self.protected_zero else v for i,v in enumerate(self.confirmed)]
                    self.health = "TOLERATING"
                    self.reason = f"WKC 异常：{self.wkc}/{self.expected}"
                    for pending in list(self.pending_acks):
                        if pending in self.pending_snapshots:
                            self.wave = self.pending_snapshots.pop(pending)
                            self.ack(pending,error="本轮输出未获 WKC 确认，已撤销该命令；保持此前目标")
                            self.pending_acks.remove(pending)
                    if self.bad_count == 1:
                        if isinstance(self.bus, Hardware):
                            self.diagnostic = self.bus.diagnostic_snapshot()
                        self.event(self.reason,"runtime","warning")
                    f = self.settings["faults"]
                    if self.bad_count >= f["consecutive"] or now-self.bad_since >= f["duration_ms"]*1000000:
                        raise RuntimeError(self.reason+f"；已达容错阈值（{self.bad_count} 次）")
                else:
                    if self.bad_count:
                        elapsed = now-self.bad_since
                        self.wave = {i:replace(w,start_ns=w.start_ns+now-(self.last_good_ns or self.bad_since)) for i,w in self.wave.items()}
                        self.event(f"WKC 已恢复，暂停 {elapsed/1e6:.1f} ms，异常 {self.bad_count} 次")
                    self.bad_count = self.bad_since = 0
                    self.health,self.reason = "NORMAL",""
                    if now >= self.next_state_check:
                        self.next_state_check = now+100_000_000
                        before = time.perf_counter_ns()
                        if not self.bus.check_op():
                            raise RuntimeError("从站退出 OP")
                        self.diagnostic_ms = (time.perf_counter_ns()-before)/1e6
                        self.devices = self.bus.devices
                    raw,flags = self.bus.read()
                    valid = [not any((f or {}).get(k) for k in ("underrange","overrange","error","sync_error","invalid"))
                             and not ((f or {}).get("status",0)&0x6043) for f in flags]
                    confirmed = [calibrated(volts(raw_voltage(v)),self.profile("ao",i)) for i,v in enumerate(commands)]
                    self.confirmed = list(confirmed)
                    self.last_good_ns = now
                    self.protected_zero.clear()
                    self.zero = "有非零目标" if any(abs(v)>0.001 for v in confirmed) else "零目标已收到 WKC 确认"
                    self.good_since = self.good_since or now
                    if now >= self.next_diagnostic and isinstance(self.bus,Hardware):
                        self.next_diagnostic = now+5_000_000_000
                        self.diagnostic = self.bus.diagnostic_snapshot()
            except Exception as exc:
                error = str(exc)
                if not self.bad_count:
                    self.errors += 1
                self.fault(error)
        if not self.bad_count or error:
            for ident in self.pending_acks:
                self.ack(ident,self.status(),error=error)
                self.pending_snapshots.pop(ident,None)
            self.pending_acks.clear()
        self.seq += 1
        ai = [calibrated(volts(x),self.profile("ai",i)) if ok else None for i,(x,ok) in enumerate(zip(raw,valid))]
        engineering = [engineering_value(v,self.profile("ai",i)) for i,v in enumerate(ai)]
        ao_engineering = [engineering_value(v,self.profile("ao",i)) for i,v in enumerate(confirmed)]
        return dict(seq=self.seq,time_ns=now,mode="LIVE",session=self.session,raw=raw,flags=flags,valid=valid,
                    ao_valid=self.bus is not None and error is None and not self.bad_count,
                    host_send_ns=start_ns,host_return_ns=return_ns,ai=ai,ao=confirmed,
                    engineering=engineering,ao_engineering=ao_engineering,
                    wkc=self.wkc,expected_wkc=self.expected,interval_ms=interval,roundtrip_ms=roundtrip,
                    period_ms=self.period_ms,health=self.health)

    def run(self):
        deadline = time.perf_counter_ns()
        publish_at = deadline
        batch = []
        integral = 0
        while not self.stop.is_set():
            if self.emergency is not None and self.emergency.is_set():
                self.lock("优先清零已执行")
                self.emergency.clear()
            revision,session = self.period_revision,self.session
            try:
                command = self.commands.get_nowait()
            except Empty:
                command = None
            if command:
                self.command(command,time.perf_counter_ns())
            now = time.perf_counter_ns()
            if self.bus is None and self.reconnect_at and now >= self.reconnect_at:
                retry = self.retry_index
                try:
                    self.connect(self.interface)
                except Exception:
                    self.retry_index = retry
                    self.schedule_reconnect()
            online = self.bus is not None
            if online:
                batch.append(self.cycle(now))
            if now >= publish_at or (online and self.bus is None):
                if self.source is not None and not emit(self.source,dict(status=self.status(),samples=batch)):
                    self.source_drops += len(batch)
                if not emit(self.telemetry,dict(status=self.status(),samples=batch)):
                    self.dropped += len(batch)
                batch = []
                publish_at = now+PUBLISH_NS
            step = self.period_ns if self.bus else PUBLISH_NS
            if revision != self.period_revision or session != self.session or not online:
                deadline = now
                integral = 0
            correction = 0
            if self.bus and getattr(self.bus,"dc_enabled",False):
                dc = self.bus.master.dc_time
                target_phase = -min(step//4, 1000000)
                phase = (dc-target_phase+step//2)%step-step//2
                self.dc_error_ns = phase
                integral = max(-step*100,min(step*100,integral+phase))
                correction = max(-step//10,min(step//10,-phase//100-integral//20000))
            deadline += step+correction
            end = time.perf_counter_ns()
            if deadline <= end:
                skipped = (end-deadline)//step+1
                if self.bus:
                    self.missed += skipped
                deadline += skipped*step
            time.sleep(max(0,(deadline-time.perf_counter_ns())/1e9))


def run_worker(commands, events, telemetry, lease, stop, cleanup_done, source=None, emergency=None):
    worker = Worker(commands,events,telemetry,lease,stop,source,emergency)
    with ConsoleStop(lambda source: worker.stop.set()):
        try:
            worker.run()
        except KeyboardInterrupt:
            pass
        finally:
            worker.lock()
            confirmed = worker.close_bus()
            zero = "not_connected" if confirmed is None else "confirmed" if confirmed else "unknown"
            log_stop("worker_stop",zero=zero)
            cleanup_done.set()
            if confirmed is not None:
                console_message(f"EtherCAT 停止结果：{worker.zero}")
