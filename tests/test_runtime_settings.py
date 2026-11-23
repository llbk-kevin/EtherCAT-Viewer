"""运行策略、校准与持久化的行为回归。"""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
import pytest

from viewer.calibration import calibrated, drive_voltage, engineering_value, engineering_voltage, two_point, validate_profile
from viewer.settings import Settings, defaults, validate
from viewer.diagnostics import History, Journal
from viewer.bridge.config import preset, validate_config
from viewer.bridge.sampling import Sampler
from test_bridge import make_worker, apply, source, catalog


def test_calibration_pipeline_and_physical_zero():
    profile = validate_profile(dict(calibration=dict(enabled=True,gain=1.02,offset=.03),
                                   engineering=dict(enabled=True,unit="MPa",voltage1=0,value1=10,voltage2=5,value2=110)))
    assert calibrated(drive_voltage(0,profile),profile) == pytest.approx(0)
    assert calibrated(drive_voltage(5,profile),profile) == pytest.approx(5)
    assert engineering_value(2.5,profile) == 60
    assert engineering_voltage(60,profile) == 2.5
    assert two_point(0,.03,5,5.13) == pytest.approx(dict(enabled=True,gain=1.02,offset=.03))
    with pytest.raises(ValueError):
        engineering_voltage(float("nan"),profile)
    with pytest.raises(ValueError):
        validate_profile(dict(calibration=dict(enabled=True,gain=0)))


def test_unreachable_calibrated_zero_is_rejected():
    data = defaults()
    data["channels"]["s3:EL4134:AO1"] = dict(calibration=dict(enabled=True,gain=1,offset=11))
    with pytest.raises(ValueError,match="超出"):
        validate(data)


def test_settings_atomic_revision_backup_and_native_bridge(tmp_path,monkeypatch):
    monkeypatch.setattr("viewer.settings.settings_path",lambda:tmp_path/"settings.json")
    card = preset(catalog(),"signature","legacy")
    store = Settings()
    data = deepcopy(store.data)
    data["bridge"] = card
    data["faults"]["consecutive"] = 17
    saved = store.save(data,0)
    assert saved["revision"] == 1
    reloaded = Settings()
    assert reloaded.data["faults"]["consecutive"] == 17
    assert reloaded.data["bridge"]["cards"] == card["cards"]
    saved["startup"]["open_browser"] = False
    store.save(saved,1)
    assert json.loads((tmp_path/"settings.backup.json").read_text(encoding="utf-8"))["revision"] == 1
    with pytest.raises(ValueError,match="其他页面"):
        store.save(data,0)
    (tmp_path/"settings.json").write_text("损坏",encoding="utf-8")
    loaded = Settings()
    assert loaded.error and not loaded.data["startup"]["auto_connect"]


def test_bridge_configuration_only_loads_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("viewer.settings.settings_path", lambda: tmp_path/"settings.json")
    legacy = tmp_path/"bridge.json"
    legacy.write_text(json.dumps(preset(catalog(), "signature", "legacy")), encoding="utf-8")
    loaded = Settings()
    assert not loaded.error and loaded.data["bridge"]["cards"] == []
    assert not loaded.path.exists()


def test_version_2_configuration_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("viewer.settings.settings_path", lambda: tmp_path/"settings.json")
    data = defaults()
    data["version"] = 2
    with pytest.raises(ValueError, match="配置版本必须为 3"):
        validate(data)
    (tmp_path/"settings.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = Settings()
    assert loaded.error and not loaded.data["startup"]["auto_connect"]


@pytest.mark.parametrize("section,key,value", [
    ("hardware", "filter", "current"),
    ("hardware", "watchdog_ms", 600),
    ("startup", "unattended", False),
])
def test_version_3_rejects_obsolete_fields(section, key, value):
    data = defaults()
    data[section][key] = value
    with pytest.raises(ValueError):
        validate(data)


@pytest.mark.parametrize("sync,filter",[("dc","fir50"),("sm","iir2"),("freerun","off")])
def test_invalid_sync_filter_combinations(sync,filter):
    data = defaults()
    data["hardware"]["sync"] = sync
    data["modules"]["s2:EL3104"] = dict(filter=filter)
    with pytest.raises(ValueError):
        validate(data)


def test_watchdog_tolerance_linkage():
    data = defaults()
    data["modules"]["s3:EL4134"] = dict(watchdog_ms=100)
    with pytest.raises(ValueError,match="看门狗不足以覆盖容错窗口"):
        validate(data)
    data["modules"]["s3:EL4134"]["watchdog_ms"] = 600
    assert validate(data)["modules"]["s3:EL4134"]["watchdog_ms"] == 600


def test_mixed_profiles_shared_readonly_and_single_writer():
    channels = catalog()
    legacy = preset(channels,"signature","legacy")
    extra = preset(channels,"signature","extended")["cards"][0]
    extra["ao_writable"] = [False]*4
    legacy["cards"].append(extra)
    checked = validate_config(legacy,channels,"signature",["127.0.0.1"])
    assert {c["profile"] for c in checked["cards"]} == {"legacy","extended"}
    extra["ao_writable"][0] = True
    with pytest.raises(ValueError,match="重复占用"):
        validate_config(legacy,channels,"signature",["127.0.0.1"])


def test_repeated_ao_alias_only_one_writable():
    channels = catalog()
    data = preset(channels,"signature","legacy")
    data["cards"] = data["cards"][:1]
    card = data["cards"][0]
    card["ao"] = [channels["ao"][0]["id"]]*2
    card["ao_writable"] = [False,True]
    assert validate_config(data,channels,"signature",["127.0.0.1"])
    card["ao_writable"] = [True,True]
    with pytest.raises(ValueError):
        validate_config(data,channels,"signature",["127.0.0.1"])


def test_web_and_tcp_per_channel_and_global_zero():
    worker = make_worker()
    assert apply(worker,"arm",channels=[2])["ok"]
    assert apply(worker,"set_output",channel=2,voltage=.4)["ok"]
    assert apply(worker,"bridge_setup",cards=[dict(id="card",outputs=[0])])["ok"]
    assert worker.values[1] == .4
    assert apply(worker,"bridge_authorize",card="card")["ok"]
    assert apply(worker,"bridge_claim",card="card",token="tcp")["ok"]
    assert apply(worker,"bridge_output",card="card",token="tcp",changes=[dict(index=0,voltage=.3)])["ok"]
    assert worker.owners()[:2] == ["card","web"]
    assert not apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"bridge_stop")["ok"]
    assert worker.values[:2] == [0,.4]
    assert apply(worker,"zero")["ok"]
    assert worker.values == [0]*4 and worker.owners() == ["locked"]*4


def test_web_loss_defaults_continue_and_optional_zero():
    worker = make_worker()
    assert apply(worker,"arm",channels=[1,2])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.4)["ok"]
    assert apply(worker,"set_output",channel=2,voltage=.6)["ok"]
    worker.settings["faults"]["web_wait_ms"] = 0
    worker.settings["faults"]["policies"]["web"][worker.channels["ao"][1]["id"]] = dict(action="zero")
    assert apply(worker,"web_presence",present=False)["ok"]
    assert worker.values[:2] == [.4,0]
    assert worker.owners()[:2] == ["web","locked"]


def test_transient_wkc_freezes_then_recovers_without_disconnect():
    worker = make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.5)["ok"]
    original = worker.bus.exchange
    results = iter([0,0,3])
    def exchange(values):
        original(values)
        return next(results)
    worker.bus.exchange = exchange
    for _ in range(2):
        sample = worker.cycle(time.perf_counter_ns())
        assert not sample["ao_valid"] and worker.health == "TOLERATING" and worker.bus is not None
        assert worker.values[0] == pytest.approx(.5,abs=.001)
    sample = worker.cycle(time.perf_counter_ns())
    assert sample["ao_valid"] and worker.health == "NORMAL" and worker.owners()[0] == "web"


@pytest.mark.parametrize("policy,voltage",[("zero",0),("hold",.5),("value",.2)])
def test_threshold_fault_applies_per_output_policy(policy,voltage):
    worker = make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.5)["ok"]
    worker.desired = True
    worker.settings["faults"]["consecutive"] = 2
    worker.settings["faults"]["policies"]["bus"][worker.channels["ao"][0]["id"]] = dict(action=policy,voltage=.2)
    worker.bus.exchange = lambda _v:0
    worker.bus.close = lambda **kwargs:False
    worker.cycle(time.perf_counter_ns())
    worker.cycle(time.perf_counter_ns())
    assert worker.connection == "FAULT" and worker.values[0] == pytest.approx(voltage,abs=.001)
    assert worker.reconnect_at and not worker.wave and worker.owners() == ["locked"]*4


def test_tolerance_duration_is_independent_of_count():
    worker = make_worker()
    worker.settings["faults"].update(consecutive=100,duration_ms=10)
    worker.bus.exchange = lambda _v:0
    worker.bus.close = lambda **kwargs:False
    now = time.perf_counter_ns()
    worker.cycle(now)
    assert worker.bus
    worker.cycle(now+10_000_000)
    assert worker.connection == "FAULT"


def test_sampler_pause_does_not_fill_fault_gap():
    sampler = Sampler([[(0,1)]],2000,"hold",10,100)
    sampler.feed(source(1,1_000_000_000))
    assert sampler.feed(source(2,1_010_000_000))[0][1] == 20
    sampler.pause()
    assert sampler.feed(source(9,2_000_000_000)) == []
    assert sampler.feed(source(10,2_010_000_000))[0][1] == 20
    assert sampler.generated == 40


def test_diagnostic_history_preserves_peaks_and_retention():
    history = History(seconds=1)
    now = time.perf_counter_ns()
    samples = [dict(time_ns=now+i*1000000,interval_ms=99 if i==500 else 1,roundtrip_ms=.1,period_ms=1,
                    wkc=-1 if i==510 else 3,expected_wkc=3,session=1) for i in range(1000)]
    history.add(samples)
    data = history.query(history.offset+now/1e6,history.offset+now/1e6+1000,32)
    assert any(r[1]==99 for r in data["rows"]) and any(r[4]==-1 for r in data["rows"])
    later = dict(samples[-1],time_ns=now+3_000_000_000)
    history.add([later])
    assert len(history.chunks) == 1


def test_structured_journal_persists_utf8(tmp_path):
    journal = Journal(tmp_path)
    journal.log("connection","连接失败","error",wkc=-1)
    journal.close()
    record = json.loads((tmp_path/"events.jsonl").read_text(encoding="utf-8"))
    assert record["message"] == "连接失败" and record["wkc"] == -1


def test_refresh_resume_token_does_not_grant_duplicate_tab_management():
    from viewer.server import session_token
    known = {"same-tab"}
    assert session_token("same-tab",known,{},set()) == "same-tab"
    assert session_token("same-tab",known,{"same-tab":object()},set()) != "same-tab"
    assert session_token("same-tab",known,{"same-tab":object()},{"same-tab"}) == "same-tab"
    assert session_token("untrusted",known,{},set()) != "untrusted"


def test_explicit_release_zero_overrides_tcp_hold():
    worker = make_worker()
    assert apply(worker,"bridge_setup",cards=[dict(id="a",outputs=[0])])["ok"]
    assert apply(worker,"bridge_authorize",card="a")["ok"]
    assert apply(worker,"bridge_claim",card="a",token="a")["ok"]
    assert apply(worker,"bridge_output",card="a",token="a",changes=[dict(index=0,voltage=.5)])["ok"]
    worker.settings["faults"]["policies"]["tcp"][worker.channels["ao"][0]["id"]] = dict(action="hold")
    assert apply(worker,"bridge_release",card="a",token="a",safe=True)["ok"]
    assert worker.values[0] == 0


def test_legacy_pause_preserves_complete_frame_sequences():
    import struct
    from viewer.bridge.runtime import Card, Session
    channels = catalog()
    configuration = preset(channels,"signature","legacy")["cards"][0]
    configuration["send_ms"] = 1000
    bridge = SimpleNamespace(channels=channels,source_status={"period_ms":10},
                             index=lambda kind,ident:next(i for i,c in enumerate(channels[kind]) if c["id"]==ident))
    writer = SimpleNamespace(get_extra_info=lambda _name:("127.0.0.1",1234))
    session = Session(bridge,Card(configuration),None,writer)
    session.start_stream(list(range(8)),200000)
    start = session.stream_started_ns+1000
    session.feed([source(1,start),source(2,start+10000000)])
    previous = bytes(session.block)
    assert previous
    session.feed([dict(source(3,start+20000000),health="TOLERATING")])
    assert session.paused and bytes(session.block) == previous
    session.feed([source(4,start+30000000),source(5,start+40000000)])
    frames = bytes(session.block)
    seq = [struct.unpack_from("<I",frames,i+12)[0] for i in range(0,len(frames),1460)]
    assert seq == list(range(1,len(seq)+1))
    assert session.sampler.generated == 4000


def test_human_readable_al_error():
    from viewer.hardware import state_failure
    message = state_failure("SAFEOP",[dict(name="EL3104",state="0x12",al_status="0x0037")])
    assert "SYNC1" in message and "0037" in message and "{" not in message


def test_unknown_revision_does_not_guess_dc_parameters():
    from viewer.hardware import Hardware, EXPECTED
    with pytest.raises(ValueError,match="尚未核对"):
        Hardware.dc_parameters(SimpleNamespace(id=EXPECTED[1][0],rev=1,name="EL3104"))


def test_emergency_zero_cannot_be_undone_by_one_bad_wkc():
    worker = make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.5)["ok"]
    original=worker.bus.exchange
    replies=iter([0,3])
    sent=[]
    def exchange(v):
        sent.append(list(v));original(v);return next(replies)
    worker.bus.exchange=exchange
    worker.lock("global zero")
    worker.cycle(time.perf_counter_ns())
    assert worker.values == [0]*4
    worker.cycle(time.perf_counter_ns())
    assert all(v==[0]*4 for v in sent) and worker.confirmed==[0]*4


def test_unconfirmed_new_wave_is_rejected_without_resuming_later():
    worker=make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.5)["ok"]
    original=worker.bus.exchange
    replies=iter([0,3])
    worker.bus.exchange=lambda values:(original(values),next(replies))[1]
    now=time.perf_counter_ns()
    worker.command(dict(id="newwave",action="wave",args=dict(channel=1,shape="sine",peak=.3,frequency=.2),owner=1),now)
    worker.cycle(now)
    answers=[]
    while not worker.events.empty():answers.append(worker.events.get_nowait())
    assert any(a.get("id")=="newwave" and not a["ok"] for a in answers)
    assert not worker.wave
    worker.cycle(time.perf_counter_ns())
    assert not worker.wave and worker.values[0]==pytest.approx(.5,abs=.001)


def test_manual_zero_still_wins_when_fault_policy_is_hold():
    worker=make_worker()
    assert apply(worker,"arm",channels=[1])["ok"]
    assert apply(worker,"set_output",channel=1,voltage=.5)["ok"]
    worker.settings["faults"]["policies"]["bus"][worker.channels["ao"][0]["id"]] = dict(action="hold")
    original=worker.bus.exchange
    sent=[]
    worker.bus.exchange=lambda values:(sent.append(list(values)),original(values))[1]
    worker.lock("global zero")
    worker.fault("link fault immediately after zero")
    assert sent and all(values==[0]*4 for values in sent)
    assert worker.values == [0]*4


def test_diagnostic_decimation_keeps_reconnection_gap():
    history=History()
    now=time.perf_counter_ns()
    history.add([dict(time_ns=now,interval_ms=10,roundtrip_ms=.1,period_ms=10,wkc=3,expected_wkc=3,session=1),
                 dict(time_ns=now+100000000,interval_ms=10,roundtrip_ms=.1,period_ms=10,wkc=3,expected_wkc=3,session=2)])
    rows=history.query(history.offset+now/1e6-1,history.offset+now/1e6+101,32)["rows"]
    assert len(rows)==3 and rows[1][1] is None and rows[1][4] is None


@pytest.mark.parametrize("section,invalid",[("faults",dict(policies=[])),("topology",dict(channels=[])),
                                         ("bridge",dict(version=1,cards=[None])),
                                         ("channels",{"ao0":dict(calibration=None)}),
                                         ("channels",{"ao0":dict(measurements=[None])})])
def test_malformed_json_structure_recovers_without_crashing(tmp_path,monkeypatch,section,invalid):
    monkeypatch.setattr("viewer.settings.settings_path",lambda:tmp_path/"settings.json")
    config=defaults();config[section]=invalid
    with pytest.raises(ValueError):validate(config)
    (tmp_path/"settings.json").write_text(json.dumps(config),encoding="utf-8")
    loaded=Settings()
    assert loaded.error and not loaded.data["startup"]["auto_connect"]


def test_history_export_uses_stable_batches_during_collection():
    history=History()
    now=time.perf_counter_ns()
    sample=dict(time_ns=now,interval_ms=10,roundtrip_ms=.1,period_ms=10,wkc=3,expected_wkc=3,session=1)
    history.add([sample])
    class CollectDuringRead(list):
        once=True
        def __getitem__(self,key):
            if self.once:
                self.once=False
                history.add([dict(sample,time_ns=now+10000000)])
            return super().__getitem__(key)
    history.chunks[0]=CollectDuringRead(history.chunks[0])
    data=history.query(history.offset+now/1e6-1,history.offset+now/1e6+100,32)
    assert len(data["rows"])==1 and len(history.chunks)==2
