"""按字段比较、关联规划与应用类别；不接触硬件或磁盘。"""
from copy import deepcopy
from .settings import validate

MISSING = None


class SettingError(ValueError):
    def __init__(self, message, paths=()):
        super().__init__(message)
        self.paths = list(paths)


def get_value(data, path):
    value = data
    for key in path:
        if isinstance(value, list):
            value = next((row for row in value if row.get("id") == key), None)
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return deepcopy(value)


def put_value(data, path, value):
    target = data
    for key in path[:-1]:
        if isinstance(target, list):
            target = next(row for row in target if row["id"] == key)
        else:
            target = target.setdefault(key, {})
    target[path[-1]] = deepcopy(value)


def allowed(data, path):
    if not isinstance(path, list) or not all(isinstance(p, str) and p for p in path):
        return False
    ai = {c["id"] for c in data["topology"]["channels"]["ai"]}
    ao = {c["id"] for c in data["topology"]["channels"]["ao"]}
    if len(path) == 2:
        return ((path[0] == "hardware" and path[1] in ("period_ms", "sync", "receive_timeout_us")) or
                (path[0] == "startup" and path[1] in ("auto_connect", "open_browser", "minimized", "login")) or
                (path[0] == "faults" and path[1] in ("consecutive", "duration_ms", "auto_reconnect", "web_wait_ms")))
    if len(path) == 3:
        return ((path[0] == "modules" and path[1] in data["modules"] and path[2] in ("filter","watchdog_ms") and path[2] in data["modules"][path[1]]) or
                (path[0] == "channels" and path[1] in ai | ao and path[2] in ("name", "calibration", "engineering", "measurements")))
    if len(path) == 4 and path[0]=="channels" and path[1] in ai|ao:
        return ((path[2]=="calibration" and path[3] in ("enabled","gain","offset")) or
                (path[2]=="engineering" and path[3] in ("enabled","unit","voltage1","value1","voltage2","value2")))
    if len(path) == 4:
        return ((path[:2] == ["faults", "policies"] and path[2] in ("bus", "web", "tcp") and path[3] in ao) or
                (path[:2] == ["bridge", "cards"] and path[2] in {c["id"] for c in data["bridge"]["cards"]}
                 and path[3] in ("start_on_boot", "authorize_on_boot")))
    return False


def reconnect_required(paths, status):
    dc = any(d.get("sync_mode") in (2, 3) for d in status.get("hardware_details", []))
    for path in paths:
        if path[0] == "modules" or path[:3] == ["faults", "policies", "bus"]:
            return True
        if path[0]=="channels" and ":AO" in path[1] and path[2]=="calibration":
            return True
        if path == ["hardware", "sync"] or (path == ["hardware", "period_ms"] and dc):
            return True
    return False


def make_plan(current, requested, status=None):
    status = status or {}
    if not isinstance(requested, list) or not 1 <= len(requested) <= 256:
        raise SettingError("应用请求必须包含 1–256 个明确字段")
    candidate = deepcopy(current)
    selected, reasons = {}, {}
    for change in requested:
        path = change.get("path") if isinstance(change, dict) else None
        if not allowed(current, path):
            raise SettingError("不允许修改该配置字段", [path] if path else [])
        key = tuple(path)
        if any(key[:len(p)]==p or p[:len(key)]==key for p in selected) or "value" not in change or "base" not in change:
            raise SettingError("配置字段重复或缺少原始值", [path])
        actual = get_value(current, path)
        if actual != change["base"] and actual != change["value"]:
            raise SettingError("该字段已被其他页面或客户端更新，请核对后台现值后重新应用", [path])
        selected[key] = deepcopy(change)
        put_value(candidate, path, change["value"])

    def depend(path, value, reason):
        if get_value(candidate, path) == value:
            return
        if tuple(path) in selected:
            raise SettingError(reason + "；本次选择的参数相互冲突", [path])
        put_value(candidate, path, value)
        selected[tuple(path)] = dict(path=path, base=get_value(current, path), value=value)
        reasons[tuple(path)] = reason

    ai_modules = [k for k in candidate["modules"] if k.endswith(":EL3104")]
    explicit_sync = ("hardware", "sync") in selected
    filter_changes = [c for p, c in selected.items() if p[0] == "modules" and p[-1] == "filter"]
    if filter_changes and not explicit_sync:
        wants_filter = any(c["value"] not in ("off", "current") for c in filter_changes)
        wants_off = any(c["value"] == "off" for c in filter_changes)
        if wants_filter and candidate["hardware"]["sync"] in ("sm", "dc"):
            depend(["hardware", "sync"], "freerun", "启用输入滤波需要自由运行模式")
        elif wants_off and candidate["hardware"]["sync"] == "freerun":
            depend(["hardware", "sync"], "sm", "关闭输入滤波需要切到帧同步")
    mode = candidate["hardware"]["sync"]
    for ident in (ai_modules if explicit_sync or filter_changes else []):
        value = candidate["modules"][ident]["filter"]
        if mode in ("sm", "dc") and value != "off":
            depend(["modules", ident, "filter"], "off", "帧同步／DC 必须关闭该模块输入滤波")
        elif mode == "freerun" and value in ("off", "current"):
            depend(["modules", ident, "filter"], "fir50", "自由运行需要启用输入滤波，默认使用 50 Hz FIR")
    if explicit_sync or filter_changes:
        requested_mode = candidate["hardware"]["sync"]
        mask = {"freerun":1,"sm":2,"dc":12}.get(requested_mode)
        for module in status.get("hardware_details",[]):
            if mask and module.get("supported_modes") is not None and not module["supported_modes"] & mask:
                raise SettingError(module.get("name","模块")+" 不支持所选同步模式", [["hardware","sync"]])
    minimum = candidate["faults"]["duration_ms"] + max(50, candidate["hardware"]["period_ms"]*2)
    for ident, module in candidate["modules"].items():
        if "watchdog_ms" in module and module["watchdog_ms"] < minimum:
            if ("modules", ident, "watchdog_ms") in selected:
                raise SettingError(f"模块看门狗至少为 {minimum} ms，请先调整容错时间", [["modules", ident, "watchdog_ms"]])
            depend(["modules", ident, "watchdog_ms"], minimum, "看门狗必须覆盖容错窗口与通信周期余量")

    try:
        checked = validate(candidate)
    except ValueError as exc:
        raise SettingError(str(exc), [list(p) for p in selected]) from exc
    changes = [dict(path=list(p), base=get_value(current, list(p)), value=get_value(checked, list(p)),
                    related=p in reasons, reason=reasons.get(p, "")) for p in selected]
    paths = [c["path"] for c in changes]
    online = status.get("connection") == "OP"
    observed = status.get("parameter_readback", {}).get("values", {})
    drift = any("/".join(c["path"]) in observed and c["value"] != "current" and observed["/".join(c["path"])] != c["value"] for c in changes)
    reconnect = reconnect_required(paths, status)
    return dict(config=checked, changes=changes,
                mode="reconnect" if online and reconnect else "live" if online else "offline",
                noop=checked == current and not drift,
                impacts=dict(outputs=[i+1 for i, owner in enumerate(status.get("output_owners", [])) if owner != "locked"],
                             cards=[c["name"] for c in status.get("bridge", {}).get("cards", [])]))


def merge_cards(current, baseline, incoming):
    """按稳定 ID、按卡字段三方合并，不覆盖其他页面更新的启动开关。"""
    if any(not isinstance(data,dict) or not isinstance(data.get("cards"),list) for data in (current,baseline,incoming)):
        raise ValueError("虚拟卡配置必须包含卡列表")
    if not all(isinstance(c, dict) for data in (current, baseline, incoming) for c in data.get("cards", [])):
        raise ValueError("虚拟卡列表无效")
    if any(len({c.get("id") for c in data["cards"]})!=len(data["cards"]) for data in (current,baseline,incoming)):
        raise ValueError("虚拟卡 ID 重复")
    result = deepcopy(current)
    live = {c["id"]: c for c in result["cards"]}
    base = {c["id"]: c for c in baseline["cards"]}
    desired = {c["id"]: c for c in incoming["cards"]}
    for ident, before in base.items():
        if ident not in desired:
            if live.get(ident) != before:
                raise ValueError("待删除虚拟卡已被其他页面修改，请重新读取")
            result["cards"] = [c for c in result["cards"] if c["id"] != ident]
    for ident, draft in desired.items():
        if ident not in base:
            if ident in live:
                raise ValueError("新增虚拟卡 ID 已存在")
            result["cards"].append(deepcopy(draft))
            continue
        if ident not in live:
            raise ValueError("虚拟卡已被其他页面删除")
        for key, value in draft.items():
            before, actual = base[ident].get(key), live[ident].get(key)
            if value != before:
                if actual != before and actual != value:
                    raise ValueError(f"虚拟卡 {draft.get('name', ident)} 的 {key} 已被其他页面修改")
                live[ident][key] = deepcopy(value)
    if incoming.get("fingerprint") != baseline.get("fingerprint"):
        if current.get("fingerprint") != baseline.get("fingerprint"):
            raise ValueError("拓扑核对信息已变化")
        result["fingerprint"] = incoming["fingerprint"]
    return result
