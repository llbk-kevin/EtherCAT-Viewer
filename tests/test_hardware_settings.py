"""用可回读的 CoE/ESC 模型验证硬件配置，而非只验证 UI 选项。"""
from types import SimpleNamespace
import pytest
from viewer.hardware import Hardware, EXPECTED, channel_catalog
from viewer.pdo import raw_voltage
from viewer.settings import defaults
from test_hardware_topology import Slave, Master

class ConfiguredSlave(Slave):
    def __init__(self,product,name,position):
        super().__init__(product,name,position)
        self.rev = (0x03FD0000 if self.is_ao else 0x00140000)
        self.parameters = {(0x1C32,1):1,(0x1C33,1):1,(0x1C32,4):0xC00B,(0x1C33,4):0xD00B,
                           (0x8000,6):0,(0x8000,0x15):0}
        self.watchdog = 100
        self.dc = None
    def sdo_read(self,index,sub,size):
        if (index,sub) in self.parameters:
            value = self.parameters[index,sub]
            return value.to_bytes(size,"little",signed=value<0)
        return super().sdo_read(index,sub,size)
    def sdo_write(self,index,sub,data):
        self.parameters[index,sub] = int.from_bytes(data,"little",signed=sub==0x13)
    def get_watchdog(self,_kind):
        return self.watchdog
    def set_watchdog(self,_kind,value):
        self.watchdog = value
    def dc_sync(self,*args):
        self.dc = args
    def _fprd(self,address,size,timeout):
        if 0x0810 <= address < 0x0818:
            return super()._fprd(address,size,timeout)
        return bytes(size)

class ConfiguredMaster(Master):
    def config_dc(self):
        return True


def bus():
    slaves = [ConfiguredSlave(p,n,i) for i,(p,n) in enumerate(EXPECTED,1)]
    obj = Hardware.__new__(Hardware)
    obj.master = ConfiguredMaster(slaves,3)
    obj.channels,obj.fingerprint = channel_catalog(slaves)
    obj.ai_modules = [[slaves[1],None]]
    obj.ao_modules = [[slaves[2],None]]
    obj.opened,obj.output_ready = True,False
    obj.mapping,obj.devices = {},[]
    obj.bus_lock = SimpleNamespace(close=lambda:None)
    return obj

@pytest.mark.parametrize("sync,filter",[("current","current"),("freerun","fir50"),("freerun","fir60"),
                                       ("freerun","iir1"),("freerun","iir8"),("sm","off"),("dc","off")])
def test_sync_filter_watchdog_readback(sync,filter):
    hardware = bus()
    config = defaults()
    config["hardware"]["sync"] = sync
    config["modules"]["s2:EL3104"] = dict(filter=filter)
    hardware.configure(config)
    assert hardware.watchdog == 600
    details = hardware.hardware_details
    if sync == "dc":
        assert [d["sync_mode"] for d in details] == [3,3]
        assert hardware.master.slaves[1].dc == (True,10000000,-100000,25000)
        assert hardware.master.slaves[2].dc == (True,10000000,0,100000)
    if sync == "freerun":
        assert details[0]["filter_enabled"] == 1 and details[0]["sync_mode"] == 0
    if sync == "sm":
        assert details[0]["filter_enabled"] == 0 and details[0]["sync_mode"] == 1
    assert hardware.close()


def test_hardware_hold_and_calibrated_default_are_checked():
    hardware = bus()
    config = defaults()
    ident = hardware.channels["ao"][0]["id"]
    config["channels"][ident] = dict(calibration=dict(enabled=True,gain=1,offset=.01))
    config["faults"]["policies"]["bus"][ident] = dict(action="hold")
    hardware.configure(config)
    slave = hardware.master.slaves[2]
    assert slave.parameters[0x8000,5] == 2
    assert slave.parameters[0x8000,0x13] == raw_voltage(-.01)
    assert hardware.zero_values[0] == -.01
    hardware.exchange([.5,0,0,0])
    hardware.close(safe=False)
    assert slave.output != bytes(8)


def test_failed_parameter_readback_aborts_configuration():
    hardware = bus()
    slave = hardware.master.slaves[2]
    slave.sdo_write = lambda *_args:None
    config = defaults()
    config["faults"]["policies"]["bus"][hardware.channels["ao"][0]["id"]] = dict(action="hold")
    with pytest.raises(RuntimeError,match="回读不一致"):
        hardware.configure(config)


def test_real_state_read_precedes_op_check():
    hardware = bus()
    hardware.master.read_state = lambda:setattr(hardware.master.slaves[1],"state",4)
    assert not hardware.check_op()


def test_normal_close_sends_calibrated_physical_zero():
    hardware=bus()
    config=defaults()
    ident=hardware.channels["ao"][0]["id"]
    config["channels"][ident]=dict(calibration=dict(enabled=True,gain=1.02,offset=.03))
    hardware.configure(config)
    hardware.exchange([.5,0,0,0])
    assert hardware.close()
    raw=int.from_bytes(hardware.master.slaves[2].output[:2],"little",signed=True)
    assert raw == raw_voltage(-.03/1.02)
    assert raw != 0


def test_unreachable_diagnostic_is_unknown_and_skips_coe():
    hardware=bus();hardware.configure(defaults())
    for slave in hardware.master.slaves:
        slave._fprd=lambda _address,size,_timeout:bytes(size)
        slave.sdo_read=lambda *_args,**_kwargs:pytest.fail("unreachable diagnostic must not wait for CoE")
    hardware.master.sdo_read_timeout=700000
    snapshot=hardware.diagnostic_snapshot()
    assert all(r["al_state"] is None and r["errors"] is None and r["lost_link"] is None for r in snapshot["slaves"])
    assert hardware.master.sdo_read_timeout==700000
    assert all(d["sync_error"] is None for d in hardware.hardware_details)


def test_diagnostic_coe_timeout_is_bounded_and_restored():
    hardware=bus();hardware.configure(defaults())
    hardware.master.sdo_read_timeout=700000
    seen=[]
    for slave in hardware.master.slaves:
        previous=slave._fprd
        slave._fprd=lambda address,size,timeout,previous=previous:bytes([8])+bytes(size-1) if address==0x130 else previous(address,size,timeout)
        previous_sdo=slave.sdo_read
        def read(index,sub,size,previous_sdo=previous_sdo):
            seen.append(hardware.master.sdo_read_timeout)
            return previous_sdo(index,sub,size)
        slave.sdo_read=read
    hardware.diagnostic_snapshot()
    assert seen and set(seen)=={2000} and hardware.master.sdo_read_timeout==700000
