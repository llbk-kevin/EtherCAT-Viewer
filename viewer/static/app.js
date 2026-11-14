"use strict";
const el = id => document.getElementById(id);
const colors = ["#48d6c7", "#ffc374", "#a8a5ff", "#72b8ff"];
const flagNames = {underrange:"欠量程", overrange:"超量程", error:"错误", sync_error:"同步错误",
  invalid:"数据无效", limit1:"限值 1", limit2:"限值 2", toggle:"更新位", status:"状态字"};
const connectionNames = {STARTING:"启动中", RUNNING:"模拟运行", OP:"OP", FAULT:"故障",
  STALE:"数据超时", DISCONNECTED:"已断开", CONNECTING:"连接中"};
const inputs = [];
const outputs = [];
for (let i = 0; i < 4; i++) {
  const ch = i + 1;
  const loop = ch === 1 || ch === 4;
  const card = document.createElement("div");
  card.className = "input";
  card.innerHTML = '<div class="input-title">AI' + ch + '</div><div class="reading">—</div>' +
    '<small>' + (loop ? '回环 AO' + ch : '接线记录：悬空') + '</small><small class="raw"></small><small class="flags"></small>';
  card.firstElementChild.style.color = colors[i];
  el("inputs").append(card);
  inputs.push(card);

  const row = document.createElement("div");
  row.className = "ao";
  row.innerHTML = '<div class="ao-head"><strong>AO' + ch + '</strong><span>' +
    (loop ? '→ AI' + ch : '固定零值') + '</span><output>指令 0.000 V</output></div>' +
    (loop ? '<div class="group"><input id="value-' + ch + '" aria-label="AO' + ch +
    ' 电压或振幅" type="number" min="-1" max="1" step="0.001" value="0">' +
    '<button id="apply-' + ch + '">设定</button><button id="wave-' + ch + '">正弦 0.2 Hz</button></div>' : '');
  el("outputs").append(row);
  outputs.push(row);
  if (loop) {
    const value = el("value-" + ch);
    for (const [id, action] of [["apply-", "set_output"], ["wave-", "wave"]]) {
      el(id + ch).onclick = () => {
        if (!value.checkValidity() || !Number.isFinite(value.valueAsNumber)) {
          value.reportValidity();
          showMessage("请输入 −1 至 +1 V 的电压或振幅", true);
          return;
        }
        command(action, {channel:ch, voltage:value.valueAsNumber});
      };
    }
  }
}

let plot, socket, token, state = {}, last = null, history = [], origin, sequence = 0;
let connected = false, pending = false, lastSocketFrame = 0;
let mapDescription = "";
const plotBox = el("plot");
const plotOptions = {
  width: Math.max(220, plotBox.clientWidth), height:295,
  scales:{x:{time:false,range:(_u,min,max) => [Math.max(0,max-30), Math.max(30,max)]},
    y:{range:(_u,min,max) => [Math.min(-1.1,min), Math.max(1.1,max)]}},
  axes:[{label:"运行时间 / s",stroke:"#a5b5c3",grid:{stroke:"#2b3948"}},
    {stroke:"#a5b5c3",size:54,grid:{stroke:"#2b3948"}}],
  series:[{}, ...colors.map((stroke,i)=>({label:"AI"+(i+1),stroke,width:1.6,spanGaps:false,
    value:(_u,v)=>v == null ? "—" : v.toFixed(3)+" V"}))],
};
plot = new uPlot(plotOptions, [[],[],[],[],[]], plotBox);
new ResizeObserver(() => plot.setSize({width:Math.max(220,plotBox.clientWidth),height:295})).observe(plotBox);

function showMessage(text, error=false) {
  const box = el("message");
  box.textContent = text;
  box.hidden = !text;
  box.className = error ? "error" : "";
}

function render() {
  const active = connected && state.controller;
  const busyBus = ["OP", "CONNECTING"].includes(state.connection);
  const validSamples = connected && ["OP","RUNNING"].includes(state.connection);
  el("mode").textContent = state.mode || "SIMULATION";
  el("mode").classList.toggle("live", state.mode === "LIVE");
  el("connection").textContent = connected ? (connectionNames[state.connection] || state.connection) : "网页连接中断";
  el("connection").classList.toggle("fault", !validSamples);
  el("claim").textContent = active ? "本页控制中" : state.controller_busy ? "另一页面控制中" : "取得控制权";
  el("claim").disabled = !connected || state.controller_busy || pending;
  el("connect").disabled = !active || busyBus || !el("adapters").value || pending;
  el("disconnect").disabled = !active || state.mode !== "LIVE" || state.connection === "DISCONNECTED" || pending;
  el("simulate").disabled = !active || state.mode === "SIMULATION" || pending;
  el("refresh").disabled = !connected || busyBus || pending;
  el("arm").disabled = !active || !state.ready || state.armed || !validSamples || pending;
  el("zero").disabled = !active || !validSamples || pending;
  el("record").disabled = !active || (!validSamples && !state.recording) || pending;
  el("record").textContent = state.recording ? "停止记录" : "开始记录";
  el("export").hidden = !state.export;
  el("lock").textContent = connected && state.armed ? "输出已解锁" : "输出已锁定";
  el("zero-state").textContent = connected ? ([state.reason, state.zero].filter(Boolean).join(" · ") || "等待数据") : "输出状态未知";
  el("wkc").textContent = "WKC " + (state.wkc ?? "—") + " / " + (state.expected_wkc ?? "—");
  el("timing").textContent = "目标周期 10 ms" +
    (state.watchdog_ms ? " · 看门狗 " + state.watchdog_ms + " ms" : "") +
    (state.max_interval_ms ? " · 最长 " + state.max_interval_ms.toFixed(2) + " ms" : "") +
    " · WKC/状态异常 " + (state.errors ?? 0) + " · 遗漏采样 " + (state.dropped ?? 0);
  for (let i = 0; i < 4; i++) {
    const valid = validSamples && last?.valid[i];
    const raw = validSamples ? last?.raw[i] : null;
    inputs[i].querySelector(".reading").textContent = valid ? last.ai[i].toFixed(3) + " V" : "—";
    inputs[i].querySelector(".raw").textContent = "原始值 " + (raw ?? "—");
    const flags = validSamples ? last?.flags[i] : null;
    inputs[i].querySelector(".flags").textContent = flags ? Object.entries(flags)
      .map(([k,v]) => (flagNames[k] || k) + " " + v).join(" · ") : "状态未提供";
    outputs[i].querySelector("output").textContent = validSamples ?
      "指令 " + (state.ao?.[i] ?? 0).toFixed(3) + " V" : "指令 —";
    const waveform = outputs[i].querySelector('[id^="wave-"]');
    if (waveform) waveform.textContent = state.wave?.includes(i) ? "重启正弦" : "正弦 0.2 Hz";
    for (const field of outputs[i].querySelectorAll("input, button")) {
      field.disabled = !active || !state.armed || !validSamples || pending;
    }
  }
  const rows = (state.devices || []).map(d => [d.name, d.vendor+" / "+d.product, d.revision, d.state, d.al_status]);
  const body = el("devices");
  const tableKey = JSON.stringify(rows);
  if (body.dataset.key !== tableKey) {
    body.dataset.key = tableKey;
    body.replaceChildren();
    for (const row of rows.length ? rows : [["—","—","—","—","—"]]) {
      const tr = body.insertRow();
      for (const value of row) tr.insertCell().textContent = value;
    }
  }
  updateMapping();
}

function updateMapping() {
  const data = JSON.stringify(state.mapping || {}, null, 2);
  el("mapping").hidden = data === "{}";
  if (data !== mapDescription) {
    mapDescription = data;
    el("mapping-text").textContent = data;
  }
}

async function refresh() {
  const select = el("adapters");
  try {
    const response = await fetch("/api/adapters");
    const data = await response.json();
    if (!response.ok) throw Error(data.detail);
    select.replaceChildren();
    const target = data.items.find(item => item.target);
    if (target) {
      const label = target.name + " · " + target.speed + (target.status === "Up" ? "" : " · 未连接");
      select.add(new Option(label, target.status === "Up" ? target.interface || "" : ""));
    } else select.add(new Option("未发现 EtherCAT 网卡", ""));
    if (data.error) showMessage(data.error, true);
  } catch (error) { showMessage(error.message, true); }
  render();
}

async function command(action, args={}) {
  if (!token || pending) return;
  pending = true;
  render();
  try {
    const response = await fetch("/api/commands", {method:"POST",
      headers:{"content-type":"application/json","x-viewer-session":token},
      body:JSON.stringify({id:String(++sequence), action, args})});
    const data = await response.json();
    if (!response.ok || !data.ok) throw Error(data.error || data.detail || "命令失败");
    if (data.result) state = {...state,...data.result};
    showMessage(action === "zero" ? "清零指令已处理，输出已锁定" : "");
  } catch (error) { showMessage(String(error.message), true); }
  finally { pending = false; render(); }
}

el("refresh").onclick = refresh;
el("adapters").onchange = render;
el("claim").onclick = () => command("claim");
el("connect").onclick = () => command("connect",{interface:el("adapters").value});
for (const action of ["disconnect","simulate","arm","zero"]) el(action).onclick = () => command(action);
el("record").onclick = () => command(state.recording ? "record_stop" : "record_start");

function connectWebSocket() {
  socket = new WebSocket("ws://" + location.host + "/ws");
  socket.onmessage = event => {
    lastSocketFrame = performance.now();
    const packet = JSON.parse(event.data);
    if (packet.kind === "hello") token = packet.token;
    connected = true;
    const modeChanged = state.mode !== packet.status.mode;
    if (modeChanged || packet.kind === "hello") {
      history = [];
      last = origin = null;
    }
    state = packet.status;
    const data = packet.samples || [];
    if (data.length) {
      origin ??= data[0].time_ns;
      history.push(...data);
      last = data.at(-1);
      history = history.filter(sample => last.time_ns-sample.time_ns <= 30e9);
      const x = [];
      const y = [[],[],[],[]];
      let previous = null;
      for (const sample of history) {
        if (previous && sample.seq !== previous.seq + 1) {
          x.push((previous.time_ns + (sample.time_ns-previous.time_ns)/2-origin)/1e9);
          for (const values of y) values.push(null);
        }
        x.push((sample.time_ns-origin)/1e9);
        for (let i=0;i<4;i++) y[i].push(sample.ai[i]);
        previous = sample;
      }
      plot.setData([x,...y]);
    }
    if (packet.kind === "hello" && !el("adapters").value && state.connection !== "OP") refresh();
    render();
  };
  socket.onclose = () => {
    connected = false;
    token = null;
    render();
    setTimeout(connectWebSocket, 1000);
  };
}
setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    if (connected && performance.now()-lastSocketFrame > 3000) {
      socket.close();
    } else if (state.controller) {
      socket.send("heartbeat");
    }
  }
},500);
render();
connectWebSocket();
