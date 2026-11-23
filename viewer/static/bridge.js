"use strict";
class BridgePanel {
  constructor(post) {
    this.post = post; this.draft = null; this.dirty = false; this.state = {}; this.channels = {ai:[],ao:[]};
    this.addresses = ["127.0.0.1"]; this.open = new Set(); this.pages = new Map(); this.runtimeKey = "";
    document.getElementById("bridge-reload").onclick=()=>{this.dirty=false;this.draft=null;this.configKey="";this.render(this.state,this.pending);};
    document.getElementById("bridge-add").onclick = () => this.add("legacy", false);
    document.getElementById("bridge-template").onclick = () => this.add(document.getElementById("bridge-template-type").value, true);
    document.getElementById("bridge-verify").onclick = () => {
      if (!this.state.fingerprint) return;
      this.draft.fingerprint = this.state.fingerprint; this.dirty = true; this.drawConfig();
    };
    document.getElementById("bridge-save").onclick = async () => {
      const response = await this.post("bridge_save", {config:this.draft,revision:this.revision});
      if (response?.ok) {this.draft = structuredClone(response.config);this.dirty = false;this.configKey = JSON.stringify(response.config);this.revision = response.result.config_revision;this.drawConfig();}
    };
    document.getElementById("bridge-start").onclick = () => this.post("bridge_start");
    document.getElementById("bridge-stop").onclick = () => this.post("bridge_stop");
    fetch("/api/bridge/network").then(r=>r.json()).then(d=>{this.addresses=d.addresses||this.addresses;if(!this.dirty)this.drawConfig();}).catch(()=>{});
  }
  nextPort(profile) {
    const used = new Set(this.draft.cards.map(c=>c.port));let port=profile==="legacy"?6000:7000;
    while(used.has(port)&&port<65535)port++;return port;
  }
  add(profile, mapped) {
    this.draft ??= {version:1,fingerprint:this.state.fingerprint||"",cards:[]};
    const ai = profile==="legacy"?Array(8).fill(null):Array(Math.max(1,this.channels.ai.length)).fill(null);
    const ao = profile==="legacy"?Array(2).fill(null):Array(this.channels.ao.length).fill(null);
    const occupied = new Set(this.draft.cards.flatMap(c=>c.ao.filter((v,i)=>v&&(c.ao_writable?.[i]??true)&&c.enabled)));
    if(mapped){ai.forEach((_,i)=>ai[i]=this.channels.ai[i]?.id||null);ao.forEach((_,i)=>ao[i]=this.channels.ao[i]?.id||null);}
    const id=crypto.randomUUID().replaceAll("-","").slice(0,12);
    this.draft.cards.push({id,name:"虚拟卡 "+(this.draft.cards.length+1),enabled:true,profile,host:"127.0.0.1",port:this.nextPort(profile),allow:["127.0.0.1/32"],
      ai,ao,ao_writable:ao.map(v=>!!v&&!occupied.has(v)),auto_reauthorize:true,ao_range:"original",sampling:"hold",send_ms:50,backlog_ms:2000,saved_range:2,saved_divider:65000});
    this.open.add(id);this.dirty=true;this.drawConfig();
  }
  field(parent,label,control){const box=document.createElement("label");control.dataset.field=label;box.append(document.createTextNode(label),control);parent.append(box);return control;}
  select(options,value,change){const node=document.createElement("select");for(const[k,v]of options)node.add(new Option(v,k));if(value!=null&&value!==""&&!options.some(([k])=>k===value))node.add(new Option("已失效："+value,value));node.value=value??"";node.onchange=()=>{this.dirty=true;change(node.value);this.renderControls();};return node;}
  input(value,type,change,min,max){const node=document.createElement("input");node.type=type;node.value=value;if(min!=null)node.min=min;if(max!=null)node.max=max;node.onchange=()=>{this.dirty=true;change(type==="number"?node.valueAsNumber:node.value);this.renderControls();};return node;}
  note(message){document.getElementById("bridge-error").textContent=message;}
  drawConfig(){
    const parent=document.getElementById("bridge-config");for(const node of parent.children)if(node.open)this.open.add(node.dataset.id);else this.open.delete(node.dataset.id);
    parent.replaceChildren();if(!this.draft)return;
    for(const card of this.draft.cards){
      card.ao_writable??=card.ao.map(()=>true);card.auto_reauthorize??=true;
      const details=document.createElement("details");details.dataset.id=card.id;details.className="virtual-card panel";details.open=this.open.has(card.id);
      const summary=document.createElement("summary");summary.textContent=card.name+" · "+(card.profile==="legacy"?"原版":"扩展 v1")+" · "+card.ai.length+" AI / "+card.ao.length+" AO · "+card.host+":"+card.port;details.append(summary);
      const fields=document.createElement("fieldset");fields.className="bridge-fields";fields.dataset.card=card.id;
      const group=document.createElement("div");group.className="form-grid";
      this.field(group,"名称",this.input(card.name,"text",v=>card.name=v));
      this.field(group,"启用",this.select([["yes","启用"],["no","停用"]],card.enabled?"yes":"no",v=>card.enabled=v==="yes"));
      this.field(group,"协议类型",this.select([["legacy","原版 8 AI＋2 AO"],["extended","扩展 v1 · 可变通道"]],card.profile,v=>{
        const lost=card.ai.slice(8).filter(Boolean).length+card.ao.slice(2).filter(Boolean).length;
        if(v==="legacy"){card.ai=Array.from({length:8},(_,i)=>card.ai[i]??null);card.ao=Array.from({length:2},(_,i)=>card.ao[i]??null);card.ao_writable=Array.from({length:2},(_,i)=>card.ao_writable[i]??false);}
        card.profile=v;this.note(lost?"切换为原版后移除 "+lost+" 个超出通道范围的映射；保存前可重新核对。":"协议类型已修改，原有范围内映射已保留。");this.drawConfig();
      }));
      this.field(group,"监听地址",this.select(this.addresses.map(a=>[a,a]),card.host,v=>card.host=v));
      this.field(group,"TCP 端口",this.input(card.port,"number",v=>card.port=v,1024,65535));
      this.field(group,"允许 IPv4 / 网段（逗号分隔）",this.input(card.allow.join(", "),"text",v=>card.allow=v.split(",").map(s=>s.trim()).filter(Boolean)));
      this.field(group,"断开后允许自动重新接管",this.select([["yes","允许 · 先归零再接收新指令"],["no","等待网页再次授权"]],card.auto_reauthorize?"yes":"no",v=>card.auto_reauthorize=v==="yes"));
      this.field(group,"原版 AO 量程",this.select([["original","保持中瑞 0–3.3 V"],["bipolar","映射到 −10 至 +10 V"]],card.ao_range,v=>{card.ao_range=v;}));
      this.field(group,"采样策略",this.select([["hold","兼容保持 · 可补点"],["native","真实采样 · 不补点"]],card.sampling,v=>card.sampling=v));
      this.field(group,"发送批量 / ms",this.input(card.send_ms,"number",v=>card.send_ms=v,5,1000));
      this.field(group,"积压上限 / ms",this.input(card.backlog_ms,"number",v=>card.backlog_ms=v,100,10000));
      if(card.profile==="extended")for(const kind of["ai","ao"])this.field(group,kind.toUpperCase()+" 数量",this.input(card[kind].length,"number",v=>{
        if(!Number.isInteger(v)||v<(kind==="ai"?1:0)||v>65535){this.note("通道数量无效");return;}
        const removed=card[kind].slice(v).filter(Boolean).length;card[kind]=Array.from({length:v},(_,i)=>card[kind][i]??null);
        if(kind==="ao")card.ao_writable=Array.from({length:v},(_,i)=>card.ao_writable[i]??false);
        this.note(removed?"缩减将移除 "+removed+" 个映射，请核对后保存。":"通道数量已调整");this.drawConfig();
      },kind==="ai"?1:0,65535));
      fields.append(group);
      const note=document.createElement("p");note.className="notice";note.textContent=card.profile==="legacy"?"原版固定 8 AI＋2 AO，选择值 3 表示两路同时操作。双极性映射中码值 0 是 −10 V；人工清零直接使用物理零目标。":"扩展直接使用物理电压；数量还受 1 MiB 单帧及发送缓冲限制。";fields.append(note);
      const maps=document.createElement("div");maps.className="bridge-mappings";
      for(const kind of["ai","ao"]){const column=document.createElement("div"),h=document.createElement("h4");h.textContent=kind.toUpperCase()+" 映射";column.append(h);
        const key=card.id+kind,pages=Math.max(1,Math.ceil(card[kind].length/32)),page=Math.min(this.pages.get(key)||0,pages-1);
        if(pages>1){const select=this.select(Array.from({length:pages},(_,i)=>[String(i),"第 "+(i+1)+" / "+pages+" 页"]),String(page),v=>{this.pages.set(key,+v);this.drawConfig();});column.append(select);}
        card[kind].slice(page*32,page*32+32).forEach((value,j)=>{const i=page*32+j,row=document.createElement("div");row.className="map-row";
          this.field(row,kind.toUpperCase()+(kind==="ai"?i:i+1),this.select([["",kind==="ai"?"未接入 · 固定零":"未映射"],...this.channels[kind].map(c=>[c.id,c.label])],value,v=>card[kind][i]=v||null));
          if(kind==="ao")this.field(row,"权限",this.select([["read","只读"],["write","唯一写入者"]],card.ao_writable[i]?"write":"read",v=>card.ao_writable[i]=v==="write"));column.append(row);
        });maps.append(column);
      }fields.append(maps);
      const actions=document.createElement("div");actions.className="group";
      const copy=document.createElement("button");copy.type="button";copy.textContent="复制为只读卡";copy.onclick=()=>{const c=structuredClone(card);c.id=crypto.randomUUID().replaceAll("-","").slice(0,12);c.name+=" 副本";c.port=this.nextPort(c.profile);c.ao_writable=c.ao.map(()=>false);this.draft.cards.push(c);this.dirty=true;this.open.add(c.id);this.drawConfig();};
      const remove=document.createElement("button");remove.type="button";remove.className="danger";remove.textContent="删除本卡";remove.onclick=()=>{this.note("将删除 "+card.name+"："+card.ai.filter(Boolean).length+" 个 AI 映射、"+card.ao.filter(Boolean).length+" 个 AO 映射。保存前可读取原配置恢复。");this.draft.cards=this.draft.cards.filter(c=>c!==card);this.dirty=true;this.drawConfig();};actions.append(copy,remove);fields.append(actions);details.append(fields);parent.append(details);
    }this.renderControls();
  }
  renderControls(){const permitted=this.state.controller&&!this.pending,active=new Set((this.state.bridge?.cards||[]).map(c=>c.id));
    for(const f of document.querySelectorAll("#bridge-config fieldset"))f.disabled=!permitted||active.has(f.dataset.card);
    for(const id of["bridge-add","bridge-template"])document.getElementById(id).disabled=!permitted;
    document.getElementById("bridge-verify").disabled=!permitted||!this.state.fingerprint||!!this.state.bridge?.active;
    document.getElementById("bridge-save").disabled=!permitted||!this.draft;
    document.getElementById("bridge-start").disabled=!permitted||!this.state.ready||this.dirty||!this.draft?.cards.some(c=>c.enabled&&!active.has(c.id));
    document.getElementById("bridge-stop").disabled=!permitted||!this.state.bridge?.active;
    document.getElementById("bridge-config-state").textContent=this.dirty?"有未保存修改。保存后才能按新配置监听。":this.draft?.fingerprint&&this.state.fingerprint&&this.draft.fingerprint!==this.state.fingerprint?"拓扑已改变：核对映射后重新确认。":"配置已保存；输出归属与发布状态由后台维护。";
  }
  render(state,pending,config){this.state=state;this.pending=pending;this.channels=state.fingerprint?state.channels:(config?.topology?.channels||this.channels);
    const bridge=state.bridge||{};
    if(!this.draft||!this.dirty&&this.configKey!==JSON.stringify(bridge.config)){this.draft=structuredClone(bridge.config||{version:1,fingerprint:state.fingerprint||"",cards:[]});this.configKey=JSON.stringify(bridge.config);this.revision=state.config_revision;this.drawConfig();}
    if(!this.dirty)this.revision=state.config_revision;
    document.getElementById("bridge-source").textContent="有效 PDO 更新率 "+(Number.isFinite(bridge.native_hz)?bridge.native_hz.toFixed(1)+" Hz":"—")+" · 后台采集独立于网页刷新"+(bridge.suspended?" · 正在等待总线恢复":"");
    if(bridge.last_error)document.getElementById("bridge-error").textContent=bridge.last_error;
    const runtime=document.getElementById("bridge-runtime"),key=JSON.stringify((bridge.cards||[]).map(c=>c.id));
    if(key!==this.runtimeKey){this.runtimeKey=key;runtime.replaceChildren();for(const card of bridge.cards||[]){const box=document.createElement("article");box.className="panel bridge-status";box.dataset.card=card.id;box.innerHTML='<h3></h3><p class="owner-line"></p><p class="stats-line muted"></p><p class="card-error warning"></p><div class="group"><button class="authorize">允许 TCP 控制本卡输出</button><button class="stop-card">停止本卡并清零</button></div>';box.querySelector(".authorize").onclick=()=>this.post("bridge_authorize",{card:card.id});box.querySelector(".stop-card").onclick=()=>this.post("bridge_stop",{card:card.id});runtime.append(box);}}
    for(const card of bridge.cards||[]){const box=[...runtime.children].find(n=>n.dataset.card===card.id);box.querySelector("h3").textContent=card.name+" · "+card.address;
      box.querySelector(".owner-line").textContent=(card.controller?"控制端 "+card.controller:card.authorized?"已授权，等待客户端":"输出未授权")+" · "+card.connections+" 个连接 · AO "+card.physical_ao.map(v=>Number.isFinite(v)?v.toFixed(3)+" V":"—").join(" / ");
      box.querySelector(".stats-line").textContent=(card.sessions||[]).map(s=>(s.controller?"控制":"只读")+" "+s.peer+" · "+(s.rate_hz??"—")+" 逻辑 Hz · 有效源 "+s.sources+" · 补点 "+s.held+" · 积压 "+s.queued_bytes+" B · 延迟 "+(s.latency_ms?.toFixed(1)??"—")+" ms").join("；");
      box.querySelector(".card-error").textContent=card.error||"";box.querySelector(".authorize").disabled=!state.controller||pending||!state.ready||!!card.controller||card.authorized;box.querySelector(".stop-card").disabled=!state.controller||pending;
    }this.renderControls();
  }
}
