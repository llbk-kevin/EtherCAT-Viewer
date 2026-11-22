"""虚拟设备配置；持久化文件仅包含配置，不恢复运行或输出状态。"""
import ipaddress
import json
import os
from pathlib import Path
import uuid

from ..paths import powershell_path


def config_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "EtherCAT-Viewer" / "bridge.json"


def local_addresses():
    import subprocess
    result = subprocess.run(
        [powershell_path(), "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         "Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress | ConvertTo-Json -Compress"],
        capture_output=True, check=True, encoding="utf-8-sig", timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW)
    values = json.loads(result.stdout or "[]")
    values = values if isinstance(values, list) else [values]
    return sorted(set(["127.0.0.1", *[v for v in values if not v.startswith("169.254.")]]))


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须为 {low}–{high} 的整数")
    return value


def validate_config(data, channels, fingerprint, addresses):
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("fingerprint") != fingerprint or not fingerprint:
        raise ValueError("设备拓扑已变化或未核对，请重新生成预设并核对映射")
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise ValueError("虚拟卡列表无效")
    known = {kind: {c["id"] for c in rows} for kind, rows in channels.items()}
    result, used, ports, ids = [], set(), set(), set()
    for source in cards:
        if not isinstance(source, dict):
            raise ValueError("虚拟卡配置必须是对象")
        card = dict(source)
        ident = card.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 64 or ident in ids:
            raise ValueError("虚拟卡 ID 无效或重复")
        ids.add(ident)
        if card.get("profile") not in ("legacy", "extended"):
            raise ValueError("协议类型无效")
        if type(card.get("enabled", True)) is not bool:
            raise ValueError("启用状态无效")
        card["enabled"] = card.get("enabled", True)
        card["name"] = str(card.get("name", ident))[:100]
        host = str(ipaddress.IPv4Address(card["host"]))
        if host == "0.0.0.0" or host not in addresses:
            raise ValueError("监听地址必须是当前本机 IPv4 地址")
        card["host"] = host
        port = integer(card.get("port"), 1024, 65535, "端口")
        if port == 8765 or (card["enabled"] and port in ports):
            raise ValueError("端口重复或占用网页服务端口")
        if card["enabled"]:
            ports.add(port)
        allow = card.get("allow", ["127.0.0.1/32"])
        if not isinstance(allow, list) or not allow:
            raise ValueError("请配置允许访问的客户端 IPv4 或网段")
        if any(not isinstance(v, str) for v in allow):
            raise ValueError("允许访问列表必须为 IPv4 文本")
        card["allow"] = [str(ipaddress.IPv4Network(v, strict=False)) for v in allow]
        for kind in ("ai", "ao"):
            mapping = card.get(kind)
            if not isinstance(mapping, list) or len(mapping) > 65535 or (kind == "ai" and not mapping):
                raise ValueError("通道映射列表无效")
            if card["profile"] == "legacy" and len(mapping) != (8 if kind == "ai" else 2):
                raise ValueError("原版虚拟卡必须为 8 AI＋2 AO")
            for value in mapping:
                if value is not None and (not isinstance(value, str) or value not in known[kind]):
                    raise ValueError(f"映射的 {kind.upper()} 通道不存在：{value}")
                if kind == "ao" and value is not None and card["enabled"]:
                    if value in used:
                        raise ValueError("正在发布的虚拟卡不能重复占用真实 AO")
                    used.add(value)
        if card.get("ao_range") not in ("original", "bipolar"):
            raise ValueError("AO 量程策略无效")
        if card.get("sampling") not in ("hold", "native"):
            raise ValueError("采样策略无效")
        integer(card.get("send_ms"), 5, 1000, "发送批量")
        integer(card.get("backlog_ms"), 100, 10000, "积压上限")
        integer(card.get("saved_range", 2), 1, 2, "采集量程")
        integer(card.get("saved_divider", 65000), 650, 65535, "采样分频")
        result.append(card)
    return dict(version=1, fingerprint=fingerprint, cards=result)


def preset(channels, fingerprint, profile):
    ai = [c["id"] for c in channels["ai"]]
    ao = [c["id"] for c in channels["ao"]]
    cards = []
    count = max(1, (len(ai)+7)//8, (len(ao)+1)//2) if profile == "legacy" else 1
    for i in range(count):
        inputs = ai[i*8:i*8+8] if len(ai) > 8 else ai[:]
        outputs = ao[i*2:i*2+2]
        if profile == "legacy":
            inputs = (inputs + [None]*8)[:8]
            outputs = (outputs + [None]*2)[:2]
        else:
            inputs, outputs = ai or [None], ao
        cards.append(dict(id=uuid.uuid4().hex[:12], name=f"虚拟卡 {i+1}", enabled=True,
                          profile=profile, host="127.0.0.1", port=(6000 if profile == "legacy" else 7000)+i,
                          allow=["127.0.0.1/32"], ai=inputs, ao=outputs, ao_range="original",
                          sampling="hold", send_ms=50, backlog_ms=2000, saved_range=2, saved_divider=65000))
    return dict(version=1, fingerprint=fingerprint, cards=cards)


def save_config(data):
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_config():
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("不支持的配置版本")
        return data, ""
    except FileNotFoundError:
        return dict(version=1, fingerprint="", cards=[]), ""
    except (OSError, ValueError) as exc:
        return dict(version=1, fingerprint="", cards=[]), f"配置读取失败：{exc}"
