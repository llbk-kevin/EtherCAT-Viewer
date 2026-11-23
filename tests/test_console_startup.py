import asyncio
from threading import Event

import launch
from viewer.console import BusConsole


def test_browser_opens_once_only_when_server_started(monkeypatch):
    events = []
    requested = Event()

    class Server:
        started = False
        should_exit = False

        async def serve(self, sockets):
            await asyncio.sleep(0.01)
            assert not events
            self.started = True
            await asyncio.sleep(0.14)
            requested.set()
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(launch, "open_dashboard", lambda stop: events.append("open"))
    asyncio.run(launch.serve(Server(), requested, object()))
    assert events == ["open"]


def test_stop_before_start_never_opens_browser(monkeypatch):
    requested = Event()
    requested.set()
    calls = []

    class Server:
        started = False
        should_exit = False

        async def serve(self, sockets):
            assert self.should_exit

    monkeypatch.setattr(launch, "open_dashboard", lambda stop: calls.append("open"))
    asyncio.run(launch.serve(Server(), requested, object()))
    assert calls == []


def test_browser_failure_reports_manual_address(monkeypatch, capsys):
    def unavailable(_url):
        raise OSError("browser unavailable")

    monkeypatch.setattr(launch.sys, "platform", "win32")
    monkeypatch.setattr(launch.os, "startfile", unavailable, raising=False)
    launch.open_dashboard(Event())
    assert launch.DASHBOARD_URL in capsys.readouterr().err


def test_console_logs_state_transitions_and_deduplicates(capsys):
    base = dict(connection="DISCONNECTED", period_ms=10, armed=False, zero="等待连接")
    console = BusConsole(base)
    console.update(base)
    online = dict(base, connection="OP", devices=[{"name": "EK1100"}], wkc=3, expected_wkc=3)
    console.update(online)
    console.update(dict(online, errors=1))
    console.update(dict(online, armed=True, period_ms=20))
    fault = dict(online, connection="FAULT", reason="WKC 异常：-1/3", period_ms=20)
    console.update(fault)
    console.update(fault)
    console.update(dict(fault, connection="CONNECTING"))
    console.update(dict(online, period_ms=20))
    console.update(dict(base, period_ms=20, zero="零值已收到 WKC 确认"))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7
    assert "EK1100 · WKC 3/3 · 目标周期 10 ms" in lines[0]
    assert lines[5].startswith("连接恢复")
    assert lines[6] == "EtherCAT 已断开 · 零值已收到 WKC 确认"


def test_browser_can_be_disabled_without_stopping_service(monkeypatch):
    requested = Event()
    calls = []
    class Server:
        started = True
        should_exit = False
        async def serve(self,sockets):
            await asyncio.sleep(.1)
            requested.set()
            while not self.should_exit:
                await asyncio.sleep(.01)
    monkeypatch.setattr(launch,"open_dashboard",lambda stop:calls.append("open"))
    asyncio.run(launch.serve(Server(),requested,object(),dict(open_browser=False,minimized=False)))
    assert calls == []


def test_o_key_opens_dashboard_even_when_startup_autoopen_is_disabled(monkeypatch):
    import sys
    from types import SimpleNamespace
    requested = Event()
    calls = []
    monkeypatch.setattr(launch.sys,"stdin",SimpleNamespace(isatty=lambda:True))
    monkeypatch.setitem(sys.modules,"msvcrt",SimpleNamespace(kbhit=lambda:True,getwch=lambda:"o"))
    monkeypatch.setattr(launch,"open_dashboard",lambda stop:(calls.append(launch.DASHBOARD_URL),requested.set()))
    launch.console_keys(requested)
    assert calls == [launch.DASHBOARD_URL]
