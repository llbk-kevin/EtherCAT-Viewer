"""诊断持久化与清空使用隔离目录，不接触现场运行日志。"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from viewer.diagnostics import Journal


def clip():
    return dict(fields=["time_ms", "interval_ms", "roundtrip_ms", "period_ms", "wkc", "expected_wkc", "session"],
                rows=[[1000, 10, .2, 10, -1, 3, 1]])


def test_fault_details_survive_restart_and_keep_unique_identity(tmp_path):
    journal = Journal(tmp_path)
    event = journal.log("alarm", "读取失败", "warning", before=clip(), fragment_state="collecting")
    assert "before" not in journal.list()[0]
    assert journal.update(event["event_id"], journal.generation, after=clip(), fragment_state="complete")
    assert journal.detail(event["event_id"])["after"] == clip()
    journal.close()
    restored = Journal(tmp_path)
    assert len(restored.list()) == 1
    assert restored.list()[0]["fragment_state"] == "complete"
    assert restored.detail(event["event_id"])["before"] == clip()
    new = restored.log("control", "新的事件")
    assert new["id"] > event["id"] and new["event_id"] != event["event_id"]
    restored.close()


def test_settings_save_with_real_journal_does_not_report_false_failure(tmp_path, monkeypatch):
    import asyncio
    from test_configuration import service, change
    obj = service(tmp_path,monkeypatch)
    obj.journal = Journal(tmp_path/"diagnostics")
    changes = [change(obj.settings.data,["startup","open_browser"],False)]
    plan = obj.settings_plan(changes)
    try:
        result = asyncio.run(obj.apply_settings(changes,plan["changes"]))
        assert result["config"]["startup"]["open_browser"] is False
        assert obj.operation is None and not obj.calls
        records = obj.journal.list()
        assert len(records) == 1 and records[0]["message"] == "逐项应用参数"
        detail = obj.journal.detail(records[0]["event_id"])
        assert detail["revision"] == 1 and detail["config_revision"] == result["config"]["revision"]
    finally:
        obj.journal.close()


def test_older_log_fragments_remain_independent_and_loadable(tmp_path):
    records = [dict(id=1,time_ms=1000,category="runtime",severity="warning",message="故障",before=clip()),
               dict(id=1,time_ms=6000,category="runtime",severity="info",message="故障后片段",related="故障",after=clip())]
    (tmp_path/"events.jsonl").write_text("\n".join(json.dumps(r) for r in records)+"\n", encoding="utf-8")
    journal = Journal(tmp_path)
    rows = journal.list()
    assert len(rows) == 2 and rows[0]["event_id"] != rows[1]["event_id"]
    assert journal.detail(rows[1]["event_id"])["after"] == clip()
    journal.close()
    again = Journal(tmp_path)
    assert [e["event_id"] for e in again.list()] == [e["event_id"] for e in rows]
    again.close()


def test_interrupted_fragment_has_explicit_incomplete_status(tmp_path):
    journal = Journal(tmp_path)
    event = journal.log("alarm", "故障", before=clip(), fragment_state="collecting")
    journal.close()
    restored = Journal(tmp_path)
    assert restored.detail(event["event_id"])["fragment_state"] == "incomplete"
    restored.close()


def test_clear_is_queue_barrier_cancels_old_updates_and_preserves_other_files(tmp_path):
    journal = Journal(tmp_path)
    old_generation = journal.generation
    event = journal.log("alarm", "故障", before=clip(), fragment_state="collecting")
    for i in range(150):
        journal.log("control", str(i))
    for name in ("backend-shutdown.jsonl", "recording.csv", "debug.jsonl"):
        (tmp_path/name).write_text("保留", encoding="utf-8")
    (tmp_path/"events-old.jsonl").write_text("{}\n", encoding="utf-8")
    result = journal.clear()
    assert result["generation"] != old_generation and not journal.list()
    assert not journal.update(event["event_id"], old_generation, after=clip())
    with pytest.raises(ValueError, match="已清空"):
        journal.detail(event["event_id"], old_generation)
    fresh = journal.log("runtime", "清空后的新事件")
    journal.close()
    assert list(tmp_path.glob("events*.jsonl")) == [tmp_path/"events.jsonl"]
    assert all((tmp_path/name).read_text(encoding="utf-8") == "保留" for name in ("backend-shutdown.jsonl", "recording.csv", "debug.jsonl"))
    restored = Journal(tmp_path)
    assert [e["event_id"] for e in restored.list()] == [fresh["event_id"]]
    restored.close()


def test_concurrent_write_and_clear_remains_usable(tmp_path):
    journal = Journal(tmp_path)
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(lambda: [journal.log("runtime", "并发") for _ in range(50)]),pool.submit(journal.clear)]
        for future in futures:
            future.result(timeout=10)
    fresh = journal.log("runtime", "结束标识")
    journal.close()
    restored = Journal(tmp_path)
    assert restored.detail(fresh["event_id"])["message"] == "结束标识"
    restored.close()


def test_clear_failure_does_not_claim_success_or_discard_events(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    event = journal.log("runtime", "保留")
    journal.queue.join()
    original = Path.rename
    def fail(file, target):
        if file.name == "events.jsonl":
            raise PermissionError("测试文件占用")
        return original(file, target)
    monkeypatch.setattr(Path,"rename",fail)
    with pytest.raises(OSError, match="清空失败"):
        journal.clear()
    assert journal.detail(event["event_id"])["message"] == "保留"
    journal.close()


def test_parameter_read_failure_is_not_configuration_mismatch():
    from viewer.hardware import Hardware
    from test_bridge import make_worker
    worker = make_worker()
    worker.settings["modules"]["s2:EL3104"] = dict(filter="off")
    key = "modules/s2:EL3104/filter"
    hardware = Hardware.__new__(Hardware)
    hardware.parameter_values = {key:"off"}
    rows = iter([dict(path=key,value="off",error=None,time_ns=1,duration_ms=.2),
                 dict(path=key,value=None,error="参数读取失败（RuntimeError）：设备未返回有效参数",time_ns=2,duration_ms=2),
                 dict(path=key,value=None,error="参数读取失败（RuntimeError）：设备未返回有效参数",time_ns=3,duration_ms=2),
                 dict(path=key,value="fir60",error=None,time_ns=4,duration_ms=.2),
                 dict(path=key,value="off",error=None,time_ns=5,duration_ms=.2)])
    hardware.parameter_poll_step = lambda:next(rows)
    worker.bus, worker.next_parameter_poll = hardware, 999
    events = []
    worker.event = lambda message,*args,**details:events.append((message,details))
    worker.poll_parameters(1)
    worker.poll_parameters(2)
    field = worker.parameter_readback["fields"][key]
    assert field["last_success_value"] == "off" and field["last_success_ns"] == 1
    assert worker.parameter_readback["values"][key] == "off" and not worker.parameter_drift
    worker.poll_parameters(3)
    worker.poll_parameters(4)
    worker.poll_parameters(5)
    assert [e[0] for e in events] == ["设备参数读取失败","设备参数读取已恢复","设备参数回读与配置不一致","设备参数回读已恢复"]


def test_empty_hardware_exception_retains_last_successful_value():
    from test_configuration import bus, configuration
    hardware = bus()
    hardware.configure(configuration())
    key = "modules/s2:EL3104/filter"
    hardware.parameter_values[key] = "off"
    hardware.parameter_jobs = iter([("filter", hardware.master.slaves[1], "s2:EL3104", "ai", 0)])
    def fail(*_args, **_kwargs):
        raise RuntimeError()
    hardware.master.slaves[1].sdo_read = fail
    result = hardware.parameter_poll_step()
    assert "RuntimeError" in result["error"] and "设备未返回有效参数" in result["error"]
    assert hardware.parameter_values[key] == "off"


def test_event_api_lazy_details_and_manager_only_clear(tmp_path):
    import asyncio
    from fastapi import HTTPException, Request
    from types import SimpleNamespace
    from viewer.server import Command, commands, events, event_detail
    journal = Journal(tmp_path/"diagnostics")
    event = journal.log("alarm","故障",before=clip())
    service = SimpleNamespace(journal=journal,clients={"manager":object(),"reader":object()},
                              controller="manager",command_results={},fault_clips=[(1,"old","old")],
                              heartbeat=lambda:None,status_for=lambda _token:dict(connection="OP",targets=[0]*4))
    def request(token):
        return Request(dict(type="http",app=SimpleNamespace(state=SimpleNamespace(service=service)),
                            headers=[(b"host",b"127.0.0.1"),(b"origin",b"http://127.0.0.1"),
                                     (b"x-viewer-session",token.encode())]))
    async def exercise():
        listed = await events(request("manager"))
        assert "before" not in listed["events"][0]
        assert (await event_detail(event["event_id"],request("reader"),listed["generation"]))["event"]["before"] == clip()
        command = Command(id="clear-1",action="clear_diagnostics",args={"path":str(tmp_path)})
        with pytest.raises(HTTPException) as denied:
            await commands(command,request("reader"))
        assert denied.value.status_code == 403
        result = await commands(command,request("manager"))
        assert result["ok"] and not journal.list() and not service.fault_clips
        assert result["diagnostics"]["generation"] != listed["generation"]
        with pytest.raises(HTTPException) as stale:
            await event_detail(event["event_id"],request("reader"),listed["generation"])
        assert stale.value.status_code == 409
        journal.log("runtime","新事件")
        assert len(journal.list()) == 1
    try:
        asyncio.run(exercise())
    finally:
        journal.close()
