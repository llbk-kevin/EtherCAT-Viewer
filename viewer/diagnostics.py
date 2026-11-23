"""有界诊断历史、事件与故障片段；磁盘写入不占用通信线程。"""
from array import array
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Thread, RLock, Lock, Event
import hashlib
import uuid
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
        self.generation = uuid.uuid4().hex
        self.lock, self.submission = RLock(), Lock()
        self.locations, self.pending = {}, {}
        self.folder.mkdir(parents=True, exist_ok=True)
        self._restore()
        self.thread = Thread(target=self._writer, daemon=True, name="diagnostic-journal")
        self.thread.start()

    @staticmethod
    def _summary(record):
        summary = {k:record[k] for k in ("id","event_id","time","time_ms","category","severity","message","revision") if k in record}
        summary["fragment_state"] = record.get("fragment_state", "complete" if "before" in record or "after" in record else "none")
        return summary

    def _remember(self, summary):
        if len(self.events) == self.events.maxlen:
            old = self.events[0]["event_id"]
            self.locations.pop(old, None)
            self.pending.pop(old, None)
        self.events.append(summary)

    def _restore(self):
        """只在内存保存摘要与磁盘偏移；大段周期数据在展开时读取。"""
        summaries = {}
        files = sorted(self.folder.glob("events*.jsonl"), key=lambda p:(p.name == "events.jsonl", p.stat().st_mtime))
        for file in files:
            if time.time()-file.stat().st_mtime > 30*86400:
                continue
            with file.open("rb") as stream:
                while True:
                    offset, line = stream.tell(), stream.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                        ident = record.get("event_id") or hashlib.sha256(line.rstrip()).hexdigest()
                        if record.get("_update"):
                            if ident in summaries:
                                summaries[ident].update(self._summary(record))
                                self.locations[ident].append((file,offset))
                            continue
                        record["event_id"] = ident
                        record.setdefault("revision", 1)
                        self.counter = max(self.counter,int(record.get("id",0)))
                        summaries[ident] = self._summary(record)
                        self.locations[ident] = [(file,offset)]
                        if len(summaries) > 2000:
                            oldest = next(iter(summaries))
                            del summaries[oldest]
                            self.locations.pop(oldest,None)
                    except (ValueError, TypeError, KeyError):
                        self.error = "部分诊断记录损坏，已跳过；其他记录仍可查看"
        for summary in summaries.values():
            if summary["fragment_state"] == "collecting":
                summary["fragment_state"] = "incomplete"
            self.events.append(summary)

    def log(self, category, message, severity="info", **details):
        with self.submission, self.lock:
            if "revision" in details:
                details["config_revision"] = details.pop("revision")
            self.counter += 1
            record = dict(id=self.counter,event_id=uuid.uuid4().hex,revision=1,
                          time=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                          time_ms=time.time()*1000,monotonic_ns=time.perf_counter_ns(),category=category,
                          severity=severity,message=message,**details)
            self._remember(self._summary(record))
            self.pending[record["event_id"]] = [record]
            self._enqueue(record)
        return record

    def _enqueue(self, record):
        try:
            self.queue.put_nowait(("write", self.generation, record))
        except Full:
            self.dropped += 1
            self.error = "诊断写入队列已满，部分记录尚未持久化"

    def update(self, ident, generation, **details):
        with self.submission, self.lock:
            if generation != self.generation:
                return False
            summary = next((e for e in self.events if e["event_id"] == ident),None)
            if summary is None:
                return False
            record = dict(_update=True,event_id=ident,revision=summary.get("revision",1)+1,**details)
            summary.update(self._summary(record))
            self.pending.setdefault(ident,[]).append(record)
            self._enqueue(record)
            return True

    def list(self, category=None, after=0):
        with self.lock:
            return [dict(e) for e in self.events if e["id"] > after and (not category or e["category"] == category)]

    def detail(self, ident, generation=None):
        with self.lock:
            if generation is not None and generation != self.generation:
                raise ValueError("诊断日志已清空，请刷新列表")
            summary = next((e for e in self.events if e["event_id"] == ident),None)
            if summary is None:
                raise KeyError("事件已过期或已清空")
            result = {}
            for file,offset in self.locations.get(ident,[]):
                try:
                    with file.open("rb") as stream:
                        stream.seek(offset)
                        result.update(json.loads(stream.readline()))
                except (OSError,ValueError) as exc:
                    raise OSError("事件详情读取失败："+str(exc)) from exc
            for patch in self.pending.get(ident,[]):
                result.update(patch)
            result.update(summary)
            result.pop("_update",None)
            return result

    def clear(self):
        """写入屏障：不丢弃后续新事件，也不允许旧片段在清空后回填。"""
        with self.submission:
            done, outcome = Event(), {}
            self.queue.put(("clear",done,outcome), timeout=10)
            if not done.wait(15):
                raise OSError("等待诊断日志清空超时，请重新读取确认")
            if "error" in outcome:
                raise OSError(outcome["error"])
            return outcome

    def _clear_files(self):
        # 仅管理 events.jsonl / events-*.jsonl；其他日志和调试材料不属于此操作。
        files = [p for p in self.folder.glob("events*.jsonl") if p.name == "events.jsonl" or p.name.startswith("events-")]
        moved = []
        try:
            for file in files:
                temporary = file.with_name(file.name+".clearing-"+uuid.uuid4().hex)
                file.rename(temporary)
                moved.append((file,temporary))
        except OSError:
            for original,temporary in reversed(moved):
                temporary.rename(original)
            raise
        try:
            for _,temporary in moved:
                temporary.unlink()
        finally:
            # 已撤下的日志不再进入列表；删除失败需明确报告，不声称成功。
            self.events.clear()
            self.locations.clear()
            self.pending.clear()
            self.generation = uuid.uuid4().hex
        return len(files)

    def _writer(self):
        target = self.folder/"events.jsonl"
        while True:
            entry = self.queue.get()
            if entry is None:
                self.queue.task_done()
                break
            try:
                with self.lock:
                    if entry[0] == "clear":
                        _,done,outcome = entry
                        try:
                            count = self._clear_files()
                            self.error = ""
                            outcome.update(generation=self.generation,files_removed=count)
                        except OSError as exc:
                            self.error = outcome["error"] = "诊断日志清空失败："+str(exc)
                        finally:
                            done.set()
                        continue
                    _,generation,record = entry
                    if generation != self.generation:
                        continue
                    if target.exists() and target.stat().st_size > 10*1024*1024:
                        rotated = self.folder/f"events-{time.time_ns()}.jsonl"
                        target.rename(rotated)
                        for ident,locations in self.locations.items():
                            self.locations[ident] = [(rotated if p == target else p,offset) for p,offset in locations]
                    encoded = (json.dumps(record,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
                    with target.open("ab") as stream:
                        offset = stream.tell()
                        stream.write(encoded)
                    ident = record["event_id"]
                    if any(e["event_id"] == ident for e in self.events):
                        self.locations.setdefault(ident,[]).append((target,offset))
                        pending = self.pending.get(ident,[])
                        if record in pending:
                            pending.remove(record)
                        if not pending:
                            self.pending.pop(ident,None)
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
