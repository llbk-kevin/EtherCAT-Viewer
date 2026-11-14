import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
import csv
import io
import json
import multiprocessing as mp
from pathlib import Path
from queue import Empty, Full
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .console import BusConsole, console_message, log_stop
from .generator import DEFAULT_PERIOD_MS
from .paths import data_root
from .timing import TimingWindow
from .worker import run_worker

STATIC = Path(__file__).resolve().parent / "static"

class Command(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    action: str
    args: dict = Field(default_factory=dict)

def csv_rows(samples):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for sample in samples:
        writer.writerow([sample["seq"], sample["time_ns"], sample["mode"], sample["wkc"],
                         sample["expected_wkc"], *sample["raw"], *sample["ai"],
                         *sample["valid"], *(json.dumps(f) for f in sample["flags"]), *sample["ao"], sample["ao_valid"],
                         sample["period_ms"], sample["interval_ms"], sample["roundtrip_ms"]])
    return stream.getvalue()

class Service:
    def __init__(self):
        ctx = mp.get_context("spawn")
        self.commands, self.events, self.telemetry = ctx.Queue(8), ctx.Queue(32), ctx.Queue(16)
        self.lease, self.stop = ctx.Array("q", 2), ctx.Event()
        self.cleanup_done = ctx.Event()
        self.worker = ctx.Process(target=run_worker, args=(self.commands, self.events, self.telemetry,
                                                          self.lease, self.stop, self.cleanup_done))
        self.status = {"mode": "LIVE", "connection": "DISCONNECTED", "period_ms": DEFAULT_PERIOD_MS}
        self.console = BusConsole(self.status)
        self.timing = TimingWindow()
        self.timing_summary = self.timing.summarize()
        self.last_summary = 0
        self.connection_session = 0
        self.clients = {}
        self.viewer_drops = {}
        self.controller = None
        self.generation = 0
        self.pending = {}
        self.recording = None
        self.last_recording = None
        self.record_error = None
        self.adapter_cache = None
        self.recent = deque(maxlen=3000)
        self.last_update = 0
        self.worker.start()

    def heartbeat(self):
        with self.lease.get_lock():
            self.lease[:] = [self.generation, time.perf_counter_ns()] if self.controller else [0, 0]

    def detach(self, token):
        self.clients.pop(token, None)
        self.viewer_drops.pop(token, None)
        if self.controller == token:
            self.controller = None
            self.heartbeat()

    def record_start(self):
        if self.recording:
            raise ValueError("正在记录")
        folder = data_root() / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{self.status['mode'].lower()}-{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
        target = folder / name
        self.record_error = None
        with target.open("x", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerow(["seq", "monotonic_ns", "mode", "wkc", "expected_wkc",
                                        *[f"{field}{i}" for field in ("raw", "ai_v", "valid", "flags", "ao_v")
                                          for i in range(1, 5)],
                                        "ao_valid", "target_period_ms", "interval_ms", "roundtrip_ms"])
        self.recording = target

    def record_stop(self):
        self.last_recording, self.recording = self.recording or self.last_recording, None

    def status_for(self, token):
        return dict(self.status, controller=self.controller == token, controller_busy=bool(self.controller),
                    recording=self.recording is not None, export=self.last_recording is not None,
                    record_error=self.record_error, timing=self.timing_summary,
                    viewer_drops=self.viewer_drops.get(token, 0))

    async def pump(self):
        while True:
            for _ in range(32):
                try:
                    answer = self.events.get_nowait()
                except Empty:
                    break
                future = self.pending.get(answer["id"])
                if future is not None and not future.done():
                    future.set_result(answer)
            batches = []
            for _ in range(16):
                try:
                    packet = self.telemetry.get_nowait()
                except Empty:
                    break
                batches.append(packet)
            collected = []
            for packet in batches:
                self.status = packet["status"]
                if self.status["session"] != self.connection_session:
                    self.connection_session = self.status["session"]
                    self.timing = TimingWindow()
                    self.timing_summary = self.timing.summarize()
                    self.last_summary = 0
                collected.extend(packet["samples"])
            now = time.monotonic()
            if batches:
                self.last_update = now
            if collected:
                self.timing.add(s for s in collected if s["session"] == self.connection_session)
                if now - self.last_summary >= 0.25 or self.status["connection"] != "OP":
                    self.timing_summary = self.timing.summarize()
                    self.last_summary = now
                self.recent.extend(collected)
                if self.recording:
                    try:
                        await asyncio.to_thread(append_recording, self.recording, csv_rows(collected))
                    except OSError as exc:
                        self.record_error = str(exc)
                        self.record_stop()
            if not self.worker.is_alive():
                self.status.update(connection="FAULT", armed=False, ready=False,
                                   reason="EtherCAT 工作进程已退出", zero="输出状态未知")
            elif (self.status["connection"] == "OP" and self.last_update
                  and time.monotonic() - self.last_update > 1):
                self.status.update(connection="STALE", armed=False, ready=False,
                                   reason="等待通信进程更新", zero="输出状态未知")
            self.console.update(self.status)
            if self.status["connection"] != "OP" and self.recording:
                self.record_stop()
            if batches or (self.status["connection"] in ("STALE", "FAULT")
                           and now - self.last_summary >= 0.25):
                if not batches:
                    self.last_summary = now
                for token, queue in tuple(self.clients.items()):
                    if queue.full():
                        skipped = queue.get_nowait()["samples"]
                        self.viewer_drops[token] += len(skipped)
                    queue.put_nowait(dict(kind="samples", status=self.status_for(token), samples=collected))
            await asyncio.sleep(0.01)

def append_recording(path, content):
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(content)

@asynccontextmanager
async def lifespan(app):
    service = app.state.service = Service()
    pump = asyncio.create_task(service.pump())
    try:
        yield
    finally:
        service.stop.set()
        console_message("正在停止服务，清理 EtherCAT 连接…")
        await asyncio.to_thread(service.worker.join, 2.5)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if service.worker.is_alive():
            log_stop("worker_stop_timeout", worker_pid=service.worker.pid, zero="unknown")
            service.worker.terminate()
            await asyncio.to_thread(service.worker.join, 0.25)
        elif not service.cleanup_done.is_set():
            log_stop("worker_stop_failed", worker_pid=service.worker.pid, zero="unknown",
                     exitcode=service.worker.exitcode)

app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

@app.get("/api/status")
async def status(request: Request):
    return request.app.state.service.status_for(request.headers.get("x-viewer-session", ""))

async def worker_request(service, action, args, generation=0):
    if not service.worker.is_alive():
        raise HTTPException(503, "工作进程已退出")
    ident = secrets.token_hex(12)
    loop = asyncio.get_running_loop()
    future = service.pending[ident] = loop.create_future()
    try:
        service.commands.put_nowait(dict(id=ident, action=action, args=args, owner=generation))
        return await asyncio.wait_for(future, timeout=15)
    except Full:
        raise HTTPException(503, "工作进程繁忙")
    except TimeoutError:
        raise HTTPException(504, "等待通信进程超时")
    finally:
        service.pending.pop(ident, None)

@app.get("/api/adapters")
async def get_adapters(request: Request):
    service = request.app.state.service
    if service.status["connection"] == "OP" and service.adapter_cache is not None:
        return service.adapter_cache
    answer = await worker_request(service, "adapters", {})
    if not answer["ok"]:
        raise HTTPException(409, answer["error"])
    service.adapter_cache = answer["result"]
    return service.adapter_cache

@app.post("/api/commands")
async def commands(command: Command, request: Request):
    service = request.app.state.service
    token = request.headers.get("x-viewer-session")
    if request.headers.get("origin") != f"http://{request.headers.get('host')}" or token not in service.clients:
        raise HTTPException(403, "请求需要同源浏览器会话")
    action = command.action
    if action == "claim":
        if service.controller not in (None, token):
            raise HTTPException(409, "另一浏览器正在控制")
        if service.controller is None:
            service.generation += 1
        service.controller = token
        service.heartbeat()
        return dict(id=command.id, ok=True, result=service.status_for(token))
    if service.controller != token:
        raise HTTPException(403, "请先取得控制权")
    if action in ("record_start", "record_stop"):
        if action == "record_start" and service.status["connection"] != "OP":
            raise HTTPException(409, "请先连接实机")
        try:
            (service.record_start if action == "record_start" else service.record_stop)()
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return dict(id=command.id, ok=True, result=service.status_for(token))
    if action in ("connect", "disconnect"):
        service.record_stop()
    answer = await worker_request(service, action, command.args, service.generation)
    answer["id"] = command.id
    return answer

@app.get("/api/recording")
async def recording(request: Request):
    path = request.app.state.service.last_recording
    if not path or not path.is_file():
        raise HTTPException(404, "尚无已完成的记录")
    return FileResponse(path, filename=path.name, media_type="text/csv")

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    if ws.headers.get("origin") != f"http://{ws.headers.get('host')}":
        await ws.close(code=1008)
        return
    service = ws.app.state.service
    await ws.accept()
    token = secrets.token_urlsafe(24)
    queue = service.clients[token] = asyncio.Queue(maxsize=2)
    service.viewer_drops[token] = 0

    async def receive():
        while True:
            if await ws.receive_text() == "heartbeat" and service.controller == token:
                service.heartbeat()

    async def send():
        while True:
            await ws.send_json(await queue.get())

    tasks = []
    try:
        await ws.send_json(dict(kind="hello", token=token, status=service.status_for(token),
                                samples=list(service.recent)))
        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        service.detach(token)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
