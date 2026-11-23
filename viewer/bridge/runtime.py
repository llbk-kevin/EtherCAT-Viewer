"""异步 TCP 服务；所有物理输出经通信进程校验及 PDO 回执。"""
import asyncio
from collections import deque
import ipaddress
import json
import math
import socket
import struct
import time
import uuid

from ..console import console_message
from ..generator import build_table
from ..paths import data_root
from ..pdo import raw_voltage, volts
from .config import load_config, local_addresses, preset, save_config, validate_config
from . import protocol as p
from .sampling import Sampler


class UnsupportedCommand(ValueError):
    pass


class Card:
    def __init__(self, config):
        self.config = config
        self.sessions = set()
        self.controller = None
        self.authorized = False
        self.ever_authorized = False
        self.last_error = ""
        self.last_stats = {}


class Bridge:
    def __init__(self, request, critical):
        self.request, self.critical = request, critical
        self.config, self.last_error = load_config()
        self.cards, self.servers = {}, []
        self.channels = {"ai": [], "ao": []}
        self.source_status = {}
        self.last_source_ns = 0
        self.latest_sample = {}
        self.active = self.stopping = False
        self.events = deque(maxlen=30)
        self.samples = deque()
        self.native_hz = None
        self.seen_tcp = False
        self.persist_lock = asyncio.Lock()
        self.close_tasks = set()
        self.server_cards = {}
        self.suspended = False
        self.persist_callback = None
        self.event_callback = None
        self.fault_callback = None
        self.fault_pending = False

    def log(self, message):
        self.events.append(dict(time=time.time(), message=message))
        console_message("TCP 桥接 · " + message)
        if self.event_callback:
            self.event_callback("control", "TCP 桥接 · " + message)
        try:
            target = data_root() / "logs" / "bridge-events.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.events[-1], ensure_ascii=False) + "\n")
        except OSError:
            pass

    def replace_config(self, data):
        self.config = data
        by_id = {c["id"]:c for c in data["cards"]}
        for ident,card in self.cards.items():
            if ident in by_id:
                card.config = by_id[ident]

    async def configure(self, data, status):
        if self.active:
            by_id = {c["id"]: c for c in data.get("cards", [])}
            if any(by_id.get(ident) != card.config for ident, card in self.cards.items()):
                raise ValueError("请先停止受影响的虚拟卡再修改配置")
        addresses = await asyncio.to_thread(local_addresses)
        checked = validate_config(data, status.get("channels", {}), status.get("fingerprint"), addresses)
        if self.persist_callback:
            await self.persist_callback(checked)
        else:
            await asyncio.to_thread(save_config, checked)
        self.replace_config(checked)
        self.last_error = ""
        return self.config

    def make_preset(self, profile, status):
        if profile not in ("legacy", "extended") or not status.get("fingerprint"):
            raise ValueError("请先连接实机并选择有效模板")
        return preset(status["channels"], status["fingerprint"], profile)

    def writable(self, config):
        permissions = config.get("ao_writable", [True]*len(config["ao"]))
        return [self.index("ao", x) for x, allowed in zip(config["ao"], permissions) if x is not None and allowed]

    async def start(self, status, card_id=None):
        if status.get("connection") != "OP" or not status.get("ready"):
            raise ValueError("请先连接实机并等待通信稳定")
        self.channels = status["channels"]
        config = validate_config(self.config, self.channels, status["fingerprint"], await asyncio.to_thread(local_addresses))
        self.replace_config(config)
        selected = [c for c in config["cards"] if c["enabled"] and c["id"] not in self.cards and (card_id is None or c["id"] == card_id)]
        if not selected:
            raise ValueError("没有尚未监听的启用虚拟卡")
        pending = []
        try:
            for c in selected:
                server = await asyncio.start_server(lambda r,w,ident=c["id"]: self.accept(ident,r,w),
                                                   c["host"],c["port"],start_serving=False,limit=p.MAX_PAYLOAD+32,reuse_address=False)
                pending.append((c,server))
            if not self.active:
                await self.request("bridge_setup", {"cards": [dict(id=c["id"],outputs=self.writable(c)) for c in selected]})
            else:
                for c in selected:
                    await self.request("bridge_add", {"definition": dict(id=c["id"],outputs=self.writable(c))})
            self.active, self.stopping, self.suspended = True,False,False
            self.seen_tcp = False
            self.source_status = status
            for c,server in pending:
                self.cards[c["id"]] = Card(c)
                self.server_cards[c["id"]] = server
                self.servers.append(server)
                await server.start_serving()
                if not self.writable(c):
                    await self.authorize(c["id"])
            self.last_error = ""
            self.log(f"开始监听 {len(pending)} 张虚拟卡；可写通道等待授权")
        except Exception:
            for _c,server in pending:
                server.close()
                await server.wait_closed()
            raise

    async def resume(self, status):
        if not self.active or not self.suspended or not status.get("ready"):
            return
        await self.request("bridge_setup", {"cards": [dict(id=c.config["id"],outputs=self.writable(c.config)) for c in self.cards.values()]})
        self.suspended = False
        self.seen_tcp = False
        for ident,card in self.cards.items():
            if card.ever_authorized and card.config.get("auto_reauthorize",True):
                await self.authorize(ident)
        self.log("通信恢复，零输出确认后等待新的客户端指令")

    async def stop_card(self, ident):
        card = self.cards.get(ident)
        if card is None:
            return
        card.ever_authorized = False
        server = self.server_cards.pop(ident)
        server.close()
        await server.wait_closed()
        self.servers.remove(server)
        await asyncio.gather(*(s.close("本卡停止发布",release=False) for s in tuple(card.sessions)),return_exceptions=True)
        if not self.suspended:
            await self.request("bridge_disable",dict(card=ident))
        self.cards.pop(ident,None)
        self.active = bool(self.cards)
        self.log(f"{card.config['name']} 已停止；仅本卡可写 AO 清零")

    def index(self, kind, ident):
        return next(i for i, c in enumerate(self.channels[kind]) if c["id"] == ident)

    async def authorize(self, ident):
        if not self.active or ident not in self.cards:
            raise ValueError("虚拟卡未监听")
        await self.request("bridge_authorize", {"card": ident})
        self.cards[ident].authorized = True
        self.cards[ident].ever_authorized = True
        self.cards[ident].last_error = ""
        self.log(f"{self.cards[ident].config['name']} 已授权接管")

    async def stop(self, reason="停止发布", shutdown=False):
        if self.stopping:
            return
        self.stopping = True
        was_active = self.active
        self.active = False
        for server in self.servers:
            server.close()
        for server in self.servers:
            await server.wait_closed()
        self.servers = []
        self.server_cards = {}
        sessions = [s for c in self.cards.values() for s in c.sessions]
        await asyncio.gather(*(s.close(reason, release=False) for s in sessions), return_exceptions=True)
        if was_active and not shutdown:
            try:
                await self.request("bridge_stop", {})
            except Exception as exc:
                self.last_error = f"{reason}；清零结果：{exc}"
        for card in self.cards.values():
            card.authorized = False
        if was_active:
            self.log(reason)
        self.cards = {}
        self.suspended = False
        self.stopping = False

    async def accept(self, ident, reader, writer):
        card = self.cards[ident]
        peer = writer.get_extra_info("peername")
        permitted = peer and any(ipaddress.ip_address(peer[0]) in ipaddress.ip_network(n)
                                 for n in card.config["allow"])
        if (not self.active or not permitted or len(card.sessions) >= 32 or
                self.suspended or not self.source_status.get("ready", True) or
                (card.config["profile"] == "legacy" and card.controller is not None)):
            writer.close()
            await writer.wait_closed()
            return
        session = Session(self, card, reader, writer)
        card.sessions.add(session)
        await session.run()

    async def persist(self, updates=None):
        async with self.persist_lock:
            if self.persist_callback:
                await self.persist_callback(self.config,updates=updates)
            else:
                await asyncio.to_thread(save_config, self.config)

    def feed(self, status, samples):
        self.source_status = status
        if samples:
            self.latest_sample = samples[-1]
        for sample in samples:
            stamp = sample.get("host_send_ns") or sample["time_ns"]
            if sample.get("ao_valid", True):
                self.samples.append(stamp)
            self.last_source_ns = stamp
        if self.samples:
            cutoff = self.samples[-1] - 10_000_000_000
            while self.samples and self.samples[0] < cutoff:
                self.samples.popleft()
            self.native_hz = ((len(self.samples)-1)*1e9/(self.samples[-1]-self.samples[0])
                              if len(self.samples) > 1 and self.samples[-1] > self.samples[0] else None)
        if status.get("connection") != "OP":
            self.native_hz = None
            self.samples.clear()
        if not self.active:
            return
        if samples and time.perf_counter_ns() - (samples[-1].get("host_send_ns") or samples[-1]["time_ns"]) > 1_000_000_000:
            self.fault("原始数据积压超过 1 秒")
            return
        if status.get("control_mode") == "TCP":
            self.seen_tcp = True
        if not self.seen_tcp and status.get("connection") == "OP":
            return
        if status.get("connection") != "OP" or status.get("control_mode") != "TCP":
            if not self.suspended:
                self.suspended = True
                for card in self.cards.values():
                    card.authorized = False
                    for session in tuple(card.sessions):
                        session.stop_stream()
                        if session.owner:
                            task = asyncio.create_task(session.close("总线故障，等待恢复", release=False))
                            self.close_tasks.add(task)
                            task.add_done_callback(self.close_tasks.discard)
            return
        if self.suspended:
            return
        for card in self.cards.values():
            for session in tuple(card.sessions):
                try:
                    session.feed(samples)
                except Exception as exc:
                    self.schedule_close(session, str(exc))

    def schedule_close(self, session, reason):
        if session.closing or session.failed:
            return
        session.failed = True
        task = asyncio.create_task(session.close(reason))
        self.close_tasks.add(task)
        task.add_done_callback(self.close_tasks.discard)

    def fault(self, reason):
        if self.fault_callback and self.active and not self.stopping:
            if not self.fault_pending:
                self.fault_pending = True
                async def report_fault():
                    try:
                        await self.fault_callback(reason)
                    finally:
                        self.fault_pending = False
                asyncio.create_task(report_fault())
            return
        if self.active and not self.stopping:
            self.last_error = reason
            self.critical()
            self.stopping = True
            async def finish():
                self.stopping = False
                await self.stop(reason)
            asyncio.create_task(finish())

    def status(self):
        fresh = (self.source_status.get("connection") == "OP" and self.last_source_ns > 0 and
                 time.perf_counter_ns()-self.last_source_ns < 1_000_000_000)
        result = []
        for card in self.cards.values():
            config = card.config
            outputs = []
            for ident in config["ao"]:
                index = self.index("ao", ident) if ident else None
                actual = self.source_status.get("ao", [])
                confirmed = (fresh and
                             self.source_status.get("wkc") is not None and
                             self.source_status.get("wkc") == self.source_status.get("expected_wkc"))
                outputs.append(volts(raw_voltage(actual[index])) if confirmed and index is not None
                               and index < len(actual) else None)
            result.append(dict(id=config["id"], name=config["name"], profile=config["profile"],
                               address=f"{config['host']}:{config['port']}", authorized=card.authorized,
                               controller=card.controller.peer if card.controller else None,
                               connections=len(card.sessions), error=card.last_error, physical_ao=outputs, ao_writable=config.get("ao_writable"),
                               sessions=[s.summary() for s in card.sessions], last_stats=card.last_stats))
        source = {key: self.source_status.get(key) for key in (
            "connection", "control_mode", "ready", "period_ms", "expected_wkc", "watchdog_ms",
            "errors", "missed", "source_drops", "reason", "last_error", "zero")}
        source.update(live=fresh, wkc=self.source_status.get("wkc") if fresh else None,
                      last_wkc=self.source_status.get("wkc"),
                      age_ms=(time.perf_counter_ns()-self.last_source_ns)/1e6 if self.last_source_ns else None,
                      interval_ms=self.latest_sample.get("interval_ms") if fresh else None,
                      roundtrip_ms=self.latest_sample.get("roundtrip_ms") if fresh else None)
        return dict(active=self.active, suspended=self.suspended, last_error=self.last_error, cards=result, source=source,
                    native_hz=self.native_hz if fresh else None, events=list(self.events), config=self.config,
                    source_batch_ms=50, timestamp_source="host_pdo_exchange")


class PrefixedReader:
    def __init__(self, prefix, reader):
        self.prefix, self.reader = prefix, reader

    async def readexactly(self, size):
        prefix, self.prefix = self.prefix[:size], self.prefix[size:]
        return prefix + await self.reader.readexactly(size-len(prefix))


class Session:
    def __init__(self, bridge, card, reader, writer):
        self.bridge, self.card = bridge, card
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}"
        self.token = uuid.uuid4().hex
        self.owner = self.closing = self.failed = False
        self.queue = asyncio.Queue()
        self.queued_bytes = 0
        self.send_task = self.status_task = None
        self.sampler = None
        self.pending_data = bytearray()
        self.block = bytearray()
        self.block_meta = []
        self.last_flush = time.monotonic()
        self.frame_sequence = self.sent_samples = self.sent_frames = 0
        self.frame_limit = 0
        self.range = card.config.get("saved_range", 2)
        self.divider = card.config.get("saved_divider", 65000)
        self.buffer_first_ns = self.pending_first_ns = None
        self.wave_divider = 65000
        self.tables = {}
        self.wave_info = []
        self.table_rate = 2000
        self.paused = False
        self.subscribed = False
        self.last_command = None
        self.last_stats = {}
        self.last_output = []

    @property
    def legacy(self):
        return self.card.config["profile"] == "legacy"

    async def request(self, action, **args):
        return await self.bridge.request(action, dict(card=self.card.config["id"], token=self.token, **args))

    async def claim(self):
        if self.owner:
            return
        if self.card.controller is not None:
            raise ValueError("该卡已有控制客户端")
        if not self.card.authorized and self.card.ever_authorized and self.card.config.get("auto_reauthorize", True):
            await self.bridge.authorize(self.card.config["id"])
        await self.request("bridge_claim")
        self.card.controller = self
        self.owner = True

    def enqueue(self, data, kind="reply"):
        if self.closing:
            return
        now = time.monotonic()
        if self.queued_bytes + len(data) > 8*1024*1024:
            raise ValueError("客户端积压超过 8 MiB")
        self.queued_bytes += len(data)
        self.queue.put_nowait((now, data, kind))

    async def sender(self):
        while True:
            created, data, _kind = await self.queue.get()
            limit = self.card.config["backlog_ms"]/1000
            if time.monotonic()-created > limit:
                raise ValueError("客户端发送积压超时")
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), timeout=limit)
            self.queued_bytes -= len(data)

    async def run(self):
        sock = self.writer.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
        self.writer.transport.set_write_buffer_limits(high=256*1024)
        reason = "控制连接已关闭" if self.legacy else "扩展连接已关闭"
        try:
            if self.legacy:
                await self.claim()
            self.bridge.log(f"{self.card.config['name']} 连接 {self.peer}")
            self.send_task = asyncio.create_task(self.sender())
            self.send_task.add_done_callback(self.sender_done)
            if not self.legacy:
                self.enqueue(p.extended_json(p.CAPS, {"ok": True, "version": 1, "capabilities": self.capabilities()}))
                self.status_task = asyncio.create_task(self.send_status())
            while not self.closing:
                first = await self.reader.readexactly(1)
                parse = p.read_legacy if self.legacy else p.read_extended
                opcode, payload = await asyncio.wait_for(parse(PrefixedReader(first, self.reader)), 5)
                self.last_command = f"0x{opcode:04X}"
                if self.legacy:
                    try:
                        await self.legacy_command(opcode, payload)
                    except UnsupportedCommand:
                        raise
                    except ValueError as exc:
                        self.card.last_error = str(exc)
                        self.bridge.log(f"{self.card.config['name']} 拒绝 {self.last_command}：{exc}")
                else:
                    await self.extended_command(opcode, payload)
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            reason = "服务停止"
        except Exception as exc:
            reason = str(exc)
        finally:
            await self.close(reason)

    def sender_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.bridge.schedule_close(self, f"发送失败：{task.exception()}")

    async def close(self, reason, release=True):
        if self.closing:
            return
        self.closing = True
        self.card.last_stats = self.summary()
        self.stop_stream()
        for task in (self.send_task, self.status_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self.owner:
            self.card.authorized = False
            if release and self.bridge.active:
                try:
                    await asyncio.wait_for(self.request("bridge_release"), 2)
                except Exception as exc:
                    self.bridge.fault(f"控制连接清零未确认：{exc}")
            self.card.controller = None
            self.owner = False
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), .25)
        except Exception:
            pass
        self.card.sessions.discard(self)
        self.card.last_error = reason
        self.bridge.log(f"{self.card.config['name']} {self.peer}：{reason}")

    def stop_stream(self, discard=True):
        if self.sampler:
            self.last_stats = dict(logical_samples=self.sampler.generated, held=self.sampler.held,
                                   sources=self.sampler.sources, clipped=self.sampler.clipped)
        self.sampler = None
        self.pending_data.clear()
        self.buffer_first_ns = self.pending_first_ns = None
        self.block.clear()
        self.block_meta.clear()
        retained = []
        while not self.queue.empty():
            entry = self.queue.get_nowait()
            if discard and entry[2] == "data":
                self.queued_bytes -= len(entry[1])
            else:
                retained.append(entry)
        for entry in retained:
            self.queue.put_nowait(entry)

    def summary(self):
        sampler = self.sampler
        return dict(peer=self.peer, controller=self.owner, streaming=sampler is not None,
                    rate_hz=sampler.rate if sampler else None,
                    logical_samples=sampler.generated if sampler else self.last_stats.get("logical_samples", 0),
                    held=sampler.held if sampler else self.last_stats.get("held", 0),
                    sources=sampler.sources if sampler else self.last_stats.get("sources", 0),
                    clipped=sampler.clipped if sampler else self.last_stats.get("clipped", 0),
                    sent_frames=self.sent_frames, sent_samples=self.sent_samples, queued_bytes=self.queued_bytes,
                    last_command=self.last_command, last_output=self.last_output, wave_info=self.wave_info,
                    latency_ms=getattr(self, "latency_ms", None))

    def capabilities(self):
        return dict(card=self.card.config, channels=self.bridge.channels,
                    timestamp_source="host_pdo_exchange", target_period_ms=self.bridge.source_status.get("period_ms", 10),
                    native_hz=self.bridge.native_hz, supported=["adc", "dc", "table_wave", "range", "info"],
                    unsupported=["pwm", "dio", "counter", "uart", "trigger", "external_clock", "network_write"],
                    source_batch_ms=50, max_payload=p.MAX_PAYLOAD, max_clients=32)

    async def send_status(self):
        while True:
            await asyncio.sleep(.25)
            if self.subscribed:
                self.enqueue(p.extended_json(p.EVENT, self.bridge.status()))

    def start_stream(self, virtual_channels, rate, frame_limit=0, differential=False):
        indices = self.card.config["ai"]
        mappings = []
        for channel in virtual_channels:
            if type(channel) is not int or not 0 <= channel < len(indices):
                raise ValueError("虚拟 AI 通道编号无效")
            selected = [(channel, 1)]
            if differential:
                selected = [(channel+1, 1), (channel, -1)]
            sources = []
            for ch, sign in selected:
                if ch >= len(indices):
                    raise ValueError("差分通道无效")
                if indices[ch] is not None:
                    sources.append((self.bridge.index("ai", indices[ch]), sign))
            mappings.append(sources)
        if not mappings:
            raise ValueError("至少选择一个输入通道")
        sampler = Sampler(mappings, rate, self.card.config["sampling"], 5 if self.range == 1 else 10,
                          1000/self.bridge.source_status.get("period_ms", 10))
        self.stop_stream()
        self.sampler = sampler
        self.stream_started_ns = time.perf_counter_ns()
        self.stream_channels = virtual_channels
        self.frame_limit = frame_limit
        self.frame_sequence = self.sent_samples = self.sent_frames = 0
        self.last_flush = time.monotonic()

    def pause_stream(self):
        if self.sampler:
            self.sampler.pause()
        self.pending_data.clear()
        self.pending_first_ns = None
        self.paused = True

    def feed(self, samples):
        if self.failed or not self.sampler:
            return
        for sample in samples:
            if self.sampler is None:
                break
            if Sampler.stamp(sample) < self.stream_started_ns:
                continue
            if sample.get("health") == "TOLERATING" or any(not sample["valid"][i] for group in self.sampler.mappings for i,_ in group):
                self.pause_stream()
                continue
            self.paused = False
            for data, count, meta in self.sampler.feed(sample):
                self.latency_ms = max(0, (time.perf_counter_ns()-meta["logical_start_ns"])/1e6)
                if self.legacy:
                    if not self.pending_data:
                        self.pending_first_ns = meta["logical_start_ns"]
                    self.pending_data.extend(data)
                    while len(self.pending_data) >= 1440:
                        if not self.block:
                            self.buffer_first_ns = self.pending_first_ns
                        self.pending_first_ns += round(720 / len(self.stream_channels) * 1e9 / self.sampler.rate)
                        self.frame_sequence += 1
                        self.block.extend(p.legacy_adc(self.frame_sequence, self.pending_data[:1440]))
                        del self.pending_data[:1440]
                        self.sent_frames += 1
                        self.sent_samples += 720 // len(self.stream_channels)
                        if self.frame_limit and self.sent_frames >= self.frame_limit:
                            self.flush()
                            self.stop_stream(discard=False)
                            break
                else:
                    meta["engineering"] = [sample.get("engineering", [])[self.bridge.index("ai", self.card.config["ai"][ch])] if self.card.config["ai"][ch] and self.bridge.index("ai", self.card.config["ai"][ch]) < len(sample.get("engineering", [])) else None for ch in self.stream_channels]
                    meta["channel_profiles"] = {ident:self.bridge.source_status.get("channel_profiles",{}).get(ident,{}) for ident in self.card.config["ai"] if ident}
                    meta.update(sample_count=count, channel_count=len(self.stream_channels),
                                channels=self.stream_channels, encoding="i16le", sequence=self.sent_frames+1)
                    if not self.block:
                        self.buffer_first_ns = meta["logical_start_ns"]
                    self.block.extend(p.data_frame(meta, data))
                    self.sent_frames += 1
                    self.sent_samples += count
                    if self.frame_limit and self.sent_frames >= self.frame_limit:
                        self.flush()
                        self.stop_stream(discard=False)
                        break
                if len(self.block) > 512*1024:
                    self.flush()
        if not self.paused and time.monotonic()-self.last_flush >= self.card.config["send_ms"]/1000:
            self.flush()

    def flush(self):
        if self.block:
            self.latency_ms = max(0, (time.perf_counter_ns() - self.buffer_first_ns)/1e6)
            self.enqueue(bytes(self.block), "data")
            self.buffer_first_ns = None
            self.block.clear()
        self.last_flush = time.monotonic()

    def output_indices(self, channels):
        indices = []
        for channel in channels:
            if type(channel) is not int or not 1 <= channel <= len(self.card.config["ao"]):
                raise ValueError("虚拟 AO 编号无效，输出从 1 开始")
            if not self.card.config.get("ao_writable", [True]*len(self.card.config["ao"]))[channel-1]:
                raise ValueError("该虚拟 AO 为只读映射，不能写入")
            ident = self.card.config["ao"][channel-1]
            if ident is None:
                raise ValueError("该 AO 未映射")
            indices.append(self.bridge.index("ao", ident))
        return indices

    async def dc(self, channels, voltages, codes=None):
        if not self.owner:
            raise ValueError("只读连接不能改变输出")
        indices = self.output_indices(channels)
        if len(voltages) != len(indices):
            raise ValueError("输出电压数量不符")
        await self.request("bridge_output", changes=[dict(index=i, voltage=v) for i, v in zip(indices, voltages)])
        for ch in channels:
            self.tables.pop(ch, None)
        self.update_wave_info(self.table_rate)
        self.last_output = [dict(channel=ch, physical_v=v, code=codes[j] if codes else None,
                                 software_v=codes[j]*3.3/4095 if codes else None)
                            for j, (ch, v) in enumerate(zip(channels, voltages))]

    def update_wave_info(self, rate):
        period = self.bridge.source_status.get("period_ms", 10)
        self.wave_info = []
        for channel, table in self.tables.items():
            frequency = rate/len(table) if len(set(table)) > 1 else 0
            points = (1000/period/frequency) if frequency else None
            self.wave_info.append(dict(channel=channel, table_points=len(table), rate_hz=rate,
                                       frequency_hz=frequency, actual_points_per_cycle=points,
                                       warning=points is not None and points < 20))

    async def table(self, channel, values, rate):
        if not self.owner:
            raise ValueError("只读连接不能改变输出")
        # 原版共享时钟：先一次性验证所有受影响波形，再原子应用。
        tables = dict(self.tables)
        tables[channel] = values
        period = self.bridge.source_status.get("period_ms", 10)
        for table in tables.values():
            build_table(table, rate, period, 0)
        changes = [dict(index=self.output_indices([ch])[0], table=table, rate=rate) for ch, table in tables.items()]
        await self.request("bridge_table", changes=changes)
        self.tables = tables
        self.table_rate = rate
        self.update_wave_info(rate)
        self.last_output = [v for v in self.last_output if v["channel"] not in tables]

    async def legacy_command(self, opcode, payload):
        if opcode == 0xA452:
            if payload != bytes.fromhex("13 A4 F1 C5"):
                raise ValueError("设备查询校验参数无效")
            config = self.card.config
            info = bytearray(46)
            info[:5] = bytes.fromhex("AA 55 2E 52 A4")
            info[5:9], info[9:13], info[13:17] = bytes.fromhex("26 20 00 14"), bytes.fromhex("23 45 00 00"), bytes.fromhex("6D 12 03 0D")
            info[17:21] = ipaddress.IPv4Address(config["host"]).packed
            struct.pack_into("<H", info, 21, config["port"])
            info[27:31] = bytes.fromhex("FF 00 00 00" if config["host"].startswith("127.") else "FF FF FF 00")
            import hashlib
            info[31:37] = b"\x02" + hashlib.sha256(config["id"].encode()).digest()[:5]
            info[37] = config.get("saved_range", 2)
            struct.pack_into("<HH", info, 38, 1, config.get("saved_divider", 65000))
            self.enqueue(bytes(info))
        elif opcode == 0xA3D1:
            self.enqueue(p.legacy_command(0xFAF5, bytes([(payload[0]+1)&255])))
        elif opcode == 0xA505:
            self.stop_stream()
        elif opcode in (0xA3E2, 0xA3D0):
            if self.sampler:
                raise ValueError("请先停止采集再修改量程或采样率")
            save = payload[-1]
            if save not in (1, 2):
                raise ValueError("保存标志无效")
            if opcode == 0xA3E2:
                value, save = payload
                if value not in (1, 2):
                    raise ValueError("采集量程无效")
                key = "saved_range"
                self.range = value
            else:
                value, save = struct.unpack("<HB", payload)
                if not 650 <= value <= 65535:
                    raise ValueError("原版 ADC 分频必须为 650–65535")
                key = "saved_divider"
                self.divider = value
            if save not in (1, 2):
                raise ValueError("保存标志无效")
            if save == 2:
                self.card.config[key] = value
                await self.bridge.persist({self.card.config["id"]:{key:value}})
        elif opcode in (0xA111, 0xA112, 0xA113, 0xA114, 0xA115):
            group = payload[0] if opcode in (0xA112, 0xA114, 0xA115) else 1
            frames = struct.unpack("<I", payload[1:] if len(payload) == 5 else payload)[0]
            if opcode == 0xA111:
                channels = list(range(8))
            elif opcode == 0xA113:
                channels = [0, 2, 4, 6]
            else:
                width = {0xA112: 4, 0xA114: 2, 0xA115: 1}[opcode]
                if not 1 <= group <= 8//width:
                    raise ValueError("采集通道组无效")
                channels = list(range((group-1)*width, group*width))
            self.start_stream(channels, 130_000_000/self.divider, frames, opcode == 0xA113)
        elif opcode == 0xA659:
            channel, code, save = struct.unpack("<BHB", payload)
            if channel not in (1, 2, 3) or save not in (1, 2):
                raise ValueError("DAC 参数无效")
            voltage = p.dac_voltage(code, self.card.config["ao_range"])
            if save == 2 and voltage != 0:
                raise ValueError("不支持保存非零上电输出；清零始终使用物理 0 V")
            channels = [1, 2] if channel == 3 else [channel]
            await self.dc(channels, [voltage]*len(channels), [code]*len(channels))
        elif opcode == 0xA65A:
            channel, divider, last = struct.unpack_from("<BHB", payload)
            if channel not in (1, 2) or divider == 0 or len(payload) != 4+2*(last+1):
                raise ValueError("原版波形参数无效")
            codes = struct.unpack_from("<"+"H"*(last+1), payload, 4)
            values = [p.dac_voltage(c, self.card.config["ao_range"]) for c in codes]
            await self.table(channel, values, 130_000_000/divider)
            self.wave_divider = divider
        elif opcode == 0xA65B:
            _nc, divider = struct.unpack("<BH", payload)
            if divider == 0:
                raise ValueError("波形分频不能为零")
            await self.request("bridge_rate", rate=130_000_000/divider)
            self.wave_divider = divider
            self.table_rate = 130_000_000/divider
            self.update_wave_info(self.table_rate)
        else:
            raise UnsupportedCommand(f"未实现真实硬件能力或命令：0x{opcode:04X}")

    async def extended_command(self, opcode, payload):
        ident = None
        try:
            args = json.loads(payload.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("非有限数")))
            if not isinstance(args, dict):
                raise ValueError("扩展参数必须是 JSON 对象")
            ident = args.get("id")
            result = None
            if opcode == p.CAPS:
                result = self.capabilities()
            elif opcode == p.CLAIM:
                await self.claim()
            elif opcode == p.RELEASE:
                if self.owner:
                    await self.request("bridge_release", safe=True)
                    self.card.ever_authorized = False
                    self.owner = False
                    self.card.controller = None
                    self.card.authorized = False
                self.stop_stream()
            elif opcode == p.START:
                self.range = args.get("range", 2)
                if type(self.range) is not int or self.range not in (1, 2):
                    raise ValueError("量程必须为 1 或 2")
                frames = args.get("blocks", 0)
                if type(frames) is not int or not 0 <= frames <= 0xffffffff:
                    raise ValueError("数据块数量无效")
                self.start_stream(args.get("channels", list(range(len(self.card.config["ai"])))),
                                  args.get("rate_hz", 100), frames)
            elif opcode == p.STOP:
                self.stop_stream()
            elif opcode == p.OUTPUT:
                await self.dc(args["channels"], args["volts"])
            elif opcode == p.WAVE:
                await self.table(args["channel"], args["volts"], args["rate_hz"])
            elif opcode == p.RATE:
                if not self.owner:
                    raise ValueError("只读连接不能改变时钟")
                await self.request("bridge_rate", rate=args["rate_hz"])
                self.table_rate = args["rate_hz"]
                self.update_wave_info(self.table_rate)
            elif opcode == p.STATUS:
                result = self.bridge.status()
            elif opcode == p.SUBSCRIBE:
                if type(args.get("enabled", True)) is not bool:
                    raise ValueError("订阅参数无效")
                self.subscribed = args.get("enabled", True)
            else:
                raise ValueError("未知扩展操作码")
            self.enqueue(p.extended_json(opcode, dict(id=ident, ok=True, result=result)))
        except (ValueError, KeyError, TypeError, struct.error) as exc:
            self.enqueue(p.extended_json(p.ERROR, dict(id=ident, ok=False, error=str(exc))))
