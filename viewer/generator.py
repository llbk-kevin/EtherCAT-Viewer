from dataclasses import dataclass
import math

DEFAULT_PERIOD_MS = 10
WAVEFORMS = ("sine", "triangle", "square")


def max_frequency(period_ms):
    return min(100, 250 / period_ms)


def validated_period(value):
    if type(value) is not int or not 1 <= value <= 50:
        raise ValueError("通信周期必须为 1–50 ms 的整数")
    return value


@dataclass(frozen=True)
class Wave:
    shape: str
    peak: float
    frequency: float
    start_ns: int

    def voltage_at(self, now_ns):
        phase = ((now_ns - self.start_ns) * self.frequency / 1e9) % 1
        if self.shape == "sine":
            return self.peak * math.sin(2 * math.pi * phase)
        if self.shape == "triangle":
            return self.peak * (1 - 4 * abs(((phase + 0.25) % 1) - 0.5))
        return self.peak if phase < 0.5 else -self.peak


def build_wave(shape, peak, frequency, period_ms, now_ns):
    if shape not in WAVEFORMS:
        raise ValueError("请选择正弦波、三角波或方波")
    if type(peak) not in (int, float) or not 0 <= peak <= 10:
        raise ValueError("峰值必须在 0–10 V 内")
    limit = max_frequency(period_ms)
    if type(frequency) not in (int, float) or not 0.01 <= frequency <= limit:
        raise ValueError(f"频率必须在 0.01–{limit:g} Hz 内")
    return Wave(shape, peak, frequency, now_ns)


@dataclass(frozen=True)
class TableWave:
    table: tuple
    rate: float
    start_ns: int

    @property
    def frequency(self):
        return self.rate / len(self.table) if len(set(self.table)) > 1 else 0

    def voltage_at(self, now_ns):
        return self.table[int(max(0, now_ns - self.start_ns) * self.rate / 1e9) % len(self.table)]

    def reclock(self, rate, now_ns):
        position = max(0, now_ns - self.start_ns) * self.rate / 1e9
        return TableWave(self.table, rate, now_ns - round(position / rate * 1e9))


def build_table(table, rate, period_ms, now_ns):
    if not isinstance(table, (list, tuple)) or not 1 <= len(table) <= 65536:
        raise ValueError("波形表需要 1–65536 个电压值")
    from .pdo import validated_voltage
    values = tuple(validated_voltage(v) for v in table)
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 13_000_000:
        raise ValueError("波形更新率无效")
    wave = TableWave(values, rate, now_ns)
    if wave.frequency > max_frequency(period_ms):
        raise ValueError(f"波形频率 {wave.frequency:g} Hz 超过当前周期上限 {max_frequency(period_ms):g} Hz")
    return wave
