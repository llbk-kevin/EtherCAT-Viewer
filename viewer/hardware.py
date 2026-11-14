import ctypes
import json
import os
import re
import subprocess
import time

from .pdo import Mapping, read_int, read_mapping

GUID = "9E210308-47E1-4EA0-AAB8-42FB54A8A7AC"
EXPECTED = [(0x044C2C52, "EK1100"), (0x0C203052, "EL3104"), (0x10263052, "EL4134")]
INIT, PREOP, SAFEOP, OP = 1, 2, 4, 8

def load_driver():
    try:
        import pysoem
    except ImportError as exc:
        raise RuntimeError("无法加载 PySOEM。请安装 Npcap 并启用 WinPcap API 兼容模式。") from exc
    return pysoem

def adapters():
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-NetAdapter -Physical | Select-Object Name,InterfaceGuid,Status,LinkSpeed,"
        "InterfaceDescription | ConvertTo-Json -Compress"
    )
    system = os.environ.get("SystemRoot", r"C:\Windows")
    result = subprocess.run(
        [os.path.join(system, r"System32\WindowsPowerShell\v1.0\powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", command],
        check=True, capture_output=True, encoding="utf-8-sig", timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = json.loads(result.stdout or "[]")
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
        guid = row["InterfaceGuid"].strip("{}").upper()
        ident = fr"\Device\NPF_{{{guid}}}"
        items.append(dict(guid=guid, name=row["Name"], description=row["InterfaceDescription"],
                          status=row["Status"], speed=row["LinkSpeed"],
                          interface=visible.get(ident.upper()), target=guid == GUID))
    return {"items": items, "error": error}

def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

def check_adapter(ifname):
    match = re.fullmatch(r"\\Device\\NPF_\{([0-9A-F-]{36})\}", ifname, re.I)
    if not match or match[1].upper() != GUID:
        raise ValueError("请选择已确认的 EtherCAT 网卡「以太网 3」")

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
    def __init__(self):
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = kernel.CreateMutexW(None, False, f"Local\\EtherCAT-Viewer-{GUID}")
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

class Hardware:
    def __init__(self, ifname):
        check_adapter(ifname)
        self.master = load_driver().Master()
        self.opened = False
        self.output_ready = False
        self.devices = []
        self.mapping = {}
        self.expected = None
        self.watchdog = None
        self.bus_lock = BusLock()
        try:
            self.master.open(ifname)
            self.opened = True
            self.master.config_init()
            self.devices = identities(self.master)
            if [(s.man, s.id) for s in self.master.slaves] != [(2, p) for p, _ in EXPECTED]:
                raise ValueError(f"从站链与现场记录不一致：{self.devices}")
            _, self.ai_slave, self.ao_slave = self.master.slaves
        except Exception:
            self.close()
            raise

    def configure(self):
        for ch in range(4):
            idx = 0x8000 + 16 * ch
            signed_default(self.ai_slave, idx)
            signed_default(self.ao_slave, idx)
            if read_int(self.ao_slave, idx, 5, 1) != 0 or read_int(self.ao_slave, idx, 0x13, 2, True) != 0:
                raise ValueError(f"EL4134 通道 {ch+1} 的看门狗替代值需要核实为零")
        inputs = read_mapping(self.ai_slave, 0x1C13)
        outputs = read_mapping(self.ao_slave, 0x1C12)
        self.master.manual_state_change = 1
        self.master.config_map()
        # Enable the ESC watchdog trigger while the output SM is in PREOP.
        sm = self.ao_slave._fprd(0x0810, 8, 2000)
        if int.from_bytes(sm[2:4], "little") != 8 or not sm[4] & 0x04:
            raise RuntimeError(f"EL4134 SM2 方向或长度异常：{sm.hex()}")
        if not sm[4] & 0x40:
            self.ao_slave._fpwr(0x0816, bytes([sm[6] & ~1]), 2000)
            try:
                self.ao_slave._fpwr(0x0814, bytes([sm[4] | 0x40]), 2000)
            finally:
                self.ao_slave._fpwr(0x0816, bytes([sm[6]]), 2000)
        self.master.state = SAFEOP
        self.master.write_state()
        self.master.state_check(SAFEOP, 2000000)
        self.devices = identities(self.master)
        if any(slave.state != SAFEOP for slave in self.master.slaves):
            raise RuntimeError(f"未进入 SAFEOP：{self.devices}")
        self.input = Mapping(inputs, len(self.ai_slave.input))
        self.output = Mapping(outputs, len(self.ao_slave.output), output=True)
        self.mapping = {"ai": self.input.describe(), "ao": self.output.describe(),
                        "sm2_control_original": sm[4],
                        "sm2_control_active": self.ao_slave._fprd(0x0814, 1, 2000)[0]}
        self.expected = self.master.expected_wkc
        if self.expected <= 0:
            raise RuntimeError("PDO 预期 WKC 无效")
        self.watchdog = self.ao_slave.get_watchdog("processdata")
        if not 90 <= self.watchdog <= 110:
            old = self.watchdog
            self.ao_slave.set_watchdog("processdata", 100)
            self.watchdog = self.ao_slave.get_watchdog("processdata")
            self.mapping["watchdog_previous_ms"] = old
        if not 90 <= self.watchdog <= 110:
            raise RuntimeError(f"过程数据看门狗回读异常：{self.watchdog} ms")
        # SM2 control bit 6 enables the process-data watchdog.
        if not self.ao_slave._fprd(0x0814, 1, 2000)[0] & 0x40:
            raise RuntimeError("EL4134 SM2 未启用过程数据看门狗")
        self.output_ready = True
        self.exchange([0] * 4)
        self.master.state = OP
        self.master.write_state()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            wkc = self.exchange([0] * 4)
            if self.master.state_check(OP, 2000) == OP and wkc == self.expected and self.check_op():
                return
            time.sleep(0.01)
        raise RuntimeError(f"未进入 OP：{identities(self.master)}")

    def exchange(self, values):
        self.ao_slave.output = self.output.encode(values)
        self.master.send_processdata()
        return self.master.receive_processdata(2000)

    def check_op(self):
        self.devices = identities(self.master)
        return all(s.state == OP and s.al_status == 0 for s in self.master.slaves)

    def read(self):
        return self.input.decode(self.ai_slave.input)

    def close(self):
        zero_ack = False
        try:
            if not self.opened:
                return False
            if self.output_ready:
                zero_wkc = self.exchange([0] * 4)
                in_op = self.check_op()
                zero_ack = zero_wkc == self.expected and in_op
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
