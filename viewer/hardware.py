import ctypes
import json
import re
import subprocess
import time
from uuid import UUID

from .paths import powershell_path
from .pdo import Mapping, read_int, read_mapping, raw_voltage, volts

EXPECTED = [(0x044C2C52, "EK1100"), (0x0C203052, "EL3104"), (0x10263052, "EL4134")]
INIT, PREOP, SAFEOP, OP = 1, 2, 4, 8

def load_driver():
    try:
        import pysoem
    except ImportError as exc:
        raise RuntimeError("无法加载 PySOEM。请安装 Npcap 并启用 WinPcap API 兼容模式。") from exc
    return pysoem

def adapter_guid(value):
    try:
        return str(UUID(value)).upper()
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError("网卡 GUID 格式无效") from exc


def adapters():
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-NetAdapter -Physical | Where-Object InterfaceType -eq 6 | "
        "Select-Object Name,InterfaceGuid,Status,LinkSpeed,InterfaceDescription | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", command],
        check=True, capture_output=True, encoding="utf-8-sig", timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = json.loads(result.stdout or "[]") or []
    rows = rows if isinstance(rows, list) else [rows]
    try:
        visible = {
            _text(item.name).upper(): _text(item.name)
            for item in load_driver().find_adapters()
        }
        error = None
    except Exception as exc:
        visible, error = {}, str(exc)
    items = []
    for row in rows:
        guid = adapter_guid(row["InterfaceGuid"])
        ident = fr"\Device\NPF_{{{guid}}}"
        items.append(dict(guid=guid, name=row["Name"], description=row["InterfaceDescription"],
                          status=row["Status"], speed=row["LinkSpeed"],
                          interface=visible.get(ident.upper())))
    return {"items": items, "error": error}


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def check_adapter(ifname):
    match = re.fullmatch(r"\\Device\\NPF_\{([0-9A-F-]{36})\}", ifname, re.I) if isinstance(ifname, str) else None
    if not match:
        raise ValueError("请选择可用的 EtherCAT 有线网卡")
    guid = adapter_guid(match[1])
    result = adapters()
    row = next((item for item in result["items"] if item["guid"] == guid), None)
    if row is None:
        raise ValueError("所选网卡不是当前 Windows 物理有线网卡")
    if row["status"] != "Up":
        raise ValueError(f"所选网卡「{row['name']}」链路未连接，请检查网线和 EK1100 电源")
    if not row["interface"]:
        raise ValueError(result["error"] or f"Npcap 无法访问网卡「{row['name']}」")
    return row


def identities(master):
    master.read_state()
    return [dict(name=s.name, vendor=f"0x{s.man:08X}", product=f"0x{s.id:08X}",
                 revision=f"0x{s.rev:08X}", state=f"0x{s.state:02X}",
                 al_status=f"0x{s.al_status:04X}") for s in master.slaves]

AL_MESSAGES = {0x001B: "过程数据看门狗超时", 0x001C: "SyncManager 配置无效", 0x001D: "输出配置无效",
               0x001E: "输入配置无效", 0x002D: "未收到同步信号", 0x002E: "周期过短", 0x0030: "DC 配置无效",
               0x0032: "DC 锁相错误", 0x0033: "DC 同步 I/O 错误", 0x0034: "DC 同步超时",
               0x0035: "DC 周期不符合要求", 0x0036: "DC SYNC0 周期不符合要求", 0x0037: "DC SYNC1 周期不符合要求"}


def state_failure(stage, devices):
    rows = []
    for i,d in enumerate(devices,1):
        code = int(d["al_status"],16)
        state = int(d["state"],16)
        if code or state & 0x10:
            rows.append(f"从站 {i} {d['name']}：{AL_MESSAGES.get(code,'设备报告状态错误')}（AL 0x{code:04X}）")
    return f"未进入 {stage}；"+"；".join(rows or ["从站未达到请求状态，详见诊断记录"])


def signed_default(slave, index):
    for sub in (1, 2, 7):
        if read_int(slave, index, sub, 1) != 0:
            raise ValueError(f"{slave.name} CoE {index:04X}:{sub:02X} 为非默认缩放、表示或用户校准")

class BusLock:
    def __init__(self, guid):
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = kernel.CreateMutexW(None, False, f"Local\\EtherCAT-Viewer-{guid}")
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:
            self.close()
            raise RuntimeError("EtherCAT 网卡正在被本项目的另一个实机进程使用")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

def validate_topology(slaves):
    if len(slaves) < 2 or (slaves[0].man, slaves[0].id) != (2, EXPECTED[0][0]):
        raise ValueError("从站链必须以 EK1100 开始，并至少包含一个支持的 IO 模块")
    for position, slave in enumerate(slaves[1:], 2):
        if slave.man != 2 or slave.id not in (EXPECTED[1][0], EXPECTED[2][0]):
            raise ValueError(f"从站 {position} 不支持：{slave.name} / 0x{slave.id:08X}")


def channel_catalog(slaves):
    import hashlib
    signature = [(i, s.man, s.id, s.rev) for i, s in enumerate(slaves, 1)]
    channels = {"ai": [], "ao": []}
    for position, slave in enumerate(slaves[1:], 2):
        kind = "ai" if slave.id == EXPECTED[1][0] else "ao"
        model = "EL3104" if kind == "ai" else "EL4134"
        for ch in range(1, 5):
            channels[kind].append(dict(id=f"s{position}:{model}:{kind.upper()}{ch}",
                                      label=f"从站 {position} · {model} · {kind.upper()}{ch}",
                                      slave_position=position, module=model, channel=ch))
    fingerprint = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    return channels, fingerprint


class Hardware:
    def __init__(self, ifname):
        adapter = check_adapter(ifname)
        self.master = load_driver().Master()
        self.opened = self.output_ready = False
        self.devices, self.mapping = [], {}
        self.channels = {"ai": [], "ao": []}
        self.fingerprint = ""
        self.ai_modules, self.ao_modules = [], []
        self.expected = self.watchdog = None
        self.bus_lock = BusLock(adapter["guid"])
        try:
            self.master.open(adapter["interface"])
            self.opened = True
            self.master.config_init()
            self.devices = identities(self.master)
            validate_topology(self.master.slaves)
            self.channels, self.fingerprint = channel_catalog(self.master.slaves)
            for slave in self.master.slaves[1:]:
                (self.ai_modules if slave.id == EXPECTED[1][0] else self.ao_modules).append([slave, None])
        except Exception:
            self.close()
            raise

    @staticmethod
    def dc_parameters(slave):
        # 官方 ESI：EL31xx DCIN / EL4xxx DC，2026-11-23 核对。
        revision = slave.rev >> 16
        if slave.id == EXPECTED[1][0] and revision in (16, 17, 18, 19, 20):
            return (-100000 if revision >= 18 else 0), (25000 if revision >= 18 else 5000)
        if slave.id == EXPECTED[2][0] and revision in range(1016, 1022):
            return 0, 100000
        raise ValueError(f"{slave.name} Revision 0x{slave.rev:08X} 的 DC ESI 参数尚未核对；请选择帧同步或自由运行")

    def configure(self, configuration=None):
        self.configuration = configuration
        self.zero_values = [0.0] * len(self.channels["ao"])
        self.hardware_details = []
        pending = []
        for modules, output in ((self.ai_modules, False), (self.ao_modules, True)):
            for pair in modules:
                slave = pair[0]
                for ch in range(4):
                    idx = 0x8000 + 16 * ch
                    signed_default(slave, idx)
                    if configuration is None and output and (read_int(slave, idx, 5, 1) != 0 or
                                   read_int(slave, idx, 0x13, 2, True) != 0):
                        raise ValueError(f"{slave.name} 通道 {ch+1} 看门狗替代值必须为零")
                pending.append((pair, output, read_mapping(slave, 0x1C12 if output else 0x1C13)))
        if configuration is not None:
            self.apply_configuration(configuration)
        self.master.manual_state_change = 1
        self.master.config_map()
        sm_details = []
        for slave, _mapping in self.ao_modules:
            sm = slave._fprd(0x0810, 8, 2000)
            if int.from_bytes(sm[2:4], "little") != 8 or not sm[4] & 0x04:
                raise RuntimeError(f"{slave.name} SM2 方向或长度异常：{sm.hex()}")
            if not sm[4] & 0x40:
                slave._fpwr(0x0816, bytes([sm[6] & ~1]), 2000)
                try:
                    slave._fpwr(0x0814, bytes([sm[4] | 0x40]), 2000)
                finally:
                    slave._fpwr(0x0816, bytes([sm[6]]), 2000)
            sm_details.append({"original": sm[4], "active": slave._fprd(0x0814, 1, 2000)[0]})
        for pair, output, entries in pending:
            pair[1] = Mapping(entries, len(pair[0].output if output else pair[0].input), output)
        self.output_ready = True
        self.mapping = dict(ai=[m.describe() for _, m in self.ai_modules],
                            ao=[m.describe() for _, m in self.ao_modules],
                            channels=self.channels, fingerprint=self.fingerprint, sm2=sm_details)
        watchdogs = []
        for slave, _ in self.ao_modules:
            from .settings import module_settings
            position = self.master.slaves.index(slave)+1
            target = module_settings(configuration,position,slave.name).get("watchdog_ms",configuration["hardware"]["watchdog_ms"]) if configuration else 100
            value = slave.get_watchdog("processdata")
            if abs(value-target) > 1:
                slave.set_watchdog("processdata", target)
                value = slave.get_watchdog("processdata")
            if abs(value-target) > 1 or not slave._fprd(0x0814, 1, 2000)[0] & 0x40:
                raise RuntimeError(f"{slave.name} 过程数据看门狗回读异常：{value} ms")
            watchdogs.append(value)
        self.watchdog = max(watchdogs) if watchdogs else None
        self.mapping["watchdogs_ms"] = watchdogs
        self.expected = self.master.expected_wkc
        if self.expected <= 0:
            raise RuntimeError("PDO 预期 WKC 无效")
        self.dc_enabled = any(d["sync_mode"] in (2, 3) for d in self.hardware_details)
        if configuration and self.dc_enabled:
            if not self.master.config_dc():
                raise ValueError("当前从站未发现可用 DC 时钟")
            for (slave, _), detail in zip(self.ai_modules + self.ao_modules, self.hardware_details):
                if detail["sync_mode"] in (2, 3):
                    shift, sync1 = self.dc_parameters(slave)
                    slave.dc_sync(True, configuration["hardware"]["period_ms"]*1000000, shift, sync1)
                    detail["dc"] = dict(sync0_shift_ns=shift, sync1_delta_ns=sync1, cycle_ns=configuration["hardware"]["period_ms"]*1000000)
        self.parameter_values, self.parameter_modes = {}, {}
        self.parameter_poll_start()
        self.master.state = SAFEOP
        self.master.write_state()
        self.master.state_check(SAFEOP, 2000000)
        self.devices = identities(self.master)
        if any(s.state != SAFEOP for s in self.master.slaves):
            raise RuntimeError(state_failure("SAFEOP", self.devices))
        # 进入 OP 前也必须按 DC 周期供给零 PDO，避免初始化循环错过 SYNC1。
        next_send = 0
        period_ns = (configuration or {}).get("hardware", {}).get("period_ms", 10)*1000000
        def zero_exchange():
            nonlocal next_send
            if next_send:
                time.sleep(max(0,(next_send-time.perf_counter_ns())/1e9))
            wkc = self.exchange(self.zero_values)
            if self.dc_enabled:
                reference = getattr(self.master,"dc_time",0)
                target = -min(period_ns//4,1000000)
                phase = (reference-target+period_ns//2)%period_ns-period_ns//2
                next_send = self.last_send_ns+period_ns-phase
            else:
                next_send = self.last_send_ns+period_ns
            return wkc
        warmup = time.monotonic()+(0.25 if self.dc_enabled else 0.03)
        while time.monotonic() < warmup:
            zero_exchange()
        self.master.read_state()
        if any(s.state & 0x10 and s.al_status == 0x001B for s in self.master.slaves):
            self.master.state = SAFEOP | 0x10
            self.master.write_state()
            self.master.state_check(SAFEOP,200000)
            zero_exchange()
        self.master.state = OP
        self.master.write_state()
        deadline = time.monotonic()+3
        acknowledged_watchdog = False
        while time.monotonic() < deadline:
            wkc = zero_exchange()
            if self.master.state_check(OP,2000)==OP and wkc==self.expected and self.check_op():
                return
            self.master.read_state()
            if not acknowledged_watchdog and any(s.state & 0x10 and s.al_status==0x001B for s in self.master.slaves):
                acknowledged_watchdog = True
                self.master.state = SAFEOP | 0x10
                self.master.write_state()
                zero_exchange()
                self.master.state = OP
                self.master.write_state()
        raise RuntimeError(state_failure("OP", identities(self.master)))


    def apply_configuration(self, configuration):
        from .calibration import drive_voltage
        from .settings import module_settings
        h = configuration["hardware"]
        profiles = configuration["channels"]
        policies = configuration["faults"]["policies"]["bus"]
        def checked_write(slave, index, sub, value, size, signed=False):
            slave.sdo_write(index, sub, int(value).to_bytes(size, "little", signed=signed))
            if read_int(slave, index, sub, size, signed) != value:
                raise RuntimeError(f"{slave.name} 参数 {index:04X}:{sub:02X} 回读不一致")
        for kind, modules in (("ai", self.ai_modules), ("ao", self.ao_modules)):
            for module_index, (slave, _) in enumerate(modules):
                position = self.master.slaves.index(slave)+1
                module = module_settings(configuration,position,slave.name)
                selection = module.get("filter",h["filter"])
                parameter = 0x1C33 if kind == "ai" else 0x1C32
                supported = read_int(slave, parameter, 4, 2)
                minimum = read_int(slave, parameter, 5, 4)
                mode = read_int(slave, parameter, 1, 2)
                if h["sync"] != "current":
                    requested = {"freerun": 0, "sm": 1, "dc": (3 if supported & 8 else 2) if kind == "ai" else (2 if supported & 4 else 3)}[h["sync"]]
                    mask = (4|8) if h["sync"] == "dc" else 1 << requested
                    if not supported & mask:
                        raise ValueError(f"{slave.name} 不支持选择的同步模式")
                    if minimum and h["period_ms"]*1000000 < minimum:
                        raise ValueError(f"{slave.name} 周期低于设备报告的最小周期")
                    if h["sync"] != "dc":
                        slave.dc_sync(False, 0)
                    if kind == "ai":
                        selected_filter = selection
                        if h["sync"] in ("sm", "dc"):
                            selected_filter = "off"
                        elif h["sync"] == "freerun" and selected_filter == "current":
                            selected_filter = "fir50"
                        self.set_filter(slave, selected_filter, checked_write)
                    checked_write(slave, parameter, 1, requested, 2)
                    mode = requested
                elif kind == "ai" and selection != "current":
                    self.set_filter(slave, selection, checked_write)
                    mode = read_int(slave, parameter, 1, 2)
                detail = dict(id=f"s{position}:{slave.name}",position=position,name=slave.name, kind=kind, sync_mode=mode, supported_modes=supported,
                              minimum_cycle_ns=minimum)
                if kind == "ai":
                    detail.update(filter_enabled=read_int(slave, 0x8000, 6, 1), filter_index=read_int(slave, 0x8000, 0x15, 2))
                else:
                    detail["outputs"] = []
                    for channel in range(4):
                        physical = self.channels["ao"][module_index*4+channel]["id"]
                        profile = profiles.get(physical, {})
                        self.zero_values[module_index*4+channel] = drive_voltage(0, profile)
                        rule = policies.get(physical, {"action": "zero", "voltage": 0})
                        value = 0 if rule["action"] != "value" else rule["voltage"]
                        idx = 0x8000+channel*16
                        checked_write(slave, idx, 0x13, raw_voltage(drive_voltage(value, profile)), 2, True)
                        checked_write(slave, idx, 5, 2 if rule["action"] == "hold" else 0, 1)
                        detail["outputs"].append(dict(channel=channel+1, watchdog_action=rule["action"], default_raw=read_int(slave,idx,0x13,2,True), zero_command_v=self.zero_values[module_index*4+channel]))
                self.hardware_details.append(detail)

    @staticmethod
    def set_filter(slave, selection, checked_write):
        if selection == "current":
            return
        checked_write(slave, 0x8000, 6, 0, 1)
        if selection != "off":
            index = 0 if selection == "fir50" else 1 if selection == "fir60" else int(selection[3:])+1
            checked_write(slave, 0x8000, 0x15, index, 2)
            checked_write(slave, 0x8000, 6, 1, 1)

    def diagnostic_snapshot(self):
        import time
        started = time.perf_counter_ns()
        result = []
        previous = getattr(self, "previous_diagnostic", {})
        for position, slave in enumerate(self.master.slaves, 1):
            row = dict(position=position, name=slave.name)
            for name, address, length in (("al", 0x0130, 6), ("link", 0x0110, 2), ("errors", 0x0300, 14),
                                           ("lost_link", 0x0310, 4), ("watchdog", 0x0440, 4)):
                try:
                    data = slave._fprd(address, length, 500)
                    row[name] = list(data) if len(data) == length else None
                except Exception:
                    row[name] = None
            al = row.get("al")
            row["al_state"] = int.from_bytes(bytes(al[:2]),"little") if al and any(al[:2]) else None
            row["al_status"] = int.from_bytes(bytes(al[4:6]),"little") if row["al_state"] else None
            if row["al_state"] is None:
                for name in ("link","errors","lost_link","watchdog"):
                    row[name] = None
            old = previous.get(position, {})
            row["counter_changes"] = {}
            for name in ("errors", "lost_link", "watchdog"):
                values, before = row.get(name), old.get(name)
                if values is not None:
                    row["counter_changes"][name] = [dict(value=v, delta=(v-before[i]) if before and v>=before[i] else None, saturated=v==255, reset=bool(before and v<before[i])) for i,v in enumerate(values)]
            result.append(row)
        previous_timeout = getattr(self.master,"sdo_read_timeout",700000)
        self.master.sdo_read_timeout = 2000
        positions = {id(slave):i+1 for i,slave in enumerate(self.master.slaves)}
        try:
            for (slave, _), detail in zip(self.ai_modules + self.ao_modules, getattr(self, "hardware_details", [])):
                detail["sync_error"],detail["sync_counters"] = None,None
                if not next((r["al_state"] for r in result if r["position"]==positions.get(id(slave))),None):
                    continue
                try:
                    parameter = 0x1C33 if detail["kind"] == "ai" else 0x1C32
                    detail["sync_error"] = bool(read_int(slave, parameter, 0x20, 1))
                    detail["sync_counters"] = {name: read_int(slave,parameter,sub,2) for name,sub in (("missed_events",0x0B),("cycle_exceeded",0x0C),("short_shift",0x0D))}
                except Exception:
                    pass
        finally:
            self.master.sdo_read_timeout = previous_timeout
        self.previous_diagnostic = {r["position"]: {k:r.get(k) for k in ("errors","lost_link","watchdog")} for r in result}
        return dict(time_ns=time.perf_counter_ns(), duration_ms=(time.perf_counter_ns()-started)/1e6, slaves=result)


    def parameter_poll_start(self):
        """生成一轮只读任务；由通信循环每轮最多执行一个字段。"""
        jobs = []
        positions = {id(s): i+1 for i,s in enumerate(self.master.slaves)}
        for slave, _ in self.ai_modules + self.ao_modules:
            position = positions[id(slave)]
            kind = "ai" if slave.id == EXPECTED[1][0] else "ao"
            ident = f"s{position}:{slave.name}"
            self.parameter_modes.setdefault(ident,None)
            jobs.append(("sync", slave, ident, kind, None))
            if kind == "ai":
                jobs.append(("filter", slave, ident, kind, None))
            else:
                jobs.append(("watchdog", slave, ident, kind, None))
                for index in range(4):
                    jobs.append(("policy", slave, ident, kind, index))
        self.parameter_jobs = iter(jobs)
        self.parameter_polling = True

    def parameter_poll_step(self):
        if not getattr(self, "parameter_polling", False):
            return None
        try:
            field, slave, ident, kind, index = next(self.parameter_jobs)
        except StopIteration:
            self.parameter_polling = False
            return None
        started = time.perf_counter_ns()
        previous = getattr(self.master,"sdo_read_timeout",700000)
        self.master.sdo_read_timeout = 2000
        path = ("modules/"+ident+"/"+field if field in ("filter","watchdog") else ident+"/sync")
        if field == "watchdog":
            path = "modules/"+ident+"/watchdog_ms"
        if field == "policy":
            path = f"faults/policies/bus/{ident}:AO{index+1}"
        value, error = None, None
        try:
            if field == "sync":
                parameter = 0x1C33 if kind == "ai" else 0x1C32
                raw = read_int(slave, parameter, 1, 2)
                value = {0:"freerun",1:"sm",2:"dc",3:"dc"}.get(raw)
                self.parameter_modes[ident] = value
                for detail in self.hardware_details:
                    if detail.get("id") == ident:
                        detail["sync_mode"] = raw
            elif field == "filter":
                enabled = read_int(slave,0x8000,6,1)
                code = read_int(slave,0x8000,0x15,2)
                value = ("off" if not enabled else "fir50" if code == 0 else "fir60" if code == 1
                         else f"iir{code-1}" if 2 <= code <= 9 else None)
            elif field == "watchdog":
                value = round(slave.get_watchdog("processdata"))
            else:
                from .calibration import calibrated, drive_voltage
                channel = ident+f":AO{index+1}"
                profile = (self.configuration or {}).get("channels",{}).get(channel,{})
                action = read_int(slave,0x8000+16*index,5,1)
                raw = read_int(slave,0x8000+16*index,0x13,2,True)
                if action not in (0,2):
                    raise ValueError("未知硬件看门狗模式")
                target = calibrated(volts(raw),profile)
                configured = (self.configuration or {}).get("faults",{}).get("policies",{}).get("bus",{}).get(channel,{})
                if configured.get("action")=="value" and raw==raw_voltage(drive_voltage(configured.get("voltage",0),profile)):
                    target = configured["voltage"]
                is_zero = raw == raw_voltage(drive_voltage(0,profile))
                value = dict(action="hold" if action == 2 else "zero" if is_zero else "value",
                             voltage=0 if action == 2 or is_zero else target)
        except Exception as exc:
            error = str(exc)
            if field == "sync":
                self.parameter_modes[ident] = None
        finally:
            self.master.sdo_read_timeout = previous
        result = dict(path=path,value=value,time_ns=time.perf_counter_ns(),
                      duration_ms=(time.perf_counter_ns()-started)/1e6,error=error)
        self.parameter_values[path] = value
        modes = list(self.parameter_modes.values())
        self.parameter_values["hardware/sync"] = modes[0] if modes and all(m == modes[0] for m in modes) else "mixed"
        return result

    def exchange(self, values):
        if len(values) != len(self.channels["ao"]):
            raise ValueError("输出通道数量不匹配")
        for i, (slave, mapping) in enumerate(self.ao_modules):
            slave.output = mapping.encode(values[i*4:i*4+4])
        self.last_send_ns = time.perf_counter_ns()
        self.master.send_processdata()
        try:
            return self.master.receive_processdata((getattr(self, "configuration", None) or {}).get("hardware", {}).get("receive_timeout_us", 2000))
        finally:
            self.last_receive_ns = time.perf_counter_ns()

    def check_op(self):
        self.master.read_state()
        self.devices = identities(self.master)
        return all(s.state == OP and s.al_status == 0 for s in self.master.slaves)

    def read(self):
        raw, flags = [], []
        for slave, mapping in self.ai_modules:
            r, f = mapping.decode(slave.input)
            raw.extend(r)
            flags.extend(f)
        return raw, flags

    def close(self, safe=True):
        zero_ack = False
        try:
            if not self.opened:
                return False
            if self.output_ready and safe:
                zero_wkc = self.exchange(self.zero_values)
                zero_ack = self.check_op() and zero_wkc == self.expected
        finally:
            try:
                if self.opened:
                    try:
                        if self.devices:
                            self.master.state = INIT
                            self.master.write_state()
                    finally:
                        self.master.close()
                        self.opened = False
            finally:
                self.bus_lock.close()
        return zero_ack
