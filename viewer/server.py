import asyncio
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import csv
import io
import json
import multiprocessing as mp
from pathlib import Path
from queue import Empty, Full
import secrets
import time
import zipfile

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .bridge.runtime import Bridge
from .bridge.config import local_addresses, validate_config
from .calibration import two_point, engineering_voltage
from .console import BusConsole, console_message, log_stop
from .diagnostics import History, Journal
from .hardware import adapters
from .paths import data_root
from .settings import Settings, validate
from .configuration import SettingError, get_value, make_plan, merge_cards
from .system import login_status, set_login, open_config_directory
from .timing import TimingWindow
from .worker import run_worker

STATIC = Path(__file__).resolve().parent/"static"


class Command(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    action: str
    args: dict = Field(default_factory=dict)


def csv_rows(samples):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for s in samples:
        writer.writerow([s["seq"],s["time_ns"],s["mode"],s["wkc"],s["expected_wkc"],*s["raw"],*s["ai"],
                         *s["valid"],*(json.dumps(f) for f in s["flags"]),*s["ao"],s["ao_valid"],s["period_ms"],
                         s["interval_ms"],s["roundtrip_ms"],s.get("host_send_ns"),s.get("host_return_ns"),
                         *s.get("engineering",[]),*s.get("ao_engineering",[])])
    return stream.getvalue()


class Service:
    def __init__(self):
        self.ctx = mp.get_context("spawn")
        self.lease,self.stop = self.ctx.Array("q",2),self.ctx.Event()
        self.emergency,self.cleanup_done = self.ctx.Event(),self.ctx.Event()
        self.settings = Settings()
        self.config_lock = asyncio.Lock()
        self.status = dict(mode="LIVE",connection="DISCONNECTED",health="NORMAL",period_ms=10)
        self.console = BusConsole(self.status)
        self.timing = TimingWindow()
        self.timing_summary = self.timing.summarize()
        self.last_summary = self.connection_session = self.last_update = 0
        self.last_broadcast = 0
        self.clients,self.viewer_drops,self.known_sessions = {},{},set()
        self.controller = None
        self.page_hiding = set()
        self.generation,self.backend_lease = 1,True
        self.pending = {}
        self.recording = self.last_recording = self.record_error = None
        self.adapter_cache = None
        self.recent = deque(maxlen=3000)
        self.history = History()
        self.journal = Journal(data_root()/"logs"/"diagnostics")
        self.bridge = Bridge(self.bridge_request,self.critical_zero)
        self.bridge.config = deepcopy(self.settings.data["bridge"])
        self.bridge.persist_callback = self.persist_bridge
        self.bridge.event_callback = self.journal.log
        self.bridge.fault_callback = self.source_fault
        self.operation,self.operation_task = None,None
        self.publish_intent = False
        self.manual_lock = False
        self.nic = {}
        self.last_nic = 0
        self.fault_clips = []
        self.alarm_key = None
        self.alarm_started = 0
        self.alarm_samples = 0
        self.command_results = {}
        self.start_worker()

    def start_worker(self):
        self.commands,self.events,self.telemetry,self.source = (self.ctx.Queue(n) for n in (8,64,16,64))
        self.cleanup_done.clear()
        self.worker = self.ctx.Process(target=run_worker,args=(self.commands,self.events,self.telemetry,self.lease,
                                      self.stop,self.cleanup_done,self.source,self.emergency))
        self.worker.start()

    def heartbeat(self):
        with self.lease.get_lock():
            self.lease[:] = [self.generation,time.perf_counter_ns()] if (getattr(self,"backend_lease",False) or self.controller or self.bridge.active) and getattr(self,"status",{}).get("connection") != "STALE" else [0,0]

    def critical_zero(self):
        self.generation += 1
        self.manual_lock = True
        self.publish_intent = False
        self.heartbeat()
        self.emergency.set()

    async def bridge_request(self,action,args):
        answer = await worker_request(self,action,args,self.generation)
        if not answer["ok"]:
            raise ValueError(answer["error"])
        return answer.get("result")

    async def source_fault(self,reason):
        try:
            await self.bridge_request("runtime_fault",dict(reason=reason))
        except Exception as exc:
            self.journal.log("runtime",f"无法请求通信故障策略：{exc}","error")

    def set_bridge_configuration(self, data):
        self.bridge.config = deepcopy(data)
        by_id = {card["id"]:card for card in self.bridge.config["cards"]}
        for ident,card in self.bridge.cards.items():
            if ident in by_id:
                card.config = by_id[ident]

    async def persist_bridge(self, data, updates=None):
        async with self.config_lock:
            config = deepcopy(self.settings.data)
            if updates is None:
                config["bridge"] = deepcopy(data)
            else:
                for card in config["bridge"]["cards"]:
                    for key,value in updates.get(card["id"],{}).items():
                        if key not in ("saved_range","saved_divider"):
                            raise ValueError("TCP 不允许保存该配置字段")
                        card[key] = value
            saved = await asyncio.to_thread(self.settings.save,config,config["revision"])
            self.set_bridge_configuration(saved["bridge"])

    async def save_bridge(self,data,base=None,revision=None):
        async with self.config_lock:
            if base is not None:
                data = merge_cards(self.settings.data["bridge"],base,data)
            elif revision is not None and revision != self.settings.data["revision"]:
                raise ValueError("配置已更新，请重新读取后保存")
            topology = self.status if self.status.get("fingerprint") else self.settings.data["topology"]
            checked = validate_config(data,topology["channels"],topology["fingerprint"],
                                      await asyncio.to_thread(local_addresses))
            active = {c["id"]:c for c in checked["cards"]}
            if any(active.get(ident) != card.config for ident,card in self.bridge.cards.items()):
                raise ValueError("请先停止受影响的虚拟卡再修改配置")
            config = deepcopy(self.settings.data)
            config["bridge"] = checked
            saved = await asyncio.to_thread(self.settings.save,config,config["revision"])
            self.set_bridge_configuration(saved["bridge"])
            return deepcopy(saved["bridge"])


    def settings_plan(self, changes):
        status = dict(self.status,bridge=self.bridge.status())
        return make_plan(self.settings.data,changes,status)

    async def apply_settings(self, changes, approved):
        async with self.config_lock:
            plan = self.settings_plan(changes)
            wanted = [(c["path"],c["value"]) for c in plan["changes"]]
            if not isinstance(approved,list) or any(not isinstance(c,dict) or not isinstance(c.get("path"),list) for c in approved):
                raise SettingError("应用方案格式无效")
            accepted = [(c.get("path"),c.get("value")) for c in approved]
            if wanted != accepted:
                raise SettingError("关联变化已更新，请重新查看应用方案", [c["path"] for c in plan["changes"]])
            for change in approved:
                actual = get_value(self.settings.data,change["path"])
                if actual != change.get("base") and actual != change["value"]:
                    raise SettingError("关联字段已被更新，请重新查看", [change["path"]])
            if plan["noop"]:
                return dict(config=deepcopy(self.settings.data),noop=True)
            if self.operation and self.operation.get("state")=="running":
                raise SettingError("请等待当前连接或参数操作完成", [c["path"] for c in changes])
            config, before = plan["config"],deepcopy(self.settings.data)
            generation = self.generation
            touched_worker = False
            login_changed = config["startup"]["login"] != before["startup"]["login"]
            operation = dict(id=secrets.token_hex(8),state="running",message="正在应用所选参数",
                             paths=[c["path"] for c in plan["changes"]])
            # 普通字段仅由调用行显示进度，避免启动开关触发全局连接提示。
            if plan["mode"] == "reconnect":
                self.operation = operation
            persistence_only = all(c["path"][0] == "startup" or c["path"][:2] == ["bridge","cards"]
                                   for c in plan["changes"])
            try:
                if plan["mode"]=="reconnect":
                    self.critical_zero()
                    generation = self.generation
                    operation["message"] = "停止发布，确认全路零输出"
                    await self.bridge.stop("参数应用：清零并重连")
                    await self.bridge_request("zero",{})
                    if generation != self.generation:
                        raise ValueError("人工清零已优先执行，本次参数应用取消")
                    await self.bridge_request("disconnect",{})
                    self.record_stop()
                    operation["message"] = "应用硬件参数并重新建立零输出连接"
                    touched_worker = True
                    await self.bridge_request("configure",dict(config=config))
                    result = await self.bridge_request("connect",dict(interface=config["hardware"]["interface"]))
                    self.status.update(result or {})
                elif persistence_only:
                    pass
                elif plan["mode"]=="live":
                    touched_worker = True
                    result = await self.bridge_request("runtime_config",dict(config=config))
                    self.status.update(result or {})
                else:
                    touched_worker = True
                    result = await self.bridge_request("configure",dict(config=config))
                    self.status.update(result or {})
                if generation != self.generation:
                    raise ValueError("人工清零已优先执行，本次参数未保存")
                if login_changed:
                    await asyncio.to_thread(set_login,config["startup"]["login"])
                saved = await asyncio.to_thread(self.settings.save,config,before["revision"])
                self.set_bridge_configuration(saved["bridge"])
                operation.update(state="done",message="参数已应用并回读" if plan["mode"]=="reconnect" else "参数已保存")
                self.journal.log("control","逐项应用参数",paths=operation["paths"],mode=plan["mode"],config_revision=saved["revision"])
                return dict(config=saved,noop=False,mode=plan["mode"])
            except Exception as exc:
                if touched_worker:
                    try:
                        if plan["mode"]=="reconnect":
                            await self.bridge_request("disconnect",{})
                            await self.bridge_request("configure",dict(config=before))
                        elif generation==self.generation:
                            await self.bridge_request("runtime_config" if self.status.get("connection")=="OP" else "configure",dict(config=before))
                    except Exception as rollback:
                        self.journal.log("runtime","参数回退无法确认","error",reason=str(rollback))
                if login_changed:
                    try:
                        await asyncio.to_thread(set_login,before["startup"]["login"])
                    except Exception as rollback:
                        self.journal.log("control","登录启动项回退无法确认","error",reason=str(rollback))
                operation.update(state="failed",message=str(exc))
                self.journal.log("control","参数应用失败","error",paths=operation["paths"],reason=str(exc))
                raise SettingError(str(exc),operation["paths"]) from exc

    async def save_config(self,data,revision):
        async with self.config_lock:
            config = validate(data)
            if revision != self.settings.data["revision"]:
                raise ValueError("配置已更新，请重新读取后保存")
            if config == self.settings.data:
                return deepcopy(self.settings.data)
            topology = config["topology"]
            if config["bridge"].get("cards"):
                config["bridge"] = validate_config(config["bridge"],topology["channels"],topology["fingerprint"],
                                                   await asyncio.to_thread(local_addresses))
            if self.bridge.active:
                by_id = {c["id"]:c for c in config["bridge"]["cards"]}
                if any(by_id.get(i) != c.config for i,c in self.bridge.cards.items()):
                    raise ValueError("请先停止受影响的虚拟卡再保存")
            hardware_changed = any(config[k] != self.settings.data[k] for k in ("hardware","modules","faults","channels"))
            if hardware_changed:
                if self.status.get("connection") not in ("DISCONNECTED","FAULT") or self.status.get("desired_connected"):
                    raise ValueError("硬件、校准或故障配置已改变，请先断开接口再保存应用")
                await self.bridge_request("configure",dict(config=config))
            old_login = self.settings.data["startup"]["login"]
            if config["startup"]["login"] != old_login:
                await asyncio.to_thread(set_login,config["startup"]["login"])
            try:
                saved = await asyncio.to_thread(self.settings.save,config,revision)
            except Exception:
                if hardware_changed:
                    await self.bridge_request("configure",dict(config=self.settings.data))
                if config["startup"]["login"] != old_login:
                    await asyncio.to_thread(set_login,old_login)
                raise
            self.set_bridge_configuration(saved["bridge"])
            self.journal.log("control","配置已保存",revision=saved["revision"])
            return saved

    def detach(self,token,queue=None):
        if queue is not None and self.clients.get(token) is not queue:
            return
        self.clients.pop(token,None)
        self.viewer_drops.pop(token,None)
        if self.controller == token:
            self.controller = None
            self.heartbeat()
        if not self.clients and hasattr(self,"settings"):
            asyncio.create_task(self.presence(False))

    async def presence(self,present):
        try:
            await self.bridge_request("web_presence",dict(present=present))
        except (ValueError,HTTPException):
            pass

    def record_start(self):
        if self.recording:
            raise ValueError("正在记录")
        folder = data_root()/"recordings"
        folder.mkdir(parents=True,exist_ok=True)
        target = folder/f"live-{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
        nai = len(self.status.get("channels",{}).get("ai",[0]*4))
        nao = len(self.status.get("ao",[0]*4))
        with target.open("x",encoding="utf-8-sig",newline="") as stream:
            csv.writer(stream).writerow(["seq","monotonic_ns","mode","wkc","expected_wkc",
                *[f"{field}{i}" for field in ("raw","ai_v","valid","flags") for i in range(1,nai+1)],
                *[f"ao_v{i}" for i in range(1,nao+1)],"ao_valid","target_period_ms","interval_ms","roundtrip_ms",
                "host_send_ns","host_return_ns",*[f"ai_engineering{i}" for i in range(1,nai+1)],
                *[f"ao_engineering{i}" for i in range(1,nao+1)]])
        self.recording,self.record_error = target,None

    def record_stop(self):
        self.last_recording,self.recording = self.recording or self.last_recording,None

    def status_for(self,token):
        return dict(self.status,app_version=__version__,controller=self.controller==token,controller_busy=bool(self.controller),
                    recording=self.recording is not None,export=self.last_recording is not None,record_error=self.record_error,
                    timing=self.timing_summary,viewer_drops=self.viewer_drops.get(token,0),bridge=self.bridge.status(),
                    config_revision=self.settings.data["revision"],config_error=self.settings.error,
                    channel_profiles=self.settings.data["channels"],operation=self.operation,nic=self.nic,
                    events=[{k:e[k] for k in ("id","time_ms","time","category","severity","message")} for e in list(self.journal.events)[-30:]],journal_error=self.journal.error,manual_lock=self.manual_lock)

    async def begin_connect(self,interface,recover=False):
        if self.operation_task and not self.operation_task.done():
            raise ValueError("接口操作正在进行，请等待完成")
        if self.status.get("connection") == "OP" and self.status.get("interface") == interface:
            return
        ident = secrets.token_hex(8)
        self.operation = dict(id=ident,state="running",message="正在搜索从站、应用配置并建立零输出连接")
        self.status.update(connection="CONNECTING",reason=self.operation["message"])
        async def run():
            try:
                if self.bridge.active and not recover:
                    await self.bridge.stop("更换接口")
                await self.bridge_request("disconnect",{})
                await self.bridge_request("configure",dict(config=self.settings.data))
                async with self.config_lock:
                    config = deepcopy(self.settings.data)
                    config["hardware"]["interface"] = interface
                    await asyncio.to_thread(self.settings.save,config,config["revision"])
                await self.bridge_request("connect",dict(interface=interface))
                self.operation.update(state="done",message="连接已建立，等待通信稳定")
            except Exception as exc:
                self.operation.update(state="failed",message=str(getattr(exc,"detail",None) or str(exc) or type(exc).__name__))
        self.operation_task = asyncio.create_task(run())

    async def source_pump(self):
        updated = time.monotonic()
        while True:
            self.heartbeat()
            for _ in range(64):
                try:
                    packet = self.source.get_nowait()
                except Empty:
                    break
                updated = time.monotonic()
                packet["status"]["channel_profiles"] = self.settings.data["channels"]
                self.bridge.feed(packet["status"],packet["samples"])
            if self.bridge.active and self.worker.is_alive() and time.monotonic()-updated > 1:
                self.bridge.fault("通信进程原始数据过期")
            await asyncio.sleep(.005)

    async def supervise(self):
        self.heartbeat()
        await self.bridge_request("configure",dict(config=self.settings.data))
        if self.settings.data["startup"]["auto_connect"] and not self.settings.error:
            interface = self.settings.data["hardware"]["interface"]
            if interface:
                self.publish_intent = [c["id"] for c in self.settings.data["bridge"]["cards"] if c["enabled"] and c.get("start_on_boot")]
                await self.begin_connect(interface)
        retry_at = 0
        while True:
            if not self.worker.is_alive() and not self.stop.is_set():
                if not retry_at:
                    retry_at = time.monotonic()+1
                    self.journal.log("runtime","通信进程退出，等待重新启动","error",exitcode=self.worker.exitcode)
                if time.monotonic() >= retry_at:
                    self.bridge.suspended = self.bridge.active
                    desired = self.status.get("desired_connected",False)
                    for future in self.pending.values():
                        if not future.done():
                            future.set_result(dict(ok=False,error="通信进程重启，旧命令已撤销"))
                    self.start_worker()
                    self.heartbeat()
                    await self.bridge_request("configure",dict(config=self.settings.data))
                    if desired and not self.manual_lock:
                        await self.begin_connect(self.settings.data["hardware"]["interface"],recover=True)
                    retry_at = 0
            if self.status.get("ready") and not self.manual_lock:
                try:
                    if self.bridge.suspended:
                        await self.bridge.resume(self.status)
                    elif self.publish_intent:
                        selected,self.publish_intent = list(self.publish_intent),False
                        generation = self.generation
                        for ident in selected:
                            if self.manual_lock or generation != self.generation:
                                break
                            source = next((c for c in self.settings.data["bridge"]["cards"] if c["id"]==ident),None)
                            try:
                                await self.bridge.start(self.status,ident)
                                if source and source.get("authorize_on_boot") and not self.manual_lock:
                                    await self.bridge.authorize(ident)
                            except (ValueError,OSError,HTTPException) as exc:
                                self.bridge.last_error = str(exc)
                                self.journal.log("connection",f"虚拟卡 {ident} 冷启动失败：{exc}","error")
                except (ValueError,OSError,HTTPException) as exc:
                    self.publish_intent = False
                    self.bridge.last_error = str(exc)
                    self.journal.log("connection",f"桥接自动恢复失败：{exc}","error")
            if self.status.get("fingerprint") and self.settings.data["topology"]["fingerprint"] != self.status["fingerprint"]:
                async with self.config_lock:
                    config = deepcopy(self.settings.data)
                    config["topology"] = {k:self.status[k] for k in ("channels","fingerprint","devices")}
                    await asyncio.to_thread(self.settings.save,config,config["revision"])
            if time.monotonic()-self.last_nic > 5:
                self.last_nic = time.monotonic()
                try:
                    self.adapter_cache = await asyncio.to_thread(adapters)
                    self.nic = dict(time_ms=time.time()*1000,items=self.adapter_cache["items"],error=self.adapter_cache.get("error"))
                    interface = self.status.get("interface")
                    item = next((i for i in self.adapter_cache["items"] if i.get("interface")==interface),None)
                    if item and item.get("status") != "Up" and self.status.get("connection") == "OP":
                        await self.bridge_request("link_fault",dict(reason="网卡链路已断开"))
                except Exception as exc:
                    self.nic = dict(time_ms=time.time()*1000,error=str(exc))
            await asyncio.sleep(.25)

    async def pump(self):
        while True:
            for _ in range(64):
                try:
                    answer = self.events.get_nowait()
                except Empty:
                    break
                if answer.get("kind") == "diagnostic":
                    details = answer.get("details",{})
                    stamp = time.time()*1000
                    fault = answer["severity"] in ("warning","error")
                    clip = dict(before=self.history.query(stamp-5000,stamp,2000)) if fault else {}
                    record = self.journal.log(answer["category"],answer["message"],answer["severity"],nic=self.nic,
                                              fragment_state="collecting" if fault else "none",**clip,**details)
                    if fault:
                        self.fault_clips.append((stamp+5000,record["event_id"],self.journal.generation))
                    continue
                future = self.pending.get(answer["id"])
                if future is not None and not future.done():
                    future.set_result(answer)
            batches = []
            for _ in range(16):
                try:
                    batches.append(self.telemetry.get_nowait())
                except Empty:
                    break
            collected = []
            for packet in batches:
                self.status = packet["status"]
                if self.status["session"] != self.connection_session:
                    self.connection_session = self.status["session"]
                    self.recent.clear()
                    self.timing = TimingWindow()
                    self.last_summary = 0
                collected.extend(packet["samples"])
            now = time.monotonic()
            if batches:
                self.last_update = now
            if collected:
                self.timing.add(s for s in collected if s["session"]==self.connection_session)
                self.history.add(collected)
                if now-self.last_summary >= .25:
                    self.timing_summary = self.timing.summarize()
                    self.last_summary = now
                self.recent.extend(collected)
                alarm = tuple(tuple(sorted((k,str(v)) for k,v in (f or {}).items() if v and k in ("underrange","overrange","error","sync_error","invalid"))) for f in collected[-1]["flags"])
                if alarm != self.alarm_key:
                    if self.alarm_key is not None or any(alarm):
                        self.journal.log("alarm","通道报警状态变化" if any(alarm) else "通道报警已恢复",
                                         "warning" if any(alarm) else "info",channels=alarm,session=self.connection_session,
                                         previous_samples=self.alarm_samples,previous_duration_ms=(now-self.alarm_started)*1000 if self.alarm_started else 0)
                    self.alarm_key = alarm
                    self.alarm_started = now if any(alarm) else 0
                    self.alarm_samples = 0
                if any(alarm):
                    self.alarm_samples += len(collected)
                if self.recording:
                    try:
                        await asyncio.to_thread(append_recording,self.recording,csv_rows(collected))
                    except OSError as exc:
                        self.record_error = str(exc)
                        self.record_stop()
            if not self.worker.is_alive():
                self.status.update(connection="FAULT",health="FAULT",armed=False,ready=False,
                                   reason="EtherCAT 工作进程已退出",zero="输出状态无法确认")
            elif self.status["connection"]=="OP" and self.last_update and now-self.last_update > 1 and not (self.operation and self.operation["state"]=="running"):
                self.status.update(connection="STALE",health="FAULT",ready=False,reason="等待通信进程更新",zero="输出状态无法确认")
            self.console.update(self.status)
            if self.status["connection"] not in ("OP","CONNECTING") and self.recording:
                self.record_stop()
            for stamp,ident,generation in list(self.fault_clips):
                if time.time()*1000 >= stamp:
                    self.journal.update(ident,generation,fragment_state="complete",after=self.history.query(stamp-5000,stamp,2000))
                    self.fault_clips.remove((stamp,ident,generation))
            if batches or now-self.last_broadcast >= .25:
                self.last_broadcast = now
                for token,queue in tuple(self.clients.items()):
                    if queue.full():
                        self.viewer_drops[token] += len(queue.get_nowait()["samples"])
                    queue.put_nowait(dict(kind="samples",status=self.status_for(token),samples=collected))
            await asyncio.sleep(.01)


def append_recording(path,content):
    with path.open("a",encoding="utf-8",newline="") as stream:
        stream.write(content)


@asynccontextmanager
async def lifespan(app):
    service = app.state.service = Service()
    tasks = [asyncio.create_task(f()) for f in (service.pump,service.source_pump,service.supervise)]
    try:
        yield
    finally:
        service.stop.set()
        if service.operation_task:
            service.operation_task.cancel()
        await service.bridge.stop("服务退出",shutdown=True)
        console_message("正在停止服务，清理 EtherCAT 连接…")
        await asyncio.to_thread(service.worker.join,2.5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for stamp,ident,generation in service.fault_clips:
            service.journal.update(ident,generation,fragment_state="incomplete",
                                   after=service.history.query(stamp-5000,time.time()*1000,2000))
        if service.worker.is_alive():
            log_stop("worker_stop_timeout",worker_pid=service.worker.pid,zero="unknown")
            service.worker.terminate()
            await asyncio.to_thread(service.worker.join,.25)
        elif not service.cleanup_done.is_set():
            log_stop("worker_stop_failed",worker_pid=service.worker.pid,zero="unknown",exitcode=service.worker.exitcode)
        await asyncio.to_thread(service.journal.close)


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1"])
app.mount("/static",StaticFiles(directory=STATIC),name="static")

@app.middleware("http")
async def revalidate_ui(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/")
async def index():
    return FileResponse(STATIC/"index.html")

@app.get("/api/status")
async def status(request:Request):
    return request.app.state.service.status_for(request.headers.get("x-viewer-session",""))

async def worker_request(service,action,args,generation=0):
    if not service.worker.is_alive():
        raise HTTPException(503,"工作进程已退出")
    ident = secrets.token_hex(12)
    future = service.pending[ident] = asyncio.get_running_loop().create_future()
    try:
        service.commands.put_nowait(dict(id=ident,action=action,args=args,owner=generation))
        return await asyncio.wait_for(future,timeout=15)
    except Full:
        raise HTTPException(503,"工作进程繁忙")
    except TimeoutError:
        raise HTTPException(504,"等待通信进程超时")
    finally:
        service.pending.pop(ident,None)

@app.get("/api/adapters")
async def get_adapters(request:Request):
    service = request.app.state.service
    service.adapter_cache = await asyncio.to_thread(adapters)
    return service.adapter_cache

@app.get("/api/bridge/network")
async def bridge_network():
    return {"addresses":await asyncio.to_thread(local_addresses)}

@app.get("/api/config")
async def config(request:Request):
    service = request.app.state.service
    return dict(config=service.settings.data,error=service.settings.error,login=await asyncio.to_thread(login_status),
                path=str(service.settings.path),backup=str(service.settings.path.with_suffix(".backup.json")),
                readback=service.status.get("parameter_readback",{}),capabilities=service.status.get("hardware_details",[]))

@app.get("/api/diagnostics/history")
async def history(request:Request,start:float|None=None,end:float|None=None,limit:int=4000):
    return request.app.state.service.history.query(start,end,limit)

@app.get("/api/events")
async def events(request:Request,category:str="",after:int=0):
    journal = request.app.state.service.journal
    return dict(events=journal.list(category,after),generation=journal.generation,error=journal.error,dropped=journal.dropped)

@app.get("/api/events/{event_id}")
async def event_detail(event_id:str,request:Request,generation:str|None=None):
    journal = request.app.state.service.journal
    generation = generation or journal.generation
    try:
        detail = await asyncio.to_thread(journal.detail,event_id,generation)
        return dict(event=detail,generation=generation)
    except KeyError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503,str(exc)) from exc

@app.get("/api/diagnostics/export")
async def export_diagnostics(request:Request):
    service = request.app.state.service
    output = io.BytesIO()
    def build():
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as z:
            z.writestr("诊断状态.json",json.dumps(service.status_for(""),ensure_ascii=False,indent=2))
            z.writestr("诊断历史.json",json.dumps(service.history.query(time.time()*1000-1800000,limit=20000),ensure_ascii=False))
            with service.journal.lock:
                for p in service.journal.folder.glob("events*.jsonl"):
                    z.write(p,"日志/"+p.name)
        return output.getvalue()
    return Response(await asyncio.to_thread(build),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=ethercat-diagnostics.zip"})

@app.post("/api/commands")
async def commands(command:Command,request:Request):
    service = request.app.state.service
    token = request.headers.get("x-viewer-session")
    if request.headers.get("origin") != f"http://{request.headers.get('host')}" or token not in service.clients:
        raise HTTPException(403,"请求需要同源浏览器会话")
    action,args = command.action,command.args
    if action == "claim":
        if service.controller not in (None,token) and not args.get("takeover"):
            raise HTTPException(409,"另一页面正在管理，可显式切换到本页")
        service.controller = token
        return dict(id=command.id,ok=True,result=service.status_for(token))
    if service.controller != token:
        raise HTTPException(403,"本页为只读，请先切换管理到本页")
    key = (token,command.id)
    if key in service.command_results:
        return service.command_results[key]
    service.heartbeat()
    try:
        extra = {}
        if action == "preview_settings":
            plan = service.settings_plan(args["changes"])
            return dict(id=command.id,ok=True,plan={k:v for k,v in plan.items() if k!="config"})
        elif action == "apply_settings":
            extra.update(await service.apply_settings(args["changes"],args.get("approved")))
        elif action == "open_config_directory":
            await asyncio.to_thread(open_config_directory)
        elif action == "clear_diagnostics":
            # 不接收路径参数，也不清空连续诊断历史、CSV 或退出日志。
            extra["diagnostics"] = await asyncio.to_thread(service.journal.clear)
            service.fault_clips.clear()
        elif action == "set_period":
            changes = [dict(path=["hardware","period_ms"],base=service.settings.data["hardware"]["period_ms"],value=args["period_ms"])]
            plan = service.settings_plan(changes)
            if plan["mode"]=="reconnect":
                raise ValueError("该修改需要清零重连，请使用逐项应用入口")
            extra.update(await service.apply_settings(changes,plan["changes"]))
        elif action == "config_save":
            extra["config"] = await service.save_config(args["config"],args["revision"])
        elif action == "calibration_two_point":
            extra["calibration"] = two_point(*(args[k] for k in ("x1","y1","x2","y2")))
        elif action == "connect":
            service.manual_lock = False
            await service.begin_connect(args["interface"])
        elif action == "bridge_preset":
            extra["config"] = service.bridge.make_preset(args["profile"],service.status)
        elif action == "bridge_save":
            extra["config"] = await service.save_bridge(args["config"],args.get("base_config"),args.get("revision"))
        elif action == "bridge_start":
            service.manual_lock = False
            await service.bridge.start(service.status,args.get("card"))
        elif action == "bridge_authorize":
            service.manual_lock = False
            await service.bridge.authorize(args["card"])
        elif action == "bridge_stop":
            if args.get("card"):
                await service.bridge.stop_card(args["card"])
            else:
                await service.bridge.stop()
                service.publish_intent = False
        else:
            if action in ("zero","disconnect"):
                service.critical_zero()
                await service.bridge.stop("全部清零并锁定")
            if action in ("record_start","record_stop"):
                if action=="record_start" and service.status.get("connection")!="OP":
                    raise ValueError("请先连接实机")
                getattr(service,action)()
            else:
                if action=="disconnect":
                    service.record_stop()
                    if service.operation_task:
                        service.operation_task.cancel()
                if action=="set_output" and "engineering" in args:
                    rows = service.status.get("channels",{}).get("ao",[])
                    ch = args.get("channel")
                    if type(ch) is not int or not 1<=ch<=len(rows):
                        raise ValueError("通道无效")
                    args["voltage"] = engineering_voltage(args["engineering"],service.settings.data["channels"].get(rows[ch-1]["id"],{}))
                answer = await worker_request(service,action,args,service.generation)
                if not answer["ok"]:
                    raise ValueError(answer["error"])
                service.status.update(answer.get("result") or {})
                if action=="set_period":
                    async with service.config_lock:
                        data = deepcopy(service.settings.data)
                        data["hardware"]["period_ms"] = service.status["period_ms"]
                        await asyncio.to_thread(service.settings.save,data,data["revision"])
                if action=="arm":
                    service.manual_lock = False
        if action not in ("claim","calibration_two_point","clear_diagnostics","apply_settings"):
            labels = {"connect":"连接接口","disconnect":"断开接口","zero":"全部清零并锁定","arm":"允许网页控制","lock_channel":"清零并锁定通道","set_output":"应用输出目标","wave":"启动波形","stop_wave":"停波清零","record_start":"开始记录","record_stop":"停止记录","bridge_save":"保存虚拟卡配置","bridge_start":"开始监听虚拟卡","bridge_stop":"停止虚拟卡","bridge_authorize":"授权 TCP 控制","apply_settings":"应用参数","config_save":"保存配置","open_config_directory":"打开配置文件夹"}
            service.journal.log("control",labels.get(action,"管理操作"),operation=action,channels=args.get("channels"))
        result = dict(id=command.id,ok=True,result=service.status_for(token),**extra)
        service.command_results[key] = result
        if len(service.command_results)>256:
            service.command_results.pop(next(iter(service.command_results)))
        return result
    except SettingError as exc:
        raise HTTPException(409,dict(message=str(exc),paths=exc.paths)) from exc
    except (ValueError,OSError,KeyError) as exc:
        raise HTTPException(409,str(exc)) from exc

@app.get("/api/recording")
async def recording(request:Request):
    path = request.app.state.service.last_recording
    if not path or not path.is_file():
        raise HTTPException(404,"尚无已完成的记录")
    return FileResponse(path,filename=path.name,media_type="text/csv")

def session_token(resume, known, clients, hiding):
    return resume if resume in known and (resume not in clients or resume in hiding) else secrets.token_urlsafe(24)


@app.websocket("/ws")
async def websocket(ws:WebSocket):
    if ws.headers.get("origin") != f"http://{ws.headers.get('host')}":
        await ws.close(code=1008)
        return
    service = ws.app.state.service
    await ws.accept()
    resume = ws.query_params.get("resume")
    token = session_token(resume, service.known_sessions, service.clients, service.page_hiding)
    service.page_hiding.discard(token)
    service.known_sessions.add(token)
    if len(service.known_sessions)>128:
        service.known_sessions.intersection_update(set(service.clients)|{token})
    queue = service.clients[token] = asyncio.Queue(maxsize=2)
    service.viewer_drops[token] = 0
    if service.controller is None:
        service.controller = token
    asyncio.create_task(service.presence(True))
    async def receive():
        while True:
            message = await ws.receive_text()
            if message == "pagehide":
                service.page_hiding.add(token)
    async def send():
        while service.clients.get(token) is queue:
            await ws.send_json(await queue.get())
    tasks = []
    try:
        await ws.send_json(dict(kind="hello",token=token,status=service.status_for(token),samples=list(service.recent)))
        tasks = [asyncio.create_task(receive()),asyncio.create_task(send())]
        await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect,RuntimeError):
        pass
    finally:
        service.detach(token,queue)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
