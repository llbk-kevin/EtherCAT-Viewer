import json
import subprocess
from types import SimpleNamespace

import pytest

from viewer import hardware

GUID = "A17A58C3-BB4A-44ED-9C0E-745E859A85BE"
OTHER_GUID = "49A6C1BC-3F92-4F71-BA50-46B1F981553A"
INTERFACE = fr"\Device\NPF_{{{GUID}}}"


def adapter(status="Up", interface=INTERFACE):
    return {"guid": GUID, "name": "测试有线网卡", "status": status, "interface": interface}


def test_adapter_listing_matches_npcap_and_windows_guid(monkeypatch):
    rows = [{"InterfaceGuid": f"{{{GUID.lower()}}}", "Name": "有线一",
             "Status": "Up", "LinkSpeed": "100 Mbps", "InterfaceDescription": "USB Ethernet"},
            {"InterfaceGuid": OTHER_GUID, "Name": "有线二", "Status": "Disconnected",
             "LinkSpeed": "0 bps", "InterfaceDescription": "Ethernet"}]

    def run(argv, **_kwargs):
        assert "Get-NetAdapter -Physical | Where-Object InterfaceType -eq 6" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows))

    monkeypatch.setattr(hardware.subprocess, "run", run)
    monkeypatch.setattr(hardware, "load_driver", lambda: SimpleNamespace(
        find_adapters=lambda: [SimpleNamespace(name=INTERFACE.lower().encode())]))
    result = hardware.adapters()
    assert result["error"] is None
    first, second = result["items"]
    assert first["guid"] == GUID and first["interface"] == INTERFACE.lower()
    assert second["guid"] == OTHER_GUID and second["interface"] is None


@pytest.mark.parametrize(("interface", "rows", "message"), [
    ("Wi-Fi", [], "有线网卡"),
    (None, [], "有线网卡"),
    (INTERFACE, [], "物理有线网卡"),
    (INTERFACE, [adapter(status="Disconnected")], "链路未连接"),
    (INTERFACE, [adapter(interface=None)], "Npcap 无法访问"),
])
def test_connection_revalidates_selected_adapter(monkeypatch, interface, rows, message):
    monkeypatch.setattr(hardware, "adapters", lambda: {"items": rows, "error": None})
    with pytest.raises(ValueError, match=message):
        hardware.check_adapter(interface)


def test_connection_uses_current_npcap_identifier(monkeypatch):
    row = adapter(interface=INTERFACE.lower())
    monkeypatch.setattr(hardware, "adapters", lambda: {"items": [row], "error": None})
    assert hardware.check_adapter(INTERFACE) == row


def test_bus_lock_excludes_only_same_guid():
    first = hardware.BusLock(GUID)
    try:
        with pytest.raises(RuntimeError, match="另一个实机进程"):
            hardware.BusLock(GUID)
        second = hardware.BusLock(OTHER_GUID)
        second.close()
    finally:
        first.close()
    reopened = hardware.BusLock(GUID)
    reopened.close()
