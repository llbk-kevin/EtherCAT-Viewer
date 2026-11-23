import multiprocessing as mp
from queue import Queue
import time
from unittest.mock import Mock

import pytest

from viewer.pdo import read_int
from viewer.hardware import signed_default
from viewer.worker import LEASE_NS, Worker

def worker():
    return Worker(Queue(), Queue(), Queue(), mp.Array("q", 2), mp.Event())

@pytest.mark.parametrize("fault", ["wkc", "state"])
def test_bus_fault_invalidates_sample_and_clears_nonzero_command(fault):
    w = worker()
    w.settings["faults"]["consecutive"] = 1
    now = time.perf_counter_ns()
    w.lease[:] = [1, now]
    w.connection, w.owner = "OP", 1
    w.values[0] = 0.5
    bus = w.bus = Mock()
    w.expected = 3
    bus.exchange.return_value = 0 if fault == "wkc" else 3
    bus.check_op.return_value = False
    bus.close.return_value = False
    bus.last_send_ns = now
    bus.last_receive_ns = now + 100_000
    sample = w.cycle(now)
    assert sample["ai"] == [None] * 4
    assert sample["ao"] == [None] * 4 and not sample["ao_valid"]
    assert w.values == [0] * 4
    assert w.connection == "FAULT" and not w.owner
    assert "无法确认" in w.zero
    bus.close.assert_called_once()

def test_expired_controller_lease_cancels_wave_and_output():
    w = worker()
    now = time.perf_counter_ns()
    w.lease[:] = [1, now - LEASE_NS]
    w.owner, w.values[3], w.wave[0] = 1, 0.8, (1.0, now)
    assert w.cycle(now)["ao"] == [None] * 4
    assert w.values == [0] * 4
    assert w.owner == 0 and not w.wave

def test_non_default_representation_is_rejected():
    slave = Mock(name="EL4134")
    slave.name = "EL4134"
    slave.sdo_read.side_effect = [b"\x00", b"\x01"]
    with pytest.raises(ValueError, match="非默认"):
        signed_default(slave, 0x8000)

def test_sdo_read_size_is_verified():
    slave = Mock()
    slave.sdo_read.return_value = b"\x00\x00"
    with pytest.raises(ValueError, match="长度"):
        read_int(slave, 0x8000, 2, 1)
