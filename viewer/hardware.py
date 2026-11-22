import ctypes
import json
import re
import subprocess
import time
from uuid import UUID

from .paths import powershell_path
from .pdo import Mapping, read_int, read_mapping

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

    def configure(self):
        pending = []
        for modules, output in ((self.ai_modules, False), (self.ao_modules, True)):
            for pair in modules:
                slave = pair[0]
                for ch in range(4):
                    idx = 0x8000 + 16 * ch
                    signed_default(slave, idx)
                    if output and (read_int(slave, idx, 5, 1) != 0 or
                                   read_int(slave, idx, 0x13, 2, True) != 0):
                        raise ValueError(f"{slave.name} 通道 {ch+1} 看门狗替代值必须为零")
                pending.append((pair, output, read_mapping(slave, 0x1C12 if output else 0x1C13)))
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
            value = slave.get_watchdog("processdata")
            if not 90 <= value <= 110:
                slave.set_watchdog("processdata", 100)
                value = slave.get_watchdog("processdata")
            if not 90 <= value <= 110 or not slave._fprd(0x0814, 1, 2000)[0] & 0x40:
                raise RuntimeError(f"{slave.name} 过程数据看门狗回读异常：{value} ms")
            watchdogs.append(value)
        self.watchdog = max(watchdogs) if watchdogs else None
        self.mapping["watchdogs_ms"] = watchdogs
        self.expected = self.master.expected_wkc
        if self.expected <= 0:
            raise RuntimeError("PDO 预期 WKC 无效")
        self.master.state = SAFEOP
        self.master.write_state()
        self.master.state_check(SAFEOP, 2000000)
        self.devices = identities(self.master)
        if any(s.state != SAFEOP for s in self.master.slaves):
            raise RuntimeError(f"未进入 SAFEOP：{self.devices}")
        self.exchange([0] * len(self.channels["ao"]))
        self.master.state = OP
        self.master.write_state()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            wkc = self.exchange([0] * len(self.channels["ao"]))
            if self.master.state_check(OP, 2000) == OP and wkc == self.expected and self.check_op():
                return
            time.sleep(0.01)
        raise RuntimeError(f"未进入 OP：{identities(self.master)}")

    def exchange(self, values):
        if len(values) != len(self.channels["ao"]):
            raise ValueError("输出通道数量不匹配")
        for i, (slave, mapping) in enumerate(self.ao_modules):
            slave.output = mapping.encode(values[i*4:i*4+4])
        self.last_send_ns = time.perf_counter_ns()
        self.master.send_processdata()
        try:
            return self.master.receive_processdata(2000)
        finally:
            self.last_receive_ns = time.perf_counter_ns()

    def check_op(self):
        self.devices = identities(self.master)
        return all(s.state == OP and s.al_status == 0 for s in self.master.slaves)

    def read(self):
        raw, flags = [], []
        for slave, mapping in self.ai_modules:
            r, f = mapping.decode(slave.input)
            raw.extend(r)
            flags.extend(f)
        return raw, flags

    def close(self):
        zero_ack = False
        try:
            if not self.opened:
                return False
            if self.output_ready:
                zero_wkc = self.exchange([0] * len(self.channels["ao"]))
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
