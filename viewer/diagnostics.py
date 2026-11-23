"""有界诊断历史、事件与故障片段；磁盘写入不占用通信线程。"""
from array import array
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Thread
import time

FIELDS = ("time_ms", "interval_ms", "roundtrip_ms", "period_ms", "wkc", "expected_wkc", "session")


class History:
    def __init__(self, seconds=1800):
        self.seconds = seconds
        self.chunks = deque()
        self.offset = time.time()*1000-time.perf_counter_ns()/1e6
        self.previous = None

    def add(self, samples):
        rows = array("d")
        for s in samples:
            stamp = s.get("host_send_ns") or s["time_ns"]
            at = self.offset+stamp/1e6
            if self.previous and s["session"] != self.previous[1]:
                rows.extend([(self.previous[0]+at)/2, *([math.nan]*5), s["session"]])
            self.previous = (at,s["session"])
            rows.extend([at, *[float(s[k]) if s.get(k) is not None else math.nan
                         for k in FIELDS[1:]]])
        if rows:
            self.chunks.append(rows)
            cutoff = rows[-7]-self.seconds*1000
            while self.chunks and self.chunks[0][-7] < cutoff:
                self.chunks.popleft()

    def query(self, start=None, end=None, limit=4000):
        end = float(end or time.time()*1000)
        start = float(start if start is not None else end-30000)
        if not math.isfinite(start) or not math.isfinite(end) or start > end:
            raise ValueError("诊断时间范围无效")
        limit = min(20000, max(32, int(limit)))
        buckets = max(1, limit//8)
        width = max(0.001, (end-start)/buckets)
        selected, group, last_bucket = [], [], None
        def flush():
            if not group:
                return
            indices = {0, len(group)-1}
            for key in (1, 2, 4):
                invalid = [i for i,r in enumerate(group) if math.isnan(r[key])]
                if invalid:
                    indices.update((invalid[0],invalid[-1]))
                good = [i for i,r in enumerate(group) if not math.isnan(r[key])]
                if good:
                    indices.update((min(good, key=lambda i:group[i][key]), max(good, key=lambda i:group[i][key])))
            selected.extend(group[i] for i in sorted(indices))
        # 导出在线程中执行；固定已发布的不可变批次，避免采集中修改 deque。
        for chunk in tuple(self.chunks):
            if chunk[-7] < start or chunk[0] > end:
                continue
            for offset in range(0, len(chunk), 7):
                stamp = chunk[offset]
                if not start <= stamp <= end:
                    continue
                bucket = int((stamp-start)/width)
                if bucket != last_bucket:
                    flush()
                    group = []
                    last_bucket = bucket
                group.append(list(chunk[offset:offset+7]))
        flush()
        return dict(fields=FIELDS, rows=[[None if math.isnan(v) else v for v in row] for row in selected],
                    start_ms=start, end_ms=end, retention_seconds=self.seconds, decimation="bucket_min_max")


class Journal:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.events = deque(maxlen=2000)
        self.queue = Queue(maxsize=256)
        self.counter = self.dropped = 0
        self.error = ""
        self.thread = Thread(target=self._writer, daemon=True, name="diagnostic-journal")
        self.thread.start()

    def log(self, category, message, severity="info", **details):
        self.counter += 1
        record = dict(id=self.counter, time=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                      time_ms=time.time()*1000, monotonic_ns=time.perf_counter_ns(), category=category,
                      severity=severity, message=message, **details)
        self.events.append(record)
        try:
            self.queue.put_nowait(record)
        except Full:
            self.dropped += 1
        return record

    def list(self, category=None, after=0):
        return [{k:v for k,v in e.items() if k not in ("before","after")} for e in self.events if e["id"] > after and (not category or e["category"] == category)]

    def _writer(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder/"events.jsonl"
        while True:
            record = self.queue.get()
            if record is None:
                self.queue.task_done()
                break
            try:
                if target.exists() and target.stat().st_size > 10*1024*1024:
                    target.rename(self.folder/f"events-{time.time_ns()}.jsonl")
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+"\n")
                files = sorted(self.folder.glob("events*.jsonl"), key=lambda p:p.stat().st_mtime, reverse=True)
                total = 0
                for p in files:
                    total += p.stat().st_size
                    if p != target and (time.time()-p.stat().st_mtime > 30*86400 or total > 200*1024*1024):
                        p.unlink()
            except (OSError, ValueError) as exc:
                self.error = str(exc)
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.put(None, timeout=1)
        self.thread.join(2)
