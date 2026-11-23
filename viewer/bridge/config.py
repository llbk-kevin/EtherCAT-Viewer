"""虚拟设备配置校验和模板。"""
import ipaddress
import json
import uuid

from ..paths import powershell_path


def empty_config():
    return dict(version=1, fingerprint="", cards=[])


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
        host = str(ipaddress.IPv4Address(card.get("host")))
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
        if any(not isinstance(card.get(kind),list) for kind in ("ai","ao")):
            raise ValueError("通道映射列表无效")
        permissions = card.get("ao_writable", [True]*len(card["ao"]))
        if not isinstance(permissions, list) or len(permissions) != len(card.get("ao", [])) or any(type(v) is not bool for v in permissions):
            raise ValueError("AO 读写权限数量或类型无效")
        card["ao_writable"] = permissions
        if type(card.get("auto_reauthorize", True)) is not bool:
            raise ValueError("自动重新接管必须为布尔值")
        card["auto_reauthorize"] = card.get("auto_reauthorize", True)
        for field in ("start_on_boot", "authorize_on_boot"):
            if type(card.get(field, False)) is not bool:
                raise ValueError("虚拟卡启动选项必须为布尔值")
            card[field] = card.get(field, False)
        for kind in ("ai", "ao"):
            mapping = card.get(kind)
            if not isinstance(mapping, list) or len(mapping) > 65535 or (kind == "ai" and not mapping):
                raise ValueError("通道映射列表无效")
            if card["profile"] == "legacy" and len(mapping) != (8 if kind == "ai" else 2):
                raise ValueError("原版虚拟卡必须为 8 AI＋2 AO")
            for map_index, value in enumerate(mapping):
                if value is not None and (not isinstance(value, str) or value not in known[kind]):
                    raise ValueError(f"映射的 {kind.upper()} 通道不存在：{value}")
                if kind == "ao" and value is not None and card["enabled"] and card["ao_writable"][map_index]:
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
        # 最坏情况下每个采样段必须能装入扩展协议 1 MiB，含 JSON 元数据。
        if card["profile"] == "extended":
            n = len(card["ai"])
            estimate = len(json.dumps(dict(channels=list(range(n)), valid=[True]*n, mapped=[True]*n, clipped=[False]*n)).encode())+2*n+8192
            if estimate > 1048576:
                raise ValueError("通道元数据超过扩展 v1 的 1 MiB 单帧上限，请拆分虚拟卡")
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
                          profile=profile, ao_writable=[True]*len(outputs), auto_reauthorize=True, start_on_boot=False, authorize_on_boot=False, host="127.0.0.1", port=(6000 if profile == "legacy" else 7000)+i,
                          allow=["127.0.0.1/32"], ai=inputs, ao=outputs, ao_range="original",
                          sampling="hold", send_ms=50, backlog_ms=2000, saved_range=2, saved_divider=65000))
    return dict(version=1, fingerprint=fingerprint, cards=cards)
