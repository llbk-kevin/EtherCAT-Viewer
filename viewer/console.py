import ctypes
from datetime import datetime
import json
import os
import sys
from queue import Queue, Full
from threading import Event, Thread, Lock

from .paths import data_root

LOG_PATH = data_root() / "logs" / "backend-shutdown.jsonl"
CLOSE_WAIT_SECONDS = 4
CTRL_C_EVENT = 0
CTRL_CLOSE_EVENT = 2


def log_stop(event, **fields):
    record = dict(time=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  event=event, pid=os.getpid(), **fields)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with LOG_PATH.open("ab", buffering=0) as stream:
        stream.write(data)
        os.fsync(stream.fileno())


_console_queue = Queue(maxsize=256)
_console_thread = None
_console_lock = Lock()


def _console_writer():
    while True:
        message = _console_queue.get()
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass


def console_message(message):
    # Windows Mark/QuickEdit 可阻塞 WriteConsole，不允许拖住 Web 事件循环。
    global _console_thread
    if os.name == "nt" and sys.stdout is not None and sys.stdout.isatty():
        with _console_lock:
            if _console_thread is None:
                _console_thread = Thread(target=_console_writer,daemon=True,name="console-output")
                _console_thread.start()
        try:
            _console_queue.put_nowait(str(message))
        except Full:
            pass
    else:
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass


class BusConsole:
    def __init__(self, status):
        self.connection = status["connection"]
        self.period = status["period_ms"]
        self.armed = bool(status.get("armed"))
        self.fault = None
        self.recovering = False

    def update(self, status):
        connection = status["connection"]
        period = status["period_ms"]
        armed = bool(status.get("armed"))
        fault = (connection, status.get("reason")) if connection in ("FAULT", "STALE") else None

        if connection == "OP" and self.connection != "OP":
            devices = " → ".join(device["name"] for device in status["devices"])
            action = "连接恢复" if self.recovering else "实机已连接"
            self.recovering = False
            console_message(f"{action} · OP · {devices} · WKC {status['wkc']}/{status['expected_wkc']} · 目标周期 {period} ms")
        elif fault is not None and fault != self.fault:
            self.recovering = True
            console_message(f"EtherCAT 通信异常：{fault[1] or '状态未知'}")
        elif connection == "DISCONNECTED" and self.connection != "DISCONNECTED":
            console_message(f"EtherCAT 已断开 · {status['zero']}")

        if period != self.period:
            console_message(f"目标通信周期已调整至 {period} ms")
        if armed != self.armed:
            console_message("输出已解锁" if armed else "输出已锁定")
        self.connection, self.period, self.armed = connection, period, armed
        self.fault = fault


class ConsoleStop:
    """Keep the process alive long enough to finish an interactive console close."""

    def __init__(self, request_stop, on_timeout=None, title=None):
        self.request_stop = request_stop
        self.on_timeout = on_timeout
        self.title = title
        self.done = Event()
        self.handler = None

    def __enter__(self):
        if os.name != "nt":
            return self

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        attached = (ctypes.c_uint32 * 1)()
        if not kernel.GetConsoleProcessList(attached, len(attached)):
            return self

        # CONIN$ 不受调用者标准句柄重定向影响。
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        handle = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if handle not in (None, ctypes.c_void_p(-1).value):
            kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, (mode.value | 0x80) & ~0x40)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)

        callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint32)
        kernel.SetConsoleCtrlHandler.argtypes = [callback, ctypes.c_int]
        kernel.SetConsoleCtrlHandler.restype = ctypes.c_int
        self.kernel = kernel
        self.handler = callback(self.handle_event)
        if not kernel.SetConsoleCtrlHandler(self.handler, True):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.title:
            kernel.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
            kernel.SetConsoleTitleW(self.title)
        return self

    def handle_event(self, event):
        if event not in (CTRL_C_EVENT, CTRL_CLOSE_EVENT):
            return False
        self.request_stop("window_close" if event == CTRL_CLOSE_EVENT else "ctrl_c")
        if event == CTRL_CLOSE_EVENT and not self.done.wait(CLOSE_WAIT_SECONDS):
            if self.on_timeout:
                self.on_timeout()
        return True

    def __exit__(self, *_):
        self.done.set()
        if self.handler is not None:
            self.kernel.SetConsoleCtrlHandler(self.handler, False)
