"use strict";
class BridgePanel {
  constructor(post) {
    this.post = post;
    this.draft = null;
    this.dirty = false;
    this.addresses = ["127.0.0.1"];
    this.channels = {ai: [], ao: []};
    this.state = {};
    document.getElementById("bridge-legacy").onclick = () => this.preset("legacy");
    document.getElementById("bridge-extended").onclick = () => this.preset("extended");
    document.getElementById("bridge-verify").onclick = () => {
      if (this.draft && this.state.fingerprint) {
        this.draft.fingerprint = this.state.fingerprint;
        this.dirty = true;
        this.drawConfig();
      }
    };
    document.getElementById("bridge-add").onclick = () => this.add();
    document.getElementById("bridge-save").onclick = async () => {
      const response = await this.post("bridge_save", {config: this.draft});
      if (response?.ok) {
        this.dirty = false;
        this.draft = structuredClone(response.config);
        this.drawConfig();
      }
    };
    document.getElementById("bridge-start").onclick = () => this.post("bridge_start");
    document.getElementById("bridge-stop").onclick = () => this.post("bridge_stop");
    document.getElementById("bridge-reclaim").onclick = () => this.post("zero");
    fetch("/api/bridge/network").then(r => r.json()).then(data => {
      this.addresses = data.addresses || this.addresses;
      this.drawConfig();
    }).catch(() => {});
  }
  async preset(profile) {
    const response = await this.post("bridge_preset", {profile});
    if (response?.ok) {
      this.draft = structuredClone(response.config);
      this.dirty = true;
      this.drawConfig();
    }
  }
  add() {
    if (!this.draft) return;
    const port = Math.max(5999, ...this.draft.cards.map(c => c.port)) + 1;
    this.draft.cards.push({id: crypto.randomUUID().replaceAll("-", "").slice(0, 12),
      name: "虚拟卡 " + (this.draft.cards.length + 1), enabled: true, profile: "legacy",
      host: "127.0.0.1", port, allow: ["127.0.0.1/32"], ai: Array(8).fill(null), ao: Array(2).fill(null),
      ao_range: "original", sampling: "hold", send_ms: 50, backlog_ms: 2000, saved_range: 2, saved_divider: 65000});
    this.dirty = true;
    this.drawConfig();
  }
  field(parent, label, control) {
    const box = document.createElement("label");
    box.append(document.createTextNode(label), control);
    parent.append(box);
    return control;
  }
  select(options, value, change) {
    const select = document.createElement("select");
    for (const [key, label] of options) select.add(new Option(label, key));
    if (value != null && value !== "" && !options.some(([key]) => key === value)) select.add(new Option("已失效：" + value, value));
    select.value = value ?? "";
    select.onchange = () => { this.dirty = true; change(select.value); };
    return select;
  }
  input(value, type, change, min, max) {
    const input = document.createElement("input");
    input.type = type;
    input.value = value;
    if (min != null) input.min = min;
    if (max != null) input.max = max;
    input.onchange = () => { this.dirty = true; change(type === "number" ? input.valueAsNumber : input.value); };
    return input;
  }
  drawConfig() {
    const parent = document.getElementById("bridge-config");
    parent.replaceChildren();
    if (!this.draft) return;
    for (const card of this.draft.cards) {
      const details = document.createElement("details");
      details.className = "virtual-card";
      const summary = document.createElement("summary");
      summary.textContent = card.name + " · " + (card.profile === "legacy" ? "中瑞原版" : "扩展 v1") +
        " · " + card.ai.length + " AI / " + card.ao.length + " AO · " + card.host + ":" + card.port;
      details.append(summary);
      const fields = document.createElement("fieldset");
      fields.className = "bridge-fields";
      const group = document.createElement("div");
      group.className = "bridge-grid";
      this.field(group, "名称", this.input(card.name, "text", v => card.name = v));
      this.field(group, "启用", this.select([["yes", "启用"], ["no", "停用"]], card.enabled ? "yes" : "no", v => card.enabled = v === "yes"));
      this.field(group, "协议", this.select([["legacy", "中瑞原版（8 AI / 2 AO）"], ["extended", "扩展 v1（可变通道）"]],
        card.profile, v => {
          card.profile = v;
          if (v === "legacy") {
            card.ai = [...card.ai, ...Array(8).fill(null)].slice(0, 8);
            card.ao = [...card.ao, null, null].slice(0, 2);
          }
          this.drawConfig();
        }));
      this.field(group, "监听地址", this.select(this.addresses.map(a => [a, a]), card.host, v => card.host = v));
      this.field(group, "TCP 端口", this.input(card.port, "number", v => card.port = v, 1024, 65535));
      this.field(group, "允许访问的 IPv4 / 网段，逗号分隔", this.input(card.allow.join(", "), "text",
        v => card.allow = v.split(",").map(s => s.trim()).filter(Boolean)));
      this.field(group, "AO 量程策略（原版协议）", this.select([["original", "保持 0–3.3 V"], ["bipolar", "0–3.3 V → −10 至 +10 V"]],
        card.ao_range, v => { card.ao_range = v; this.drawConfig(); }));
      this.field(group, "采样策略", this.select([["hold", "兼容保持：允许补点"], ["native", "真实采样：不补点"]],
        card.sampling, v => card.sampling = v));
      this.field(group, "发送批量 / ms", this.input(card.send_ms, "number", v => card.send_ms = v, 5, 1000));
      this.field(group, "最大积压 / ms", this.input(card.backlog_ms, "number", v => card.backlog_ms = v, 100, 10000));
      if (card.profile === "extended") {
        for (const kind of ["ai", "ao"]) this.field(group, kind.toUpperCase() + " 数量",
          this.input(card[kind].length, "number", v => {
            if (!Number.isInteger(v) || v < (kind === "ai" ? 1 : 0) || v > 65535) return;
            card[kind] = Array.from({length: v}, (_, i) => card[kind][i] ?? null);
            this.drawConfig();
          }, kind === "ai" ? 1 : 0, 65535));
      }
      fields.append(group);
      const note = document.createElement("p");
      note.className = card.profile === "legacy" && card.ao_range === "bipolar" ? "mapping-warning" : "muted";
      note.textContent = card.profile === "extended" ? "扩展输出直接使用物理电压，支持 ±10 V。" :
        card.ao_range === "bipolar" ? "双极性映射：码值 0 → −10 V；2048 → 约 +2.44 mV；4095 → +10 V。软件发送码值 0 会到 −10 V。清零与断线始终回到物理 0 V。" :
        "原版 DAC 0–4095 对应物理 0–3.3 V。通道选择值 3 仍表示两路同时设置。";
      fields.append(note);
      const maps = document.createElement("div");
      maps.className = "bridge-mappings";
      for (const kind of ["ai", "ao"]) {
        const column = document.createElement("div");
        const h = document.createElement("h4");
        h.textContent = kind.toUpperCase() + " 通道映射";
        column.append(h);
        card[kind].forEach((value, i) => {
          this.field(column, kind.toUpperCase() + (kind === "ai" ? i : i + 1),
            this.select([["", kind === "ai" ? "未接入（固定零值）" : "未映射（拒绝输出）"],
              ...this.channels[kind].map(c => [c.id, c.label])], value, v => card[kind][i] = v || null));
        });
        maps.append(column);
      }
      fields.append(maps);
      const remove = document.createElement("button");
      remove.textContent = "删除这张虚拟卡";
      remove.type = "button";
      remove.onclick = () => {
        this.draft.cards = this.draft.cards.filter(c => c !== card);
        this.dirty = true;
        this.drawConfig();
      };
      fields.append(remove);
      details.append(fields);
      parent.append(details);
    }
    this.renderControls();
  }
  renderControls() {
    const permitted = this.state.controller && !this.state.pending;
    const active = this.state.bridge?.active;
    for (const button of document.querySelectorAll("#bridge-runtime button[data-card]")) {
      const card = (this.state.bridge?.cards || []).find(c => c.id === button.dataset.card);
      button.disabled = !permitted || !active || !card || !!card.controller || card.authorized;
    }
    for (const fieldset of document.querySelectorAll("#bridge-config fieldset")) fieldset.disabled = !permitted || active;
    for (const id of ["bridge-legacy", "bridge-extended"]) document.getElementById(id).disabled =
      !permitted || active || this.state.connection !== "OP";
    document.getElementById("bridge-verify").disabled = !permitted || active || !this.draft || !this.state.fingerprint;
    document.getElementById("bridge-config-state").textContent = this.dirty ? "配置尚未保存，请核对映射后保存。" : this.draft?.fingerprint && this.state.fingerprint && this.draft.fingerprint !== this.state.fingerprint ? "设备拓扑已变化，请核对模块和通道，再按当前拓扑重新核对并保存。" : "配置只保存映射与参数；重启后输出锁定，需手动连接和发布。";
    document.getElementById("bridge-add").disabled = !permitted || active || !this.draft?.fingerprint;
    document.getElementById("bridge-save").disabled = !permitted || active || !this.draft?.fingerprint;
    document.getElementById("bridge-start").disabled = !permitted || active || this.dirty || this.state.connection !== "OP";
    document.getElementById("bridge-stop").disabled = !permitted || !active;
    document.getElementById("bridge-reclaim").disabled = !this.state.controller || this.state.connection !== "OP";
  }
  render(state, pending) {
    this.state = {...state, pending};
    const bridge = state.bridge || {};
    const channelKey = JSON.stringify(state.channels || {ai: [], ao: []});
    if (!this.draft || (!this.dirty && this.configKey !== JSON.stringify(bridge.config))) {
      this.draft = bridge.config ? structuredClone(bridge.config) : null;
      this.configKey = JSON.stringify(bridge.config);
      this.channels = state.channels || {ai: [], ao: []};
      this.drawConfig();
    } else if (this.channelKey !== channelKey) {
      this.channels = state.channels || {ai: [], ao: []};
      this.drawConfig();
    }
    this.channelKey = channelKey;
    document.getElementById("bridge-mode").textContent = bridge.active ? "TCP 接管 · 网页只读监视" : "网页调试";
    document.getElementById("bridge-source").textContent = "实际源数据 " +
      (Number.isFinite(bridge.native_hz) ? bridge.native_hz.toFixed(1) + " Hz" : "—") +
      " · 原始批量 50 ms · 时间戳为主机 PDO 交换时刻";
    document.getElementById("bridge-error").textContent = bridge.last_error || "";
    const runtime = document.getElementById("bridge-runtime");
    const runtimeKey = JSON.stringify(bridge.cards);
    if (runtime.dataset.key !== runtimeKey) {
      runtime.dataset.key = runtimeKey;
      runtime.replaceChildren();
      for (const card of bridge.cards || []) {
        const box = document.createElement("article");
        box.className = "bridge-status";
        const h = document.createElement("h3");
        h.textContent = card.name + " · " + card.address;
        const line = document.createElement("p");
        line.textContent = (card.controller ? "控制端 " + card.controller : card.authorized ? "已授权，等待控制端" : "输出锁定") +
          " · 连接 " + card.connections + " · 物理 AO " + card.physical_ao.map(v => Number.isFinite(v) ? v.toFixed(3) + " V" : "—").join(" / ");
        const authorize = document.createElement("button");
        authorize.dataset.card = card.id;
        authorize.textContent = card.authorized ? "已授权" : "授权此卡 TCP 接管";
        authorize.disabled = !state.controller || pending || !bridge.active || !!card.controller || card.authorized;
        authorize.onclick = () => this.post("bridge_authorize", {card: card.id});
        box.append(h, line, authorize);
        const sessions = card.sessions.length ? card.sessions : [card.last_stats];
        for (const stats of sessions) {
          if (!stats || !Object.keys(stats).length) continue;
          const p = document.createElement("p");
          p.className = "muted";
          p.textContent = (stats.peer || "上次连接") + " · " + (stats.controller ? "控制" : "只读") +
            " · 逻辑 " + (stats.rate_hz ?? "—") + " Hz · 源样本 " + stats.sources + " · 保持补点 " + stats.held +
            " · 已发 " + stats.sent_samples + " 点 · 积压 " + (stats.queued_bytes / 1024).toFixed(1) + " KiB" +
            " · 缓冲延迟 " + (stats.latency_ms == null ? "—" : stats.latency_ms.toFixed(1) + " ms");
          box.append(p);
          if (stats.last_output?.length) {
            const output = document.createElement("p");
            output.textContent = stats.last_output.map(o => "AO" + o.channel + " 指令 " +
              (o.software_v == null ? "扩展电压" : o.software_v.toFixed(4) + " V / 码 " + o.code) +
              " → 物理目标 " + o.physical_v.toFixed(4) + " V").join("；");
            box.append(output);
          }
        }
        for (const stats of sessions) for (const wave of stats?.wave_info || []) {
          const line = document.createElement("p");
          line.className = wave.warning ? "mapping-warning" : "muted";
          line.textContent = "AO" + wave.channel + " 表波形 " + wave.frequency_hz.toFixed(3) + " Hz · " + wave.table_points + " 个原表点 · 每周期约 " + (wave.actual_points_per_cycle == null ? "—" : wave.actual_points_per_cycle.toFixed(1)) + " 次实际更新" + (wave.warning ? " · 台阶明显" : "");
          box.append(line);
        }
        if (card.error) {
          const err = document.createElement("p");
          err.className = "mapping-warning";
          err.textContent = card.error;
          box.append(err);
        }
        runtime.append(box);
      }
    }
    document.getElementById("bridge-events").textContent = (bridge.events || []).slice(-8).map(e => e.message).join("\n");
    this.renderControls();
  }
}
