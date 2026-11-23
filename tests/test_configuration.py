"""逐项应用、关联参数、迁移和真实回读模型的行为回归。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from threading import Event
import pytest

from viewer.configuration import make_plan, SettingError, merge_cards
from viewer.settings import defaults, validate, Settings
from viewer.bridge.config import preset
from viewer.hardware import channel_catalog, EXPECTED
from viewer.server import Service
from test_hardware_settings import bus
from test_bridge import make_worker, apply


def configuration():
    data = defaults()
    slaves = [SimpleNamespace(man=2,id=p,rev=1,name=n) for p,n in EXPECTED]
    channels, signature = channel_catalog(slaves)
    data["topology"].update(channels=channels,fingerprint=signature)
    data["hardware"].update(sync="sm",filter="off",interface=r"\Device\NPF_{TEST}")
    return validate(data)


def change(data,path,value):
    from viewer.configuration import get_value
    return dict(path=path,base=get_value(data,path),value=value)


def test_single_field_merge_allows_unrelated_revision_but_rejects_same_field():
    initial = configuration()
    request = change(initial,["hardware","period_ms"],2)
    current = deepcopy(initial)
    current["startup"]["open_browser"] = False
    current["revision"] = 17
    plan = make_plan(current,[request],dict(connection="OP"))
    assert plan["config"]["hardware"]["period_ms"] == 2
    assert not plan["config"]["startup"]["open_browser"]
    current["hardware"]["period_ms"] = 3
    with pytest.raises(SettingError,match="其他页面"):
        make_plan(current,[request])


def test_dependencies_are_explicit_and_do_not_apply_unrelated_drafts():
    data = configuration()
    plan = make_plan(data,[change(data,["modules","s2:EL3104","filter"],"fir60")],dict(connection="OP"))
    assert plan["mode"] == "reconnect"
    assert plan["config"]["hardware"]["sync"] == "freerun"
    assert plan["config"]["modules"]["s2:EL3104"]["filter"] == "fir60"
    assert any(c["path"]==["hardware","sync"] and c["related"] for c in plan["changes"])
    assert plan["config"]["hardware"]["period_ms"] == 10


def test_software_tolerance_lists_required_module_watchdog_change():
    data = configuration()
    plan = make_plan(data,[change(data,["faults","duration_ms"],700)],dict(connection="OP"))
    assert plan["config"]["modules"]["s3:EL4134"]["watchdog_ms"] == 750
    assert plan["mode"] == "reconnect"
    with pytest.raises(SettingError,match="至少"):
        make_plan(data,[change(data,["modules","s3:EL4134","watchdog_ms"],100)])


def test_unrelated_switch_does_not_reconfigure_preserved_filter():
    data = configuration()
    data["modules"]["s2:EL3104"]["filter"] = "current"
    plan = make_plan(data,[change(data,["startup","open_browser"],False)],dict(connection="OP"))
    assert len(plan["changes"]) == 1 and plan["mode"] == "live"


def test_module_and_boot_migration_preserve_card_ids_and_settings():
    data = configuration()
    data["bridge"] = preset(data["topology"]["channels"],data["topology"]["fingerprint"],"legacy")
    data["version"] = 2
    data.pop("modules")
    data["startup"].pop("auto_connect")
    data["startup"]["unattended"] = True
    for card in data["bridge"]["cards"]:
        card.pop("start_on_boot")
        card.pop("authorize_on_boot")
    migrated = validate(data)
    assert migrated["version"] == 3 and migrated["startup"]["auto_connect"]
    assert [c["id"] for c in migrated["bridge"]["cards"]] == [c["id"] for c in data["bridge"]["cards"]]
    assert all(c["start_on_boot"] and c["authorize_on_boot"] for c in migrated["bridge"]["cards"])
    assert migrated["modules"]["s2:EL3104"]["filter"] == "off"
    assert migrated["modules"]["s3:EL4134"]["watchdog_ms"] == 600


def test_same_value_save_does_not_touch_disk_or_revision(tmp_path,monkeypatch):
    monkeypatch.setattr("viewer.settings.settings_path",lambda:tmp_path/"settings.json")
    store = Settings()
    data = configuration()
    store.save(data,0)
    before = store.path.stat().st_mtime_ns
    revision = store.data["revision"]
    assert store.save(store.data,revision)["revision"] == revision
    assert store.path.stat().st_mtime_ns == before
    assert not store.path.with_suffix(".backup.json").exists()


def test_card_merge_retains_other_clients_boot_setting_and_rejects_conflict():
    data = configuration()
    baseline = preset(data["topology"]["channels"],data["topology"]["fingerprint"],"legacy")
    current, draft = deepcopy(baseline),deepcopy(baseline)
    current["cards"][0]["start_on_boot"] = True
    draft["cards"][0]["name"] = "编辑后的名称"
    merged = merge_cards(current,baseline,draft)
    assert merged["cards"][0]["start_on_boot"] and merged["cards"][0]["name"] == "编辑后的名称"
    current["cards"][0]["name"] = "他人修改"
    with pytest.raises(ValueError,match="其他页面"):
        merge_cards(current,baseline,draft)


def test_runtime_rejection_preserves_running_wave_and_period():
    worker = make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"wave",channel=1,shape="sine",peak=.5,frequency=20)["ok"]
    waveform = worker.wave[0]
    data = deepcopy(worker.settings)
    data["hardware"]["period_ms"] = 50
    with pytest.raises(ValueError,match="频率"):
        worker.runtime_config(data)
    assert worker.wave[0] is waveform and worker.period_ms == 10


def test_parameter_poll_reads_without_writes_and_marks_actual_filter():
    hardware = bus()
    hardware.configure(configuration())
    calls = []
    for slave in hardware.master.slaves:
        slave.sdo_write = lambda *_args:calls.append("write")
    results = []
    for _ in range(24):
        result = hardware.parameter_poll_step()
        if result:
            results.append(result)
    assert not calls
    assert hardware.parameter_values["modules/s2:EL3104/filter"] == "off"
    assert hardware.parameter_values["modules/s3:EL4134/watchdog_ms"] == 600
    assert hardware.parameter_values["hardware/sync"] == "sm"
    assert results and all("time_ns" in r and "duration_ms" in r for r in results)


def service(tmp_path,monkeypatch):
    monkeypatch.setattr("viewer.settings.settings_path",lambda:tmp_path/"settings.json")
    obj = Service.__new__(Service)
    obj.settings = Settings()
    obj.settings.save(configuration(),0)
    obj.config_lock = asyncio.Lock()
    obj.status = dict(connection="OP",ready=True,period_ms=10,hardware_details=[],output_owners=["web"]*4)
    obj.calls = []
    obj.generation = 1
    obj.manual_lock = False
    obj.publish_intent = True
    obj.emergency = Event()
    obj.operation = None
    obj.heartbeat = lambda:None
    obj.record_stop = lambda:None
    obj.journal = SimpleNamespace(log=lambda *args,**kwargs:None)
    async def stop(_reason):
        obj.calls.append("stop_bridge")
    obj.bridge = SimpleNamespace(status=lambda:dict(cards=[]),cards={},config=deepcopy(obj.settings.data["bridge"]),stop=stop)
    async def request(action,args):
        obj.calls.append(action)
        if action == "runtime_config":
            return dict(obj.status,period_ms=args["config"]["hardware"]["period_ms"])
        if action == "disconnect":
            return dict(obj.status,connection="DISCONNECTED")
        return dict(obj.status,connection="OP")
    obj.bridge_request = request
    return obj


def test_noop_does_not_send_worker_command(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","period_ms"],10)]
    plan = obj.settings_plan(request)
    revision = obj.settings.data["revision"]
    result = asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert result["noop"] and obj.calls == [] and obj.settings.data["revision"] == revision


def test_live_apply_changes_only_requested_parameter(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","period_ms"],2)]
    plan = obj.settings_plan(request)
    result = asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert result["config"]["hardware"]["period_ms"] == 2
    assert obj.calls == ["runtime_config"] and not obj.manual_lock


def test_reconnect_zeroes_and_revokes_publication_before_applying(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","sync"],"dc")]
    plan = obj.settings_plan(request)
    asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert obj.calls == ["stop_bridge","zero","disconnect","configure","connect"]
    assert obj.manual_lock and not obj.publish_intent
    assert obj.settings.data["hardware"]["sync"] == "dc"


def test_unapproved_dependency_never_touches_worker(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["faults","duration_ms"],700)]
    with pytest.raises(SettingError,match="关联"):
        asyncio.run(obj.apply_settings(request,request))
    assert not obj.calls


def test_failed_persistence_rolls_back_live_runtime(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","period_ms"],2)]
    plan = obj.settings_plan(request)
    before = deepcopy(obj.settings.data)
    monkeypatch.setattr(obj.settings,"save",lambda *_args:(_ for _ in ()).throw(OSError("磁盘写入失败")))
    with pytest.raises(SettingError,match="磁盘"):
        asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert obj.calls == ["runtime_config","runtime_config"] and obj.settings.data == before


def test_global_zero_during_reconnect_cancels_configuration_commit(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","sync"],"dc")]
    plan = obj.settings_plan(request)
    original = obj.bridge_request
    async def interrupted(action,args):
        result = await original(action,args)
        if action == "connect":
            obj.critical_zero()
        return result
    obj.bridge_request = interrupted
    with pytest.raises(SettingError,match="清零"):
        asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert obj.settings.data["hardware"]["sync"] == "sm"
    assert obj.calls[-2:] == ["disconnect","configure"] and obj.manual_lock


def test_current_mode_noop_and_unsupported_sync_rejected_before_hardware():
    data = configuration()
    data["hardware"]["sync"] = "current"
    request = [change(data,["hardware","sync"],"current")]
    status = dict(connection="OP",parameter_readback=dict(values={"hardware/sync":"sm"}))
    assert make_plan(data,request,status)["noop"]
    status["hardware_details"] = [dict(name="测试模块",supported_modes=3)]
    with pytest.raises(SettingError,match="不支持"):
        make_plan(data,[change(data,["hardware","sync"],"dc")],status)


def test_parent_child_changes_cannot_silently_override_each_other():
    data = configuration()
    ident = data["topology"]["channels"]["ai"][0]["id"]
    with pytest.raises(SettingError,match="重复"):
        make_plan(data,[change(data,["channels",ident,"calibration"],dict(enabled=False,gain=2,offset=0)),change(data,["channels",ident,"calibration","gain"],3)])


def test_tcp_saved_parameter_keeps_newer_boot_option_and_active_card_reference(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    data = deepcopy(obj.settings.data)
    data["bridge"] = preset(data["topology"]["channels"],data["topology"]["fingerprint"],"legacy")
    obj.settings.save(data,data["revision"])
    ident = data["bridge"]["cards"][0]["id"]
    card = SimpleNamespace(config={})
    obj.bridge.cards[ident] = card
    obj.set_bridge_configuration(obj.settings.data["bridge"])
    stale = deepcopy(obj.bridge.config)
    request = [change(obj.settings.data,["bridge","cards",ident,"start_on_boot"],True)]
    plan = obj.settings_plan(request)
    asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert card.config is next(c for c in obj.bridge.config["cards"] if c["id"]==ident)
    asyncio.run(obj.persist_bridge(stale,updates={ident:{"saved_divider":1234}}))
    saved = next(c for c in obj.settings.data["bridge"]["cards"] if c["id"]==ident)
    assert saved["start_on_boot"] and saved["saved_divider"] == 1234
    assert card.config["saved_divider"] == 1234
    assert card.config is next(c for c in obj.bridge.config["cards"] if c["id"]==ident)


def test_service_card_save_merges_metadata_under_configuration_lock(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    monkeypatch.setattr("viewer.server.local_addresses",lambda:["127.0.0.1"])
    data = deepcopy(obj.settings.data)
    data["bridge"] = preset(data["topology"]["channels"],data["topology"]["fingerprint"],"legacy")
    obj.settings.save(data,data["revision"])
    baseline = deepcopy(obj.settings.data["bridge"])
    incoming = deepcopy(baseline)
    incoming["cards"][0]["name"] = "只改名称"
    changed = deepcopy(obj.settings.data)
    changed["bridge"]["cards"][0]["start_on_boot"] = True
    obj.settings.save(changed,changed["revision"])
    saved = asyncio.run(obj.save_bridge(incoming,base=baseline))
    assert saved["cards"][0]["start_on_boot"] and saved["cards"][0]["name"] == "只改名称"


def test_invalid_approved_plan_has_no_side_effect(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    request = [change(obj.settings.data,["hardware","period_ms"],2)]
    with pytest.raises(SettingError,match="格式"):
        asyncio.run(obj.apply_settings(request,[None]))
    assert not obj.calls


def test_login_registry_failure_cannot_leave_operation_running(tmp_path,monkeypatch):
    obj = service(tmp_path,monkeypatch)
    monkeypatch.setattr("viewer.server.set_login",lambda *_args:(_ for _ in ()).throw(OSError("启动项写入被拒绝")))
    request = [change(obj.settings.data,["startup","login"],True)]
    plan = obj.settings_plan(request)
    with pytest.raises(SettingError,match="被拒绝"):
        asyncio.run(obj.apply_settings(request,plan["changes"]))
    assert obj.operation["state"] == "failed"
    assert not obj.settings.data["startup"]["login"]
    assert obj.calls == ["runtime_config","runtime_config"]
