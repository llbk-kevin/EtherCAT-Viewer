"""源码和单文件共用的版本化配置，修订号防止页面互相覆盖。"""
from copy import deepcopy
import json
import os
from pathlib import Path
from .calibration import validate_profile, drive_voltage
from .generator import validated_period
from .bridge.config import empty_config, validate_config


def settings_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()/"AppData/Local")/"EtherCAT-Viewer"/"settings.json"


def module_settings(configuration, slave_position, model):
    return configuration.get("modules", {}).get(f"s{slave_position}:{model}", {})


def defaults():
    return dict(version=3, revision=0, modules={},
                hardware=dict(interface="", period_ms=10, sync="current", receive_timeout_us=2000),
                faults=dict(consecutive=10, duration_ms=500, auto_reconnect=True, web_wait_ms=5000,
                            policies={"bus": {}, "web": {}, "tcp": {}}),
                startup=dict(auto_connect=False, open_browser=True, minimized=False, login=False),
                topology=dict(channels={"ai": [], "ao": []}, fingerprint="", devices=[]),
                channels={}, bridge=empty_config())


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须为 {low}–{high} 的整数")
    return value


def validate(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 3:
        raise ValueError("配置版本必须为 3")
    out = defaults()
    for section in ("hardware", "faults", "startup", "topology"):
        if not isinstance(data.get(section, {}), dict):
            raise ValueError(f"{section} 配置必须为对象")
        out[section].update(deepcopy(data.get(section, {})))
    out["modules"] = deepcopy(data.get("modules", {}))
    if not isinstance(out["modules"], dict):
        raise ValueError("模块配置必须为对象")
    h, f = out["hardware"], out["faults"]
    if "filter" in h or "watchdog_ms" in h:
        raise ValueError("滤波和看门狗仅支持逐模块配置")
    if "unattended" in out["startup"]:
        raise ValueError("启动配置仅支持 auto_connect，请删除 unattended")
    h["interface"] = str(h["interface"])
    validated_period(h["period_ms"])
    if h["sync"] not in ("current", "freerun", "sm", "dc"):
        raise ValueError("同步模式无效")
    integer(h["receive_timeout_us"], 100, 20000, "收包超时 / μs")
    integer(f["consecutive"], 1, 10000, "连续异常次数")
    integer(f["duration_ms"], 1, 10000, "容错时间 / ms")
    integer(f["web_wait_ms"], 0, 3600000, "网页失联等待时间 / ms")
    for key in ("auto_reconnect",):
        if type(f[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    for key in ("auto_connect", "open_browser", "minimized", "login"):
        if type(out["startup"][key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if not isinstance(data.get("channels", {}), dict):
        raise ValueError("通道校准配置无效")
    out["channels"] = {str(k): validate_profile(v) for k, v in data.get("channels", {}).items()}
    topology = out["topology"]
    channels = topology.get("channels")
    if (not isinstance(topology.get("fingerprint"),str) or not isinstance(topology.get("devices"),list)
            or any(not isinstance(d,dict) for d in topology["devices"]) or not isinstance(channels,dict)):
        raise ValueError("保存的设备拓扑结构无效")
    for kind in ("ai","ao"):
        rows = channels.get(kind)
        if not isinstance(rows,list) or any(not isinstance(c,dict) or not isinstance(c.get("id"),str) or not c["id"] for c in rows):
            raise ValueError("保存的真实通道列表无效")
        if len({c["id"] for c in rows}) != len(rows):
            raise ValueError("保存的真实通道 ID 重复")
        for channel in rows:
            channel_id = channel["id"]
            out["channels"].setdefault(channel_id, validate_profile({}))
            module_id = channel_id.rsplit(":", 1)[0]
            if kind == "ai" and module_id.endswith(":EL3104"):
                out["modules"].setdefault(module_id, dict(filter="current"))
            elif kind == "ao" and module_id.endswith(":EL4134"):
                out["modules"].setdefault(module_id, dict(watchdog_ms=600))
    policies = f.get("policies", {})
    if not isinstance(policies,dict) or any(not isinstance(policies.get(k,{}),dict) for k in ("bus","web","tcp")):
        raise ValueError("故障策略必须按类型与通道保存为对象")
    f["policies"] = {}
    for kind in ("bus", "web", "tcp"):
        f["policies"][kind] = {}
        for ident, rule in policies.get(kind, {}).items():
            if not isinstance(ident,str) or not isinstance(rule, dict) or rule.get("action") not in (("zero", "hold", "value", "continue") if kind == "web" else ("zero", "hold", "value")):
                raise ValueError(f"{kind} 的故障动作无效")
            value = rule.get("voltage", 0)
            drive_voltage(value, out["channels"].get(ident, {}))
            f["policies"][kind][ident] = dict(action=rule["action"], voltage=value)
    for ident, profile in out["channels"].items():
        if ":AO" in ident:
            drive_voltage(0, profile)
    for ident, module in out["modules"].items():
        if not isinstance(module, dict) or not isinstance(ident, str):
            raise ValueError("模块参数结构无效")
        if ident.endswith(":EL3104"):
            selection = module.get("filter")
            if selection not in ("current", "off", "fir50", "fir60", *[f"iir{i}" for i in range(1,9)]):
                raise ValueError(f"{ident} 输入滤波无效")
            if h["sync"] in ("sm", "dc") and selection not in ("current", "off"):
                raise ValueError(f"{ident} 帧同步／DC 必须关闭滤波")
            if h["sync"] == "freerun" and selection == "off":
                raise ValueError(f"{ident} 自由运行需要滤波")
        elif ident.endswith(":EL4134"):
            value = integer(module.get("watchdog_ms"), 10, 20000, ident+" 看门狗 / ms")
            if value < f["duration_ms"]+max(50,h["period_ms"]*2):
                raise ValueError(f"{ident} 看门狗不足以覆盖容错窗口")
        else:
            raise ValueError("不支持该模块配置："+ident)
    if out["startup"]["auto_connect"] and (not h["interface"] or not out["topology"].get("fingerprint")):
        raise ValueError("启用无人值守前请先选择网卡并成功连接核对拓扑")
    out["bridge"] = deepcopy(data.get("bridge", out["bridge"]))
    bridge = out["bridge"]
    if not isinstance(bridge,dict) or bridge.get("version") != 1 or not isinstance(bridge.get("cards"),list):
        raise ValueError("桥接配置无效")
    if bridge["cards"]:
        if any(not isinstance(c,dict) or any(not isinstance(c.get(k),list) for k in ("ai","ao")) for c in bridge["cards"]):
            raise ValueError("虚拟卡或通道映射结构无效")
        declared = {k:[dict(id=ident) for ident in {v for c in bridge["cards"] for v in c[k] if isinstance(v,str)}] for k in ("ai","ao")}
        # 文件读取仅检查结构与协议边界，实际绑定地址与设备映射在发布前再次核对。
        out["bridge"] = validate_config(bridge,declared,bridge.get("fingerprint"),[c.get("host") for c in bridge["cards"]])
    out["revision"] = integer(data.get("revision", 0), 0, 2**53-1, "配置修订")
    return out


class Settings:
    def __init__(self):
        self.path = settings_path()
        self.error = ""
        try:
            self.data = validate(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            self.data = defaults()
        except (ValueError, OSError) as exc:
            self.data = defaults()
            self.error = f"配置读取失败，已禁止无人值守启动：{exc}；可导入备份恢复"

    def save(self, data, revision):
        if revision != self.data["revision"]:
            raise ValueError("配置已被其他页面修改，请重新读取后保存")
        checked = validate(data)
        if checked == self.data:
            return deepcopy(self.data)
        checked["revision"] = revision+1
        encoded = json.dumps(checked, ensure_ascii=False, indent=2, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if self.path.exists():
            self.path.with_suffix(".backup.json").write_bytes(self.path.read_bytes())
        temporary.replace(self.path)
        self.data, self.error = checked, ""
        return deepcopy(checked)
