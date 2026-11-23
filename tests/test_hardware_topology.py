"""通过完整的从站/PDO 模型验证多模块配置、收发和清理。"""
import struct
from types import SimpleNamespace

import pytest

from viewer.hardware import EXPECTED, Hardware, channel_catalog
from viewer.pdo import raw_voltage


class Slave:
    def __init__(self, product, name, position):
        self.man, self.id, self.rev, self.name = 2, product, 1, name
        self.state, self.al_status = 2, 0
        self.is_ao = product == EXPECTED[2][0]
        self.entries = []
        for channel in range(4):
            index = (0x7000 if self.is_ao else 0x6000)+channel*16
            if not self.is_ao:
                self.entries.append(index << 16 | 16)
            self.entries.append(index << 16 | (1 if self.is_ao else 0x11) << 8 | 16)
        self.input = b"".join(struct.pack("<Hh", 0, position*100+i) for i in range(4)) if not self.is_ao else b""
        self.output = bytes(8) if self.is_ao else b""
        self.sm = bytearray([0, 0, 8, 0, 0x24, 0, 1, 0])

    def sdo_read(self, index, sub, size):
        if index in (0x1C12, 0x1C13):
            value = 1 if sub == 0 else 0x1600 if self.is_ao else 0x1A00
        elif index in (0x1600, 0x1A00):
            value = len(self.entries) if sub == 0 else self.entries[sub-1]
        else:
            value = 0
        return value.to_bytes(size, "little")

    def _fprd(self, address, size, _timeout):
        return bytes(self.sm[address-0x0810:address-0x0810+size])

    def _fpwr(self, address, value, _timeout):
        self.sm[address-0x0810:address-0x0810+len(value)] = value

    def get_watchdog(self, _kind):
        return 100


class Master:
    def __init__(self, slaves, expected):
        self.slaves, self.expected_wkc = slaves, expected
        self.state, self.closed = 2, False

    def read_state(self):
        pass

    def config_map(self):
        pass

    def write_state(self):
        for slave in self.slaves:
            slave.state = self.state

    def state_check(self, _state, _timeout):
        return self.state

    def send_processdata(self):
        pass

    def receive_processdata(self, _timeout):
        return self.expected_wkc

    def close(self):
        self.closed = True


@pytest.mark.parametrize("nai,nao", [(1, 1), (4, 4), (8, 2), (0, 4), (4, 0)])
def test_full_multi_module_configure_exchange_and_zero(nai, nao):
    definitions = [EXPECTED[0]]
    for i in range(max(nai, nao)):
        # AO before AI verifies that module order does not imply the signal kind.
        if i < nao:
            definitions.append(EXPECTED[2])
        if i < nai:
            definitions.append(EXPECTED[1])
    slaves = [Slave(product, name, position) for position, (product, name) in enumerate(definitions, 1)]
    bus = Hardware.__new__(Hardware)
    bus.master = Master(slaves, nai+2*nao)
    bus.channels, bus.fingerprint = channel_catalog(slaves)
    bus.ai_modules = [[s, None] for s in slaves[1:] if not s.is_ao]
    bus.ao_modules = [[s, None] for s in slaves[1:] if s.is_ao]
    bus.opened, bus.output_ready = True, False
    bus.mapping, bus.devices = {}, []
    released = []
    bus.bus_lock = SimpleNamespace(close=lambda: released.append(True))
    bus.configure()
    assert len(bus.channels["ai"]) == nai*4 and len(bus.channels["ao"]) == nao*4
    assert bus.expected == nai+2*nao
    assert len(bus.mapping["watchdogs_ms"]) == nao
    outputs = [(i%20-10)/10 for i in range(nao*4)]
    assert bus.exchange(outputs) == bus.expected
    for i, (slave, _mapping) in enumerate(bus.ao_modules):
        assert struct.unpack("<4h", slave.output) == tuple(raw_voltage(v) for v in outputs[i*4:i*4+4])
        assert slave.sm[4] == 0x64
    raw, flags = bus.read()
    assert len(raw) == len(flags) == nai*4
    assert all(not any(flag.values()) for flag in flags)
    assert bus.close() and bus.master.closed and released == [True]
    assert all(slave.output == bytes(8) for slave, _ in bus.ao_modules)
