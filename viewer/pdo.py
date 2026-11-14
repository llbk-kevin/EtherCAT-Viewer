from dataclasses import asdict, dataclass
import math

AI = (0x6000, 0x6010, 0x6020, 0x6030)
AO = (0x7000, 0x7010, 0x7020, 0x7030)
FLAGS = {1: "underrange", 2: "overrange", 3: "limit1", 5: "limit2",
         7: "error", 14: "sync_error", 15: "invalid", 16: "toggle"}

@dataclass(frozen=True)
class Entry:
    index: int
    sub: int
    width: int
    offset: int

def read_int(slave, index, sub, size, signed=False):
    data = slave.sdo_read(index, sub, size=size)
    if len(data) != size:
        raise ValueError(f"CoE {index:04X}:{sub:02X} 长度 {len(data)}，期望 {size}")
    return int.from_bytes(data, "little", signed=signed)

def read_mapping(slave, assignment):
    offset, entries = 0, []
    count = read_int(slave, assignment, 0, 1)
    if not 1 <= count <= 16:
        raise ValueError(f"不支持的 PDO 分配 0x{assignment:04X}：{count} 项")
    for sub in range(1, count + 1):
        pdo = read_int(slave, assignment, sub, 2)
        if not 0x1600 <= pdo <= 0x1BFF:
            raise ValueError(f"不支持的 PDO 索引 0x{pdo:04X}")
        count = read_int(slave, pdo, 0, 1)
        if not 1 <= count <= 64:
            raise ValueError(f"PDO 0x{pdo:04X} 条目数异常：{count}")
        for pos in range(1, count + 1):
            word = read_int(slave, pdo, pos, 4)
            width = word & 255
            if width == 0:
                raise ValueError(f"PDO 0x{pdo:04X} 出现零位宽条目")
            entries.append(Entry(word >> 16, (word >> 8) & 255, width, offset))
            offset += width
    return entries

def volts(raw):
    return raw * 10 / (32768 if raw < 0 else 32767)

def raw_voltage(value):
    return round(value * (32768 if value < 0 else 32767) / 10)

def validated_voltage(value):
    if type(value) not in (int, float) or not -10 <= value <= 10 or not math.isfinite(value):
        raise ValueError("电压必须为 −10 至 +10 V 的有限数值")
    return float(value)

class Mapping:
    def __init__(self, entries, size, output=False):
        self.entries, self.size = entries, size
        indices = AO if output else AI
        self.values, self.flags = [], []
        value_sub = 1 if output else 0x11
        seen = set()
        offset = 0
        for entry in entries:
            if entry.offset != offset or entry.width < 1 or entry.offset + entry.width > size * 8:
                raise ValueError("PDO 位偏移或长度异常")
            offset += entry.width
            if entry.index == 0 and entry.sub == 0:
                continue
            if entry.index not in indices or (entry.index, entry.sub) in seen:
                raise ValueError(f"未知或重复 PDO 条目 {entry.index:04X}:{entry.sub:02X}")
            seen.add((entry.index, entry.sub))
            if entry.sub == value_sub and entry.width == 16:
                continue
            flag_width = 2 if entry.sub in (3, 5) else 1
            if not output and ((entry.sub in FLAGS and entry.width == flag_width)
                               or (entry.sub == 0 and entry.width in (8, 16))):
                continue
            raise ValueError(f"不支持的 PDO 字段 {entry.index:04X}:{entry.sub:02X}/{entry.width}")
        if offset != size * 8:
            raise ValueError(f"PDO 映射 {offset} 位与缓冲区 {size} 字节不符")
        for index in indices:
            fields = [e for e in entries if e.index == index]
            values = [e for e in fields if e.sub == value_sub and e.width == 16]
            if len(values) != 1:
                raise ValueError(f"通道 0x{index:04X} 缺少唯一的 INT16 值")
            self.values.append(values[0])
            self.flags.append([e for e in fields if e != values[0]])

    def describe(self):
        return [asdict(e) for e in self.entries]

    def decode(self, data):
        if len(data) != self.size:
            raise ValueError("输入缓冲区长度改变")
        image = int.from_bytes(data, "little")
        raw, flags = [], []
        for value, fields in zip(self.values, self.flags):
            word = (image >> value.offset) & 0xffff
            raw.append(word - 65536 if word & 0x8000 else word)
            flags.append({FLAGS.get(e.sub, "status"): (image >> e.offset) & ((1 << e.width) - 1)
                          for e in fields} if fields else None)
        return raw, flags

    def encode(self, values):
        if len(values) != len(self.values):
            raise ValueError("输出通道数量不匹配")
        image = 0
        for value, entry in zip(values, self.values):
            image |= (raw_voltage(validated_voltage(value)) & 0xffff) << entry.offset
        return image.to_bytes(self.size, "little")
