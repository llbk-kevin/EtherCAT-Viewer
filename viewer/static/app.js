"use strict";
const el = id => document.getElementById(id);
const charts = new SharedCharts(el("time-window"));
const channelCards = [];
let socket, token, lastSample, sequence = 0, pending = false;
let state = {}, lastFrame = 0, pendingPeriod = false;

function channelCard(channel, output, source) {
  const name = (output ? "AO" : "AI") + channel;
  const card = document.createElement("article");
  card.className = "chart-card";
  card.style.setProperty("--trace", traceColors[(channel - 1) % 4]);
  card.innerHTML = '<div class="chart-head"><strong>' + name + '</strong>' +
    '<output class="reading" aria-label="' + name + ' 电压">—</output></div>' +
    '<div class="plot" aria-label="' + name + ' 电压波形"></div>';
  card.dataset.module = source ? String(source.slave_position) : "";
  if (source) {
    const origin = document.createElement("p");
    origin.className = "channel-origin";
    origin.textContent = source.label;
    card.insertBefore(origin, card.querySelector(".plot"));
  }
  if (output) {
    const form = document.createElement("form");
    form.className = "generator";
    form.innerHTML = '<div class="generator-form">' +
      '<label>波形<select aria-label="' + name + ' 波形" id="shape-' + channel + '">' +
      '<option value="dc">直流</option><option value="sine">正弦</option>' +
      '<option value="triangle">三角</option><option value="square">方波</option></select></label>' +
      '<label><span class="voltage-label">电压 / V</span><input aria-label="' + name + ' 电压或峰值" ' +
      'id="voltage-' + channel + '" type="number" min="-10" max="10" step="any" required value="0"></label>' +
      '<label>频率 / Hz<input aria-label="' + name + ' 频率" id="frequency-' + channel + '" ' +
      'type="number" min="0.01" max="25" step="any" required value="0.2"></label></div>' +
      '<p class="wave-info"></p><div class="group"><button class="apply">应用</button>' +
      '<button class="stop" type="button">停波清零</button></div>';
    const [shape, voltage, frequency] = form.querySelectorAll("select,input");
    form.oninput = () => renderGenerator(form, canOutput());
    form.onsubmit = event => {
      event.preventDefault();
      const value = voltage.valueAsNumber;
      if (shape.value === "dc") command("set_output", {channel, voltage: value});
      else command("wave", {channel, shape: shape.value, peak: value, frequency: frequency.valueAsNumber});
    };
    form.querySelector(".stop").onclick = () => command("stop_wave", {channel});
    card.append(form);
  }
  el(output ? "outputs" : "inputs").append(card);
  channelCards.push(card);
  charts.attach(card.querySelector(".plot"), channelCards.length - 1, output);
}

let channelLayout = "";
let aoCount = 4;
const bridgePanel = new BridgePanel((action, args = {}) => command(action, args));
function ensureChannels() {
  const known = !!state.fingerprint;
  const channels = known ? state.channels : {ai: Array.from({length: 4}, () => null), ao: Array.from({length: 4}, () => null)};
  if (!channels) return;
  const key = JSON.stringify([state.fingerprint, channels.ai, channels.ao]);
  if (key === channelLayout) return;
  channelLayout = key;
  charts.reset();
  channelCards.length = 0;
  el("outputs").replaceChildren();
  el("inputs").replaceChildren();
  aoCount = channels.ao.length;
  for (const output of [true, false]) {
    channels[output ? "ao" : "ai"].forEach((source, i) => channelCard(i + 1, output, source));
  }
  const filter = el("module-filter"), previous = filter.value;
  filter.replaceChildren(new Option("全部模块", ""));
  const modules = new Map([...channels.ai, ...channels.ao].filter(Boolean).map(c => [String(c.slave_position), c.module]));
  for (const [position, name] of modules) filter.add(new Option("从站 " + position + " · " + name, position));
  filter.value = [...filter.options].some(o => o.value === previous) ? previous : "";
  filter.onchange();
}
el("module-filter").onchange = () => {
  channelCards.forEach(card => card.hidden = !!el("module-filter").value && card.dataset.module !== el("module-filter").value);
};

el("zoom-in").onclick = () => charts.zoom(0.5);
el("zoom-out").onclick = () => charts.zoom(2);
el("follow").onclick = () => charts.follow();

function canOutput() {
  return token && state.controller && state.connection === "OP" && state.armed && !state.bridge?.active && !pending;
}
function text(id, value) {
  const node = el(id);
  if (node.textContent !== value) node.textContent = value;
}
function showMessage(message, error = false) {
  text("message", message);
  el("message").hidden = !message;
  el("message").classList.toggle("error", error);
}
function format(value, digits = 3, unit = " ms") {
  return Number.isFinite(value) ? value.toFixed(digits) + unit : "—";
}
function setRows(id, rows) {
  const body = el(id);
  const key = JSON.stringify(rows);
  if (body.dataset.key === key) return;
  body.dataset.key = key;
  body.replaceChildren();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) tr.insertCell().textContent = value;
  }
}

function renderGenerator(form, enabled) {
  const [shape, voltage, frequency] = form.querySelectorAll("select,input");
  const dc = shape.value === "dc";
  const period = state.period_ms || 10;
  const limit = Math.min(100, 250 / period);
  voltage.min = dc ? "-10" : "0";
  frequency.max = String(limit);
  form.querySelector(".voltage-label").textContent = dc ? "电压 / V" : "峰值 / V";
  shape.disabled = voltage.disabled = form.querySelector(".apply").disabled = form.querySelector(".stop").disabled = !enabled;
  frequency.disabled = !enabled || dc;
  const points = 1000 / period / frequency.valueAsNumber;
  const info = form.querySelector(".wave-info");
  info.classList.toggle("warning", !dc && points < 20);
  info.textContent = dc ? "−10 至 +10 V" : "约 " +
    (Number.isFinite(points) && points > 0 ? points.toFixed(1) : "—") +
    " 点 / 波形周期" + (points < 20 ? " · 台阶明显" : "");
}

function renderDiagnostics(online) {
  const stats = state.timing || {};
  const period = state.period_ms || 10;
  text("target-metric", period + " ms");
  text("interval-metric", online ? format(stats.interval?.latest) : "—");
  text("roundtrip-metric", online ? format(stats.roundtrip?.latest) : "—");
  text("rate-metric", online ? format(stats.update_hz, 1, " Hz") : "—");
  text("wkc", online ? (state.wkc ?? "—") + " / " + (state.expected_wkc ?? "—") : "—");
  text("error-metric", String(state.errors ?? 0));
  text("missed-metric", String(state.missed ?? 0));
  text("bus-summary", online ? (state.devices?.length ?? 0) + " 个从站处于 OP · " + (state.watchdog_ms == null ? "无 AO 看门狗" : "看门狗 " + state.watchdog_ms + " ms") : "等待实机连接");
  const lastWkc = !online && state.wkc != null ? " · 最后 WKC " + state.wkc + "/" + state.expected_wkc : "";
  const lastError = state.last_error && state.last_error !== state.reason ? "上次异常 " + state.last_error : "";
  text("fault-detail", [...new Set([state.reason, state.zero, lastError].filter(Boolean))].join(" · ") + lastWkc);
  setRows("timing-details", [["实际周期间隔", stats.interval], ["主站收发耗时", stats.roundtrip]]
    .map(([name, series]) => [name, ...["latest", "median", "p95", "max"].map(key => format(series?.[key])), series?.count ?? 0]));
  text("viewer-drops", "工作进程遥测遗漏 " + (state.dropped ?? 0) + " · 本页推送遗漏 " + (state.viewer_drops ?? 0));
  setRows("devices", (state.devices || []).map(d => [d.name, d.vendor + " / " + d.product, d.revision, d.state, d.al_status]));
  if (el("input-details").parentElement.open) text("input-details", online && lastSample ? lastSample.raw.map((v, i) =>
    "AI" + (i + 1) + " " + (v ?? "—") + " · " + (JSON.stringify(lastSample.flags[i]) ?? "—")).join(" | ") : "输入状态未提供");
  const mapping = el("mapping-text");
  mapping.hidden = !Object.keys(state.mapping || {}).length;
  if (!mapping.hidden && mapping.parentElement.open) text("mapping-text", JSON.stringify(state.mapping, null, 2));
}

function render() {
  ensureChannels();
  const online = token && state.connection === "OP";
  const controller = token && state.controller;
  text("connection", online ? "已连接 · OP" : pending && state.connection === "CONNECTING" ? "连接中" : "掉线");
  el("connection").classList.toggle("online", Boolean(online));
  text("claim", controller ? "本页控制中" : state.controller_busy ? "另一页面控制中" : "取得控制权");
  el("claim").disabled = !token || state.controller_busy || pending;
  el("connect").disabled = !controller || online || !el("adapters").value || pending;
  el("disconnect").disabled = !controller || !online || pending;
  el("refresh").disabled = !token || online || pending;
  el("adapters").disabled = !token || online || pending;
  el("arm").disabled = !controller || !online || !state.ready || state.armed || state.bridge?.active || pending;
  el("zero").disabled = !controller || !online;
  text("zero", state.bridge?.active ? "清零并收回控制权" : "清零并锁定");
  text("lock", online && state.armed ? "输出已解锁" : "输出已锁定");
  el("record").disabled = !controller || (!online && !state.recording) || pending;
  text("record", state.recording ? "停止记录" : "开始记录");
  el("export").hidden = !state.export;
  el("period-form").querySelector("button").disabled = !controller || state.bridge?.active || pending;
  if (!pendingPeriod && document.activeElement !== el("period")) el("period").value = state.period_ms || 10;
  const samples = online && lastSample?.session === state.session ? lastSample : null;
  channelCards.forEach((card, index) => {
    const value = index < aoCount ? samples?.ao[index] : samples?.ai[index - aoCount];
    card.querySelector(".reading").textContent = format(value, 3, " V");
    if (index < aoCount) renderGenerator(card.querySelector(".generator"), canOutput());
  });
  renderDiagnostics(online);
  bridgePanel.render(state, pending);
}

async function refresh() {
  try {
    const response = await fetch("/api/adapters");
    const data = await response.json();
    if (!response.ok) throw Error(data.detail);
    const select = el("adapters");
    const previous = select.value;
    select.replaceChildren(new Option(data.items.length ? "请选择有线网卡" : "未发现物理有线网卡", ""));
    for (const item of data.items) {
      const option = new Option([item.name, item.status === "Up" ? "已连接" : "链路未连接",
        item.speed, item.interface ? "" : "Npcap 不可用"].filter(Boolean).join(" · "), item.interface || "");
      option.disabled = item.status !== "Up" || !item.interface;
      select.add(option);
      if (!option.disabled && option.value === previous) option.selected = true;
    }
    if (data.error) showMessage(data.error, true);
  } catch (error) { showMessage(error.message, true); }
  render();
}

async function command(action, args = {}) {
  if (!token || (pending && action !== "zero")) return;
  pending = true;
  render();
  try {
    const response = await fetch("/api/commands", {method: "POST",
      headers: {"content-type": "application/json", "x-viewer-session": token},
      body: JSON.stringify({id: String(++sequence), action, args})});
    const data = await response.json();
    if (!response.ok || !data.ok) throw Error(data.error || data.detail || "命令失败");
    if (data.result) state = {...state, ...data.result};
    if (action === "set_period") {
      pendingPeriod = false;
      el("period").value = state.period_ms;
    }
    showMessage(action === "zero" ? "已清零并锁定输出" : "");
    return data;
  } catch (error) { showMessage(error.message, true); }
  finally { pending = false; render(); }
}

el("refresh").onclick = refresh;
el("adapters").onchange = render;
el("claim").onclick = () => command("claim");
el("connect").onclick = () => command("connect", {interface: el("adapters").value});
for (const action of ["disconnect", "arm", "zero"]) el(action).onclick = () => command(action);
el("record").onclick = () => command(state.recording ? "record_stop" : "record_start");
el("period").oninput = () => { pendingPeriod = true; };
el("period-form").onsubmit = event => {
  event.preventDefault();
  command("set_period", {period_ms: el("period").valueAsNumber});
};

function connectWebSocket() {
  socket = new WebSocket("ws://" + location.host + "/ws");
  socket.onmessage = event => {
    lastFrame = performance.now();
    const packet = JSON.parse(event.data);
    if (packet.kind === "hello") {
      token = packet.token;
      // A service restart breaks the timestamp epoch and sample sequence.
      if (charts.rows.length && (!packet.samples.length ||
          packet.samples.at(-1).seq < charts.rows.at(-1).seq)) {
        charts.rows = [];
        charts.start = 0;
        charts.origin = null;
        lastSample = null;
        charts.follow();
      }
      if (!el("adapters").value) refresh();
    }
    state = packet.status;
    ensureChannels();
    if (packet.samples.length) {
      lastSample = packet.samples.at(-1);
      charts.append(packet.samples);
    }
    render();
  };
  socket.onclose = () => {
    token = null;
    state = {...state, connection: "DISCONNECTED", reason: "网页与本地服务的连接已中断"};
    render();
    setTimeout(connectWebSocket, 1000);
  };
}
setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    if (token && performance.now() - lastFrame > 3000) socket.close();
    else if (state.controller) socket.send("heartbeat");
  }
}, 500);
render();
connectWebSocket();
