import asyncio
import copy
import json
import multiprocessing as mp
from queue import Queue
import socket
import struct
import time
from types import SimpleNamespace

import pytest

from viewer.bridge import config, protocol as p
from viewer.bridge.runtime import Bridge
from viewer.bridge.sampling import Sampler
from viewer.generator import TableWave, build_table
from viewer.hardware import EXPECTED, channel_catalog, validate_topology
from viewer.pdo import raw_voltage
from viewer.worker import Worker


def run(coro):
    return asyncio.run(coro)


def source(seq, ns, values=None):
    values = values if values is not None else [1, -2, 3, -4]
    return dict(seq=seq, session=1, time_ns=ns, host_send_ns=ns, host_return_ns=ns+1000,
                ai=values, valid=[True]*len(values), interval_ms=10)


def catalog(nai=4, nao=4):
    return {k: [dict(id=f"{k}{i}", label=f"{k}{i}", slave_position=2+i//4,
                     module="EL3104" if k == "ai" else "EL4134", channel=i%4+1)
                for i in range(n)] for k, n in (("ai", nai), ("ao", nao))}


def test_topology_multiple_modules_and_channel_identity():
    slaves = [SimpleNamespace(man=2, id=product, rev=1, name=name)
              for product, name in [EXPECTED[0], EXPECTED[2], EXPECTED[1], EXPECTED[1], EXPECTED[2]]]
    validate_topology(slaves)
    channels, signature = channel_catalog(slaves)
    assert len(channels["ai"]) == len(channels["ao"]) == 8
    assert channels["ai"][0]["id"] == "s3:EL3104:AI1"
    slaves[-1].rev = 2
    assert channel_catalog(slaves)[1] != signature
    slaves[-1].id = 123
    with pytest.raises(ValueError, match="不支持"):
        validate_topology(slaves)


@pytest.mark.parametrize("nai,nao", [(4, 4), (16, 16), (32, 8), (0, 4), (8, 0)])
def test_dynamic_presets_and_validation(nai, nao):
    channels = catalog(nai, nao)
    for profile in ("legacy", "extended"):
        data = config.preset(channels, "signature", profile)
        assert config.validate_config(data, channels, "signature", ["127.0.0.1"]) == data
    legacy = config.preset(channels, "signature", "legacy")
    outputs = [x for c in legacy["cards"] for x in c["ao"] if x]
    assert len(outputs) == nao == len(set(outputs))


def test_invalid_config_scope_and_topology():
    channels = catalog()
    data = config.preset(channels, "signature", "legacy")
    for mutate, message in [
        (lambda c: c.update(fingerprint="old"), "拓扑"),
        (lambda c: c["cards"][0].update(port=8765), "端口"),
        (lambda c: c["cards"][0].update(host="192.168.10.1"), "本机"),
        (lambda c: c["cards"][1]["ao"].__setitem__(0, "ao0"), "重复"),
        (lambda c: c["cards"][0]["ai"].__setitem__(0, "unknown"), "不存在"),
        (lambda c: c["cards"][0].update(send_ms=True), "整数"),
        (lambda c: c["cards"][0].update(allow=[]), "允许"),
    ]:
        candidate = copy.deepcopy(data)
        mutate(candidate)
        with pytest.raises(ValueError, match=message):
            config.validate_config(candidate, channels, "signature", ["127.0.0.1"])


def test_legacy_fragmentation_coalescing_and_variable_length():
    async def check():
        reader = asyncio.StreamReader()
        wave = bytes.fromhex("AA55FF5AA601E8FD01AA05000055AA")
        # two points, divider 65000; sample data includes the apparent header.
        wave = b"\xaa\x55\xff\x5a\xa6" + struct.pack("<BHBHH", 1, 65000, 1, 0x55aa, 0) + b"\x55\xaa"
        echo = p.legacy_command(0xA3D1, b"\x7f")
        async def feed():
            for byte in wave+echo:
                reader.feed_data(bytes([byte]))
                await asyncio.sleep(0)
        task = asyncio.create_task(feed())
        op, body = await p.read_legacy(reader)
        assert op == 0xA65A and struct.unpack_from("<H", body, 4)[0] == 0x55aa
        assert await p.read_legacy(reader) == (0xA3D1, b"\x7f")
        await task
    run(check())


def test_extended_length_version_and_legacy_length_rejected():
    async def check():
        reader = asyncio.StreamReader()
        packet = p.extended_json(p.CAPS, {"id": 1})
        reader.feed_data(packet+packet)
        assert json.loads((await p.read_extended(reader))[1]) == {"id": 1}
        assert (await p.read_extended(reader))[0] == p.CAPS
        for bad, parser in [(b"\xaa\x55\xff"+struct.pack("<HI", p.CAPS, p.MAX_PAYLOAD+1), p.read_extended),
                            (packet[:9]+b"\x02"+packet[10:], p.read_extended),
                            (bytes.fromhex("AA550859A6"), p.read_legacy)]:
            r = asyncio.StreamReader()
            r.feed_data(bad)
            with pytest.raises(ValueError):
                await parser(r)
    run(check())


def test_dac_mapping_and_adc_offsets():
    assert p.dac_voltage(0, "bipolar") == -10
    assert p.dac_voltage(4095, "bipolar") == 10
    assert p.dac_voltage(2048, "bipolar") == pytest.approx(10/4095)
    assert p.dac_voltage(0, "original") == 0
    assert p.dac_voltage(4095, "original") == 3.3
    for value in (-1, 4096, True):
        with pytest.raises(ValueError):
            p.dac_voltage(value, "bipolar")
    data = struct.pack("<"+"h"*720, *([-32768, 32767]*360))
    frame = p.legacy_adc(0xffffffff+2, data)
    assert len(frame) == 1460 and struct.unpack_from("<I", frame, 12)[0] == 1
    assert frame[16:1456] == data and frame[2:12] == bytes(10)


def test_hold_native_gaps_validity_and_clipping():
    sampler = Sampler([[(0, 1)], [], [(1, 1), (0, -1)]], 2000, "hold", 10, 100)
    assert sampler.feed(source(1, 1_000_000_000)) == []
    chunk, count, meta = sampler.feed(source(2, 1_010_000_000))[0]
    assert count == 20 and sampler.held == 19 and len(chunk) == 20*6
    assert struct.unpack_from("<3h", chunk) == (3277, 0, -9830)
    assert meta["mapped"] == [True, False, True]
    assert meta["host_send_ns"] == 1_000_000_000
    with pytest.raises(ValueError, match="序号"):
        sampler.feed(source(4, 1_020_000_000))
    with pytest.raises(ValueError, match="拒绝"):
        Sampler([[]], 2000, "native", 10, 100)
    native = Sampler([[(0, 1)]], 50, "native", 5, 100)
    assert native.feed(source(1, 1_000_000_000))[0][1] == 1
    assert native.feed(source(2, 1_010_000_000)) == []
    assert native.feed(source(3, 1_020_000_000))[0][1] == 1
    with pytest.raises(ValueError, match="过期"):
        native.feed(source(4, 1_220_000_000))
    bad = source(1, 1)
    bad["valid"][0] = False
    with pytest.raises(ValueError, match="无效"):
        Sampler([[(0, 1)]], 100, "hold", 10, 100).feed(bad)


class FakeBus:
    def __init__(self, channels):
        self.channels = channels
        self.devices = []
        self.output = [0.]*len(channels["ao"])
        self.last_send_ns = self.last_receive_ns = 0

    def exchange(self, values):
        self.output = list(values)
        self.last_send_ns = time.perf_counter_ns()
        self.last_receive_ns = self.last_send_ns + 10_000
        return 3

    def read(self):
        values = [self.output[i] if i < len(self.output) else 0 for i in range(len(self.channels["ai"]))]
        return [raw_voltage(v) for v in values], [{} for _ in values]

    def check_op(self):
        return True


def make_worker():
    worker = Worker(Queue(), Queue(), Queue(), mp.Array("q", 2), mp.Event())
    worker.channels = catalog()
    worker.bus = FakeBus(worker.channels)
    worker.values = [0.]*4
    worker.mapping = {"fingerprint": "signature"}
    worker.connection, worker.expected = "OP", 3
    worker.good_since = time.perf_counter_ns()-2_000_000_000
    return worker


def apply(worker, action, **args):
    now = time.perf_counter_ns()
    worker.lease[:] = [1, now]
    worker.command(dict(id="command", action=action, args=args, owner=1), now)
    worker.cycle(time.perf_counter_ns())
    return worker.events.get_nowait()


def test_worker_card_isolation_output_scope_release_and_shared_clock():
    worker = make_worker()
    assert apply(worker, "bridge_setup", cards=[dict(id="a", outputs=[0, 1]), dict(id="b", outputs=[2, 3])])["ok"]
    for card in ("a", "b"):
        assert apply(worker, "bridge_authorize", card=card)["ok"]
        assert apply(worker, "bridge_claim", card=card, token=card)["ok"]
    assert not apply(worker, "bridge_output", card="a", token="a", changes=[dict(index=3, voltage=1)])["ok"]
    assert apply(worker, "bridge_output", card="b", token="b", changes=[dict(index=3, voltage=.5)])["ok"]
    table = [0., 1.]*128
    assert apply(worker, "bridge_table", card="a", token="a",
                 changes=[dict(index=i, table=table, rate=2000) for i in (0, 1)])["ok"]
    previous = dict(worker.wave)
    assert not apply(worker, "bridge_rate", card="a", token="a", rate=200000)["ok"]
    assert worker.wave == previous
    assert apply(worker, "bridge_rate", card="a", token="a", rate=2200)["ok"]
    assert all(isinstance(worker.wave[i], TableWave) and worker.wave[i].rate == 2200 for i in (0, 1))
    assert apply(worker, "bridge_release", card="a", token="a")["ok"]
    assert worker.values[:2] == [0, 0] and not worker.wave and worker.values[3] == .5
    assert not apply(worker, "bridge_claim", card="a", token="new")["ok"]
    assert apply(worker, "zero")["ok"]
    assert worker.values == [0.]*4 and worker.control_mode == "WEB"


def test_table_reclock_and_nonfinite_rejection():
    table = tuple(range(4))
    wave = TableWave(table, 20, 0)
    assert wave.voltage_at(75_000_000) == 1
    assert wave.reclock(40, 75_000_000).voltage_at(75_000_000) == 1
    with pytest.raises(ValueError):
        build_table([float("nan")], 100, 10, 0)
    with pytest.raises(ValueError):
        build_table([0., 1.], 2000, 10, 0)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def fixture_bridge(monkeypatch, profile="legacy", two=False):
    monkeypatch.setattr("viewer.bridge.runtime.local_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(Bridge, "log", lambda self, text: self.events.append(dict(message=text)))
    worker = make_worker()
    manager = None

    async def request(action, args):
        result = apply(worker, action, **args)
        if manager:
            manager.feed(worker.status(), [worker.cycle(time.perf_counter_ns())])
        if not result["ok"]:
            raise ValueError(result["error"])
        return result["result"]

    manager = Bridge(request, lambda: worker.lock("critical"))
    manager.config = config.preset(worker.channels, "signature", profile)
    if not two:
        manager.config["cards"] = manager.config["cards"][:1]
    for card in manager.config["cards"]:
        card["port"] = free_port()
    await manager.start(worker.status())
    for card in manager.cards:
        await manager.authorize(card)

    async def feeder():
        while True:
            worker.lease[:] = [1, time.perf_counter_ns()]
            sample = worker.cycle(time.perf_counter_ns())
            manager.feed(worker.status(), [sample])
            await asyncio.sleep(.01)
    feeder_task = asyncio.create_task(feeder())
    return worker, manager, feeder_task


def test_socket_legacy_finite_capture_second_client_and_disconnect_zero(monkeypatch):
    async def check():
        worker, manager, feeder = await fixture_bridge(monkeypatch)
        card = next(iter(manager.cards.values()))
        reader, writer = await asyncio.open_connection("127.0.0.1", card.config["port"])
        try:
            writer.write(p.legacy_command(0xA659, struct.pack("<BHB", 1, 600, 1)))
            await writer.drain()
            await asyncio.sleep(.04)
            assert worker.values[0] == pytest.approx(600*3.3/4095)
            second, other = await asyncio.open_connection("127.0.0.1", card.config["port"])
            assert await asyncio.wait_for(second.read(1), 1) == b""
            other.close()
            writer.write(p.legacy_command(0xA452, bytes.fromhex("13A4F1C5")))
            await writer.drain()
            info = await asyncio.wait_for(reader.readexactly(46), 1)
            assert info[:5] == bytes.fromhex("AA552E52A4")
            writer.write(p.legacy_command(0xA111, struct.pack("<I", 3)))
            await writer.drain()
            frames = await asyncio.wait_for(reader.readexactly(1460*3), 2)
            assert [struct.unpack_from("<I", frames, i*1460+12)[0] for i in range(3)] == [1, 2, 3]
            assert struct.unpack_from("<h", frames, 16)[0] > 0
            writer.write(p.legacy_command(0xA3D1, b"\x7f"))
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(8), 1) == p.legacy_command(0xFAF5, b"\x80")
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.05)
            assert worker.values[0] == 0 and not card.authorized
            await manager.stop(shutdown=True)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    run(check())


def test_socket_extended_multiple_readers_and_owner(monkeypatch):
    async def check():
        worker, manager, feeder = await fixture_bridge(monkeypatch, "extended")
        card = next(iter(manager.cards.values()))
        pairs = [await asyncio.open_connection("127.0.0.1", card.config["port"]) for _ in range(2)]
        async def command(pair, opcode, args):
            reader, writer = pair
            writer.write(p.extended_json(opcode, args))
            await writer.drain()
            return await asyncio.wait_for(p.read_extended(reader), 2)
        try:
            for reader, _ in pairs:
                assert (await p.read_extended(reader))[0] == p.CAPS
            op, body = await command(pairs[1], p.OUTPUT, dict(channels=[1], volts=[1]))
            assert op == p.ERROR and not json.loads(body)["ok"] and worker.values[0] == 0
            assert (await command(pairs[0], p.CLAIM, {}))[0] == p.CLAIM
            assert (await command(pairs[1], p.CLAIM, {}))[0] == p.ERROR
            assert (await command(pairs[0], p.OUTPUT, dict(channels=[1], volts=[-.5])))[0] == p.OUTPUT
            assert worker.values[0] == -.5
            assert (await command(pairs[1], p.START, dict(rate_hz=2000, blocks=1)))[0] == p.START
            op, body = await asyncio.wait_for(p.read_extended(pairs[1][0]), 2)
            assert op == p.DATA
            meta_len = struct.unpack_from("<I", body)[0]
            meta = json.loads(body[4:4+meta_len])
            assert meta["timestamp_source"] == "host_pdo_exchange" and meta["held_count"] >= 0
            assert struct.unpack_from("<h", body, 4+meta_len)[0] < 0
        finally:
            for _, writer in pairs:
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(.05)
            assert worker.values == [0.]*4
            await manager.stop(shutdown=True)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    run(check())


def test_bridge_lease_expiry_cancels_all_cards():
    worker = make_worker()
    assert apply(worker, "bridge_setup", cards=[dict(id="a", outputs=[0, 1])])["ok"]
    assert apply(worker, "bridge_authorize", card="a")["ok"]
    assert apply(worker, "bridge_claim", card="a", token="owner")["ok"]
    assert apply(worker, "bridge_output", card="a", token="owner", changes=[dict(index=0, voltage=-1)])["ok"]
    worker.lease[:] = [1, time.perf_counter_ns()-3_000_000_000]
    worker.cycle(time.perf_counter_ns())
    assert worker.control_mode == "WEB" and worker.values == [0.]*4


def test_backend_lease_survives_browser_detach_during_tcp():
    from viewer.server import Service
    service = Service.__new__(Service)
    service.clients, service.viewer_drops = {"page": None}, {"page": 0}
    service.controller, service.generation = "page", 123
    service.lease = mp.Array("q", 2)
    service.bridge = SimpleNamespace(active=True)
    service.detach("page")
    assert service.controller is None and service.lease[0] == 123
    service.bridge.active = False
    service.heartbeat()
    assert list(service.lease) == [0, 0]


def test_high_rate_hold_is_bounded_and_counts_real_sources():
    sampler = Sampler([[(i, 1)] for i in range(4)], 200000, "hold", 10, 100)
    count = 0
    for i in range(101):
        for data, n, _ in sampler.feed(source(i+1, 1_000_000_000+i*10_000_000)):
            assert len(data) == n*8
            count += n
    assert count == 200000 and sampler.sources == 101 and sampler.held == 199900
    large = Sampler([[]]*4096, 200000, "hold", 10, 100)
    large.feed(source(1, 1_000_000_000))
    with pytest.raises(ValueError, match="数据块过大"):
        large.feed(source(2, 1_010_000_000))


def test_socket_invalid_wave_keeps_output_unsupported_command_zeroes(monkeypatch):
    async def check():
        worker, manager, feeder = await fixture_bridge(monkeypatch)
        card = next(iter(manager.cards.values()))
        reader, writer = await asyncio.open_connection("127.0.0.1", card.config["port"])
        try:
            writer.write(p.legacy_command(0xA659, struct.pack("<BHB", 1, 600, 1)))
            await writer.drain()
            await asyncio.sleep(.02)
            value = worker.values[0]
            bad_wave = bytes.fromhex("AA55FF5AA6")+struct.pack("<BHBHH", 1, 65000, 1, 0, 4095)+bytes.fromhex("55AA")
            writer.write(bad_wave+p.legacy_command(0xA3D1, b"\x01"))
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(8), 1) == p.legacy_command(0xFAF5, b"\x02")
            assert worker.values[0] == value
            writer.write(p.legacy_command(0xA4B3, struct.pack("<BBHH", 1, 1, 260, 500)))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await asyncio.sleep(.02)
            assert worker.values == [0.]*4 and "未实现" in card.last_error
        finally:
            writer.close()
            await writer.wait_closed()
            await manager.stop(shutdown=True)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    run(check())


def test_startup_port_conflict_releases_partial_listeners(monkeypatch):
    async def check():
        monkeypatch.setattr("viewer.bridge.runtime.local_addresses", lambda: ["127.0.0.1"])
        worker = make_worker()
        async def request(action, args):
            raise AssertionError("端口冲突前不应转交输出控制")
        manager = Bridge(request, lambda: worker.lock())
        manager.config = config.preset(worker.channels, "signature", "legacy")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            first_port = free_port()
            manager.config["cards"][0]["port"] = first_port
            manager.config["cards"][1]["port"] = busy.getsockname()[1]
            with pytest.raises(OSError):
                await manager.start(worker.status())
            assert not manager.active and not manager.servers and worker.control_mode == "WEB"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", first_port))
    run(check())


def test_slow_sender_timeout_revokes_only_its_card(monkeypatch):
    async def check():
        worker, manager, feeder = await fixture_bridge(monkeypatch, two=True)
        cards = list(manager.cards.values())
        pairs = [await asyncio.open_connection("127.0.0.1", c.config["port"]) for c in cards]
        try:
            await asyncio.sleep(.02)
            slow = next(iter(cards[0].sessions))
            cards[0].config["backlog_ms"] = 100
            async def blocked():
                await asyncio.Event().wait()
            slow.writer.drain = blocked
            pairs[0][1].write(p.legacy_command(0xA659, struct.pack("<BHB", 1, 600, 1)) +
                              p.legacy_command(0xA111, bytes(4)))
            pairs[1][1].write(p.legacy_command(0xA659, struct.pack("<BHB", 1, 900, 1)))
            await asyncio.sleep(.4)
            assert slow.closing and worker.values[0] == 0
            assert worker.values[2] == pytest.approx(900*3.3/4095)
        finally:
            for _, writer in pairs:
                writer.close()
                await writer.wait_closed()
            await manager.stop(shutdown=True)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    run(check())


def test_saved_range_and_divider_survive_new_runtime(tmp_path, monkeypatch):
    from viewer.bridge.runtime import Card, Session
    from viewer.server import Service
    from viewer.settings import Settings

    async def check():
        monkeypatch.setattr("viewer.settings.settings_path", lambda: tmp_path/"settings.json")
        _, manager, feeder = await fixture_bridge(monkeypatch)
        service = Service.__new__(Service)
        service.settings = Settings()
        service.config_lock = asyncio.Lock()
        service.bridge = manager
        service.settings.save(dict(service.settings.data, bridge=manager.config), 0)
        manager.persist_callback = service.persist_bridge
        card = next(iter(manager.cards.values()))
        reader, writer = await asyncio.open_connection("127.0.0.1", card.config["port"])
        try:
            writer.write(p.legacy_command(0xA3E2, bytes([1, 2]))+
                         p.legacy_command(0xA3D0, struct.pack("<HB", 32500, 2))+
                         p.legacy_command(0xA452, bytes.fromhex("13A4F1C5")))
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(46), 2)
            assert reply[37] == 1 and struct.unpack_from("<H", reply, 40)[0] == 32500
            loaded = Settings()
            assert not loaded.error and loaded.data["revision"] == 3
            replacement = Bridge(manager.request, manager.critical)
            replacement.replace_config(loaded.data["bridge"])
            peer = SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 1234))
            resumed = Session(replacement, Card(replacement.config["cards"][0]), None, peer)
            assert (resumed.range, resumed.divider) == (1, 32500)
            assert not (tmp_path/"bridge.json").exists()
        finally:
            writer.close()
            await writer.wait_closed()
            await manager.stop(shutdown=True)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    run(check())


def test_invalid_idle_clock_is_rejected():
    worker = make_worker()
    assert apply(worker, "bridge_setup", cards=[dict(id="a", outputs=[0, 1])])["ok"]
    assert apply(worker, "bridge_authorize", card="a")["ok"]
    assert apply(worker, "bridge_claim", card="a", token="a")["ok"]
    for rate in (0, -1, True, float("nan")):
        assert not apply(worker, "bridge_rate", card="a", token="a", rate=rate)["ok"]
    assert worker.values == [0]*4


def test_emergency_preempts_stale_queued_command(monkeypatch):
    worker = make_worker()
    worker.owner = 1
    worker.values[0] = .5
    worker.emergency = mp.Event()
    worker.emergency.set()
    worker.lease[:] = [2, time.perf_counter_ns()]
    worker.commands.put(dict(id="stale", action="arm", args={}, owner=1))
    monkeypatch.setattr("viewer.worker.time.sleep", lambda _delay: worker.stop.set())
    worker.run()
    assert worker.values == [0.]*4 and worker.bus.output == [0.]*4
    assert not worker.events.get_nowait()["ok"]


def test_capture_does_not_backfill_samples_before_start():
    from viewer.bridge.runtime import Card, Session
    channels = catalog()
    configuration = config.preset(channels, "signature", "extended")["cards"][0]
    bridge = SimpleNamespace(channels=channels, source_status={"period_ms": 10},
                             index=lambda kind, ident: next(i for i,c in enumerate(channels[kind]) if c["id"]==ident))
    writer = SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 1234))
    session = Session(bridge, Card(configuration), None, writer)
    session.start_stream([0], 2000)
    start = session.stream_started_ns
    session.feed([source(1, start-1000), source(2, start+1)])
    assert session.sampler.sources == 1 and session.sampler.previous[0]["seq"] == 2


def test_bridge_does_not_report_confirmed_zero_after_fault_or_stale_source():
    from viewer.bridge.runtime import Card
    async def request(_action, _args):
        return None
    manager = Bridge(request, lambda: None)
    manager.channels = catalog()
    card_config = config.preset(manager.channels, "signature", "legacy")["cards"][0]
    manager.cards[card_config["id"]] = Card(card_config)
    manager.source_status = dict(connection="OP", wkc=3, expected_wkc=3, ao=[.5, 0, 0, 0])
    manager.last_source_ns = time.perf_counter_ns()
    assert manager.status()["cards"][0]["physical_ao"][0] == pytest.approx(.5, abs=.0004)
    assert manager.status()["source"]["wkc"] == 3 and manager.status()["source"]["live"]
    manager.source_status["connection"] = "FAULT"
    assert manager.status()["source"]["wkc"] is None and not manager.status()["source"]["live"]
    assert manager.status()["cards"][0]["physical_ao"] == [None, None]
    manager.source_status["connection"] = "OP"
    manager.last_source_ns = time.perf_counter_ns()-2_000_000_000
    assert manager.status()["cards"][0]["physical_ao"] == [None, None]


def test_output_only_fault_keeps_empty_input_vectors_and_csv_width():
    import csv
    import io
    from viewer.server import csv_rows
    worker = make_worker()
    worker.channels["ai"] = []
    def fail(_values):
        raise RuntimeError("test link failure")
    worker.bus.exchange = fail
    worker.bus.close = lambda: False
    sample = worker.cycle(time.perf_counter_ns())
    assert sample["raw"] == sample["ai"] == sample["valid"] == sample["flags"] == []
    assert len(next(csv.reader(io.StringIO(csv_rows([sample]))))) == 19
