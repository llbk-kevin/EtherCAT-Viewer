"""电气校准与工程量映射；物理零值使用同一受校验的电气换算。"""
import math
from .pdo import validated_voltage


def finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label}必须为有限数值")
    return float(value)


def validate_profile(data):
    if not isinstance(data, dict):
        raise ValueError("通道配置必须是对象")
    if set(data)-{"name","calibration","engineering","measurements"}:
        raise ValueError("通道配置含不支持的字段")
    result = {}
    result["name"] = str(data.get("name", ""))[:100]
    calibration = data.get("calibration", {})
    if not isinstance(calibration, dict):
        raise ValueError("电气校准参数必须是对象")
    gain = finite(calibration.get("gain", 1), "校准增益")
    offset = finite(calibration.get("offset", 0), "校准偏移")
    if not 0.000001 <= gain <= 1000000:
        raise ValueError("校准增益必须为 0.000001–1000000 的正数")
    result["calibration"] = dict(enabled=bool(calibration.get("enabled", False)), gain=gain, offset=offset)
    engineering = data.get("engineering", {})
    if not isinstance(engineering, dict):
        raise ValueError("物理量映射参数必须是对象")
    values = {k: finite(engineering.get(k, v), k) for k, v in
              (("voltage1", 0), ("value1", 0), ("voltage2", 10), ("value2", 100))}
    if values["voltage1"] == values["voltage2"] or values["value1"] == values["value2"]:
        raise ValueError("两点映射的电压和物理量都不能相同")
    result["engineering"] = dict(enabled=bool(engineering.get("enabled", False)),
                                  unit=str(engineering.get("unit", ""))[:30], **values)
    result["measurements"] = data.get("measurements", [])
    if not isinstance(result["measurements"], list) or len(result["measurements"]) > 100:
        raise ValueError("每通道最多保存 100 条两点校准记录")
    for measurement in result["measurements"]:
        if not isinstance(measurement,dict) or measurement.get("type") != "two_point":
            raise ValueError("校准记录只接受当前两点格式")
        points = [finite(measurement.get(key),"校准记录 "+key) for key in ("x1","y1","x2","y2")]
        if points[0] == points[2]:
            raise ValueError("校准记录的两个输入点不能相同")
    return result


def calibrated(voltage, profile):
    c = profile.get("calibration", {})
    return voltage * c.get("gain", 1) + c.get("offset", 0) if c.get("enabled") else voltage


def drive_voltage(voltage, profile):
    voltage = validated_voltage(voltage)
    c = profile.get("calibration", {})
    command = (voltage - c.get("offset", 0)) / c.get("gain", 1) if c.get("enabled") else voltage
    try:
        return validated_voltage(command)
    except ValueError as exc:
        raise ValueError(f"目标 {voltage:g} V 经校准后超出 DAC ±10 V，原输出保持") from exc


def engineering_value(voltage, profile):
    e = profile.get("engineering", {})
    if voltage is None or not e.get("enabled"):
        return None
    return e["value1"] + (voltage-e["voltage1"])*(e["value2"]-e["value1"])/(e["voltage2"]-e["voltage1"])


def engineering_voltage(value, profile):
    e = profile.get("engineering", {})
    if not e.get("enabled"):
        raise ValueError("该通道未启用物理量映射")
    return validated_voltage(e["voltage1"] + (finite(value, "物理量")-e["value1"])*
                             (e["voltage2"]-e["voltage1"])/(e["value2"]-e["value1"]))


def two_point(x1, y1, x2, y2):
    x1, y1, x2, y2 = [finite(v, "校准点") for v in (x1, y1, x2, y2)]
    if x1 == x2:
        raise ValueError("两次指令／原始电压不能相同")
    gain = (y2-y1)/(x2-x1)
    return validate_profile(dict(calibration=dict(enabled=True, gain=gain, offset=y1-gain*x1)))["calibration"]
