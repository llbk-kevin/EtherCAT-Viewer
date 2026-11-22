"use strict";

const traceColors = ["#135ca6", "#a94800", "#713fa5", "#00746b"];
const HISTORY_SECONDS = 120;

function firstAt(rows, time) {
  let first = 0, last = rows.length;
  while (first < last) {
    const mid = (first + last) >>> 1;
    if (rows[mid].x < time) first = mid + 1;
    else last = mid;
  }
  return first;
}

function plotData(rows, from, to, pixels, channel) {
  const x = [], y = [];
  let low, high, previous, bucket = -1;
  const push = (row, value) => { x.push(row.x); y.push(value); };
  const flush = () => {
    if (!low) return;
    const pair = low === high ? [low] : low.x <= high.x ? [low, high] : [high, low];
    for (const row of pair) push(row, row.values[channel]);
    low = high = null;
  };
  const start = Math.max(0, firstAt(rows, from) - 1);
  const end = Math.min(rows.length, firstAt(rows, to) + 1);
  for (let i = start; i < end; i++) {
    const row = rows[i], value = row.values[channel];
    if (previous && (row.seq !== previous.seq + 1 || row.session !== previous.session)) {
      flush();
      push({x: (previous.x + row.x) / 2}, null);
    }
    const nextBucket = Math.floor((row.x - from) * pixels / (to - from));
    if (nextBucket !== bucket) {
      flush();
      bucket = nextBucket;
    }
    if (value == null) {
      flush();
      if (y.length === 0 || y.at(-1) !== null) push(row, null);
    } else {
      if (!low || value < low.values[channel]) low = row;
      if (!high || value > high.values[channel]) high = row;
    }
    previous = row;
  }
  flush();
  return [x, y];
}

class SharedCharts {
  constructor(label) {
    this.label = label;
    this.plots = [];
    this.observers = [];
    this.rows = [];
    this.start = 0;
    this.origin = null;
    this.following = true;
    this.width = 30;
    this.from = 0;
    this.to = 30;
    this.frame = null;
    this.lastDraw = 0;
  }

  reset() {
    for (const observer of this.observers) observer.disconnect();
    for (const plot of this.plots) plot.destroy();
    this.plots = [];
    this.observers = [];
    this.rows = [];
    this.start = 0;
    this.origin = null;
    this.follow();
  }

  attach(box, index, output = false) {
    const plot = new uPlot({
      width: Math.max(1, box.clientWidth), height: 125,
      legend: {show: false},
      scales: {x: {time: false, range: () => [this.from, this.to]},
               y: {range: (_u, min, max) => [Math.min(-1.1, min ?? 0), Math.max(1.1, max ?? 0)]}},
      axes: [{stroke: "#354b64", size: 24, space: 55, grid: {stroke: "#e1e9f0"}},
             {stroke: "#354b64", size: 39, grid: {stroke: "#e1e9f0"}}],
      cursor: {drag: {x: true, y: false, setScale: false, dist: 6}},
      series: [{}, {stroke: traceColors[index % 4], width: 1.5, spanGaps: false,
                    ...(output ? {paths: uPlot.paths.stepped({align: 1})} : {})}],
      hooks: {setSelect: [u => {
        if (u.select.width < 6) return;
        const left = u.posToVal(u.select.left, "x");
        const right = u.posToVal(u.select.left + u.select.width, "x");
        u.setSelect({left: 0, top: 0, width: 0, height: 0}, false);
        this.select(left, right);
      }]}
    }, [[], []], box);
    const wheel = event => {
      event.preventDefault();
      const fraction = (event.clientX - plot.over.getBoundingClientRect().left) / plot.over.clientWidth;
      this.zoom(event.deltaY < 0 ? 0.8 : 1.25, Math.max(0, Math.min(1, fraction)));
    };
    plot.over.addEventListener("wheel", wheel, {passive: false});
    plot.over.addEventListener("dblclick", () => this.follow());
    const observer = new ResizeObserver(() => {
      plot.setSize({width: Math.max(1, box.clientWidth), height: 125});
      this.schedule();
    });
    observer.observe(box);
    this.observers.push(observer);
    this.plots.push(plot);
  }

  append(samples) {
    for (const sample of samples) {
      this.origin ??= sample.time_ns;
      const time = (sample.time_ns - this.origin) / 1e9;
      if (this.rows.length && sample.seq <= this.rows.at(-1).seq) continue;
      this.rows.push({x: time, seq: sample.seq, session: sample.session,
                      values: [...sample.ao, ...sample.ai]});
    }
    const cutoff = (this.rows.at(-1)?.x ?? 0) - HISTORY_SECONDS;
    while (this.start < this.rows.length && this.rows[this.start].x < cutoff) this.start++;
    if (this.start > 2000) {
      this.rows = this.rows.slice(this.start);
      this.start = 0;
    }
    this.schedule();
  }

  select(from, to) {
    const span = Math.max(0.05, Math.min(HISTORY_SECONDS, to - from));
    const first = this.rows[this.start]?.x ?? 0;
    const latest = this.rows.at(-1)?.x ?? 0;
    this.width = span;
    this.following = false;
    this.from = Math.max(first, Math.min(from, Math.max(first, latest - span)));
    this.to = this.from + span;
    this.schedule();
  }

  zoom(factor, anchor = 0.5) {
    const value = this.from + (this.to - this.from) * anchor;
    const span = Math.max(0.05, Math.min(HISTORY_SECONDS, this.width * factor));
    this.select(value - span * anchor, value + span * (1 - anchor));
  }

  follow() {
    this.following = true;
    this.width = 30;
    this.schedule();
  }

  schedule() {
    if (this.frame !== null) return;
    this.frame = requestAnimationFrame(now => {
      this.frame = null;
      if (now - this.lastDraw < 45) {
        this.schedule();
        return;
      }
      this.lastDraw = now;
      this.draw();
    });
  }

  draw() {
    if (this.following) {
      this.to = Math.max(this.width, this.rows.at(-1)?.x ?? 0);
      this.from = this.to - this.width;
    }
    const first = this.rows[this.start]?.x ?? 0;
    if (!this.following && this.from < first) {
      this.from = first;
      this.to = first + this.width;
    }
    this.label.textContent = this.width.toFixed(this.width < 1 ? 2 : 1) + " s · " +
                             (this.following ? "实时" : "历史");
    this.plots.forEach((plot, index) => {
      const data = plotData(this.rows, this.from, this.to, plot.width, index);
      plot.batch(() => {
        plot.setData(data, false);
        plot.setScale("x", {min: this.from, max: this.to});
        plot.setScale("y", {min: null, max: null});
      });
    });
  }
}
