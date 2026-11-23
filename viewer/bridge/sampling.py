"""按源样本时间生成保持段；不按逻辑采样点逐个进行 Python 循环。"""
import math
import struct


class Sampler:
    def __init__(self, mappings, rate, policy, full_scale, target_rate):
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 1 <= rate <= 200000:
            raise ValueError("逻辑采样率必须为 1–200000 Hz")
        if policy == "native" and rate > target_rate * (1+1e-9):
            raise ValueError(f"真实采样策略拒绝 {rate:g} Hz；当前目标 PDO 更新率为 {target_rate:g} Hz")
        self.mappings, self.rate, self.policy, self.full_scale = mappings, rate, policy, full_scale
        self.fresh_ns = max(100_000_000, round(3e9 / target_rate))
        self.previous = None
        self.origin = None
        self.epoch_generated = 0
        self.generated = self.held = self.sources = self.clipped = 0

    def pause(self):
        self.previous = self.origin = None
        self.epoch_generated = self.generated

    def vector(self, sample):
        values, valid, mapped, clipped = [], [], [], []
        for sources in self.mappings:
            value = 0.0
            for index, sign in sources:
                if index >= len(sample["ai"]) or not sample["valid"][index] or sample["ai"][index] is None:
                    raise ValueError("映射输入的源数据无效")
                value += sample["ai"][index] * sign
            raw = round(value * 32768 / self.full_scale)
            clipped.append(raw < -32768 or raw > 32767)
            values.append(min(32767, max(-32768, raw)))
            mapped.append(bool(sources))
            valid.append(bool(sources))
        self.clipped += sum(clipped)
        return struct.pack("<"+"h"*len(values), *values), valid, mapped, clipped

    @staticmethod
    def stamp(sample):
        return sample.get("host_send_ns") or sample["time_ns"]

    def feed(self, sample):
        stamp = self.stamp(sample)
        row, valid, mapped, clipped = self.vector(sample)
        previous = self.previous
        if previous is not None:
            old = previous[0]
            delta = stamp - self.stamp(old)
            if sample["session"] != old["session"] or sample["seq"] != old["seq"]+1:
                raise ValueError("原始源样本序号或连接代次中断")
            if delta <= 0 or delta > self.fresh_ns:
                raise ValueError("源数据时间中断或过期")
        self.sources += 1
        self.origin = stamp if self.origin is None else self.origin
        chunks = []
        if self.policy == "native":
            # Select at most one new physical sample; never manufacture native samples.
            desired = self.origin + (self.generated-self.epoch_generated) * 1e9 / self.rate
            if stamp >= desired:
                chunks.append((row, 1, self.metadata(sample, stamp, 0, valid, mapped, clipped)))
                self.generated += 1
        elif previous is not None:
            end = math.ceil((stamp - self.origin) * self.rate / 1e9 - 1e-9)
            count = max(0, end - (self.generated-self.epoch_generated))
            if count:
                old, old_row, old_valid, old_mapped, old_clipped = previous
                if len(old_row)*count > 512*1024:
                    raise ValueError("数据块过大，请减少虚拟通道数或逻辑采样率")
                start = self.origin + round((self.generated-self.epoch_generated) * 1e9 / self.rate)
                chunks.append((old_row * count, count,
                               self.metadata(old, start, count-1, old_valid, old_mapped, old_clipped)))
                self.generated += count
                self.held += max(0, count-1)
        self.previous = (sample, row, valid, mapped, clipped)
        return chunks

    def metadata(self, sample, start, held, valid, mapped, clipped):
        return dict(source_seq=sample["seq"], source_session=sample["session"],
                    logical_start_ns=start, logical_interval_ns=1e9/self.rate,
                    host_send_ns=sample.get("host_send_ns"), host_return_ns=sample.get("host_return_ns"),
                    timestamp_source="host_pdo_exchange", valid=valid, mapped=mapped, clipped=clipped,
                    held_count=held, sampling=self.policy, full_scale=self.full_scale,
                    source_interval_ms=sample.get("interval_ms"))
