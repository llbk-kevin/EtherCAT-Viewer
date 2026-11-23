"use strict";
class BridgePanel {
  constructor(post,settings,notify){
    this.post=post;this.settings=settings;this.notify=notify;this.state={};this.channels={ai:[],ao:[]};this.addresses=["127.0.0.1"];this.draft=null;this.base=null;this.selected=null;try{this.selected=sessionStorage.getItem("viewer-bridge-card");}catch{}this.pages=new Map();this.quantityDrafts=new Map();this.dirty=false;
    const on=(id,fn)=>document.getElementById(id).onclick=fn;
    on("bridge-reload",()=>{this.draft=structuredClone(this.remote);this.base=structuredClone(this.remote);this.quantityDrafts.clear();this.conflict=false;this.dirty=false;this.draw();});
    on("bridge-verify",()=>{this.draft.fingerprint=this.state.fingerprint;this.changed();});
    on("bridge-save",async()=>{
      if(!this.checkQuantities())return;
      const invalid=document.querySelector("#bridge-config :invalid");if(invalid){invalid.reportValidity();return;}
      const response=await this.post("bridge_save",{config:this.draft,base_config:this.base});
      if(response?.ok){this.remote=structuredClone(response.config);this.base=structuredClone(response.config);this.draft=structuredClone(response.config);this.dirty=false;this.conflict=false;this.draw();}
    });
    on("bridge-start",()=>this.post("bridge_start"));on("bridge-stop",()=>this.post("bridge_stop"));
    fetch("/api/bridge/network").then(r=>r.json()).then(d=>{this.addresses=d.addresses||this.addresses;if(this.draft&&!this.dirty)this.draw();}).catch(()=>{});
  }
  nextPort(profile){const used=new Set(this.draft.cards.map(c=>c.port));let port=profile==="legacy"?6000:7000;while(used.has(port)&&port<65535)port++;return port;}
  changed(){this.dirty=this.quantityDrafts.size>0||!settingSame(this.draft,this.base);this.renderControls();}
  checkQuantities(cardId){const entry=[...this.quantityDrafts.values()].find(d=>!cardId||d.card===cardId);if(!entry)return true;this.selected=entry.card;this.draw();const input=document.querySelector('#bridge-card-panel [data-field="'+entry.kind.toUpperCase()+' 数量"]');input?.focus();input?.reportValidity();return false;}
  revealSelected(){requestAnimationFrame(()=>revealInStrip(document.getElementById("bridge-tabs"),document.getElementById("tab-"+this.selected)));}
  add(profile,mapped){
    if(!this.draft)return;
    const ai=Array(profile==="legacy"?8:Math.max(1,this.channels.ai.length)).fill(null),ao=Array(profile==="legacy"?2:this.channels.ao.length).fill(null);
    const occupied=new Set(this.draft.cards.flatMap(c=>c.ao.filter((v,i)=>v&&(c.ao_writable?.[i]??true)&&c.enabled)));
    if(mapped){ai.forEach((_,i)=>ai[i]=this.channels.ai[i]?.id||null);ao.forEach((_,i)=>ao[i]=this.channels.ao[i]?.id||null);}
    const id=crypto.randomUUID().replaceAll("-","").slice(0,12),port=this.nextPort(profile);
    this.draft.cards.push({id,name:"虚拟卡 "+(this.draft.cards.length+1),enabled:true,profile,host:"127.0.0.1",port,allow:["127.0.0.1/32"],ai,ao,ao_writable:ao.map(v=>!!v&&!occupied.has(v)),auto_reauthorize:true,ao_range:"original",sampling:"hold",send_ms:50,backlog_ms:2000,saved_range:2,saved_divider:65000,start_on_boot:false,authorize_on_boot:false});
    this.selected=id;this.changed();this.draw();
  }
  merge(remote){
    if(!this.base){this.base=structuredClone(remote);this.draft=structuredClone(remote);return true;}
    if(settingSame(remote,this.remote))return false;
    let redraw=false,conflict=false;
    const draft=this.draft,base=this.base;
    for(const key of ["version","fingerprint"]){if(settingSame(draft[key],base[key]))draft[key]=structuredClone(remote[key]);else if(!settingSame(remote[key],base[key])&&!settingSame(remote[key],draft[key]))conflict=true;}
    for(const card of remote.cards){
      const old=base.cards.find(c=>c.id===card.id),local=draft.cards.find(c=>c.id===card.id);
      if(!old&&!local){draft.cards.push(structuredClone(card));redraw=true;continue;}
      if(!old||!local)continue;
      for(const key of Object.keys(card)){
        if(this.quantityDrafts.has(card.id+"/"+key)&&!settingSame(card[key],old[key])){conflict=true;continue;}
        if(settingSame(local[key],old[key])){if(!settingSame(local[key],card[key])&&!["start_on_boot","authorize_on_boot","saved_range","saved_divider"].includes(key))redraw=true;local[key]=structuredClone(card[key]);old[key]=structuredClone(card[key]);}
        else if(!settingSame(card[key],old[key])&&!settingSame(card[key],local[key]))conflict=true;
      }
    }
    for(const old of [...base.cards])if(!remote.cards.some(c=>c.id===old.id)){
      const local=draft.cards.find(c=>c.id===old.id);
      if(settingSame(old,local)&&![...this.quantityDrafts.values()].some(d=>d.card===old.id)){draft.cards=draft.cards.filter(c=>c.id!==old.id);base.cards=base.cards.filter(c=>c.id!==old.id);redraw=true;}else if(local)conflict=true;
    }
    for(const card of remote.cards)if(!base.cards.some(c=>c.id===card.id)&&draft.cards.some(c=>c.id===card.id))base.cards.push(structuredClone(card));
    base.fingerprint=remote.fingerprint;this.conflict=conflict;this.changed();return redraw;
  }
  field(parent,label,control,help){const box=document.createElement("label");box.className="bridge-field";const title=document.createElement("span");title.textContent=label;if(help)title.append(" ",infoButton(help));control.dataset.field=label;control.setAttribute("aria-label",label);box.append(title,control);parent.append(box);return control;}
  select(options,value,change){const node=document.createElement("select");for(const[k,v]of options)node.add(new Option(v,k));if(value!=null&&value!==""&&!options.some(([k])=>k===value))node.add(new Option("已失效："+value,value));node.value=value??"";node.onchange=()=>{change(node.value);this.changed();};return node;}
  input(value,type,change,min,max){const n=document.createElement("input");n.type=type;n.value=value;if(min!=null)n.min=min;if(max!=null)n.max=max;if(type==="number"){n.required=true;n.step="1";}n.oninput=()=>{change(type==="number"?n.valueAsNumber:n.value);this.changed();};return n;}
  draw(){
    if(!this.draft)return;const cards=this.draft.cards;if(!cards.some(c=>c.id===this.selected))this.selected=cards[0]?.id||null;try{if(this.selected)sessionStorage.setItem("viewer-bridge-card",this.selected);else sessionStorage.removeItem("viewer-bridge-card");}catch{}
    const tabs=document.getElementById("bridge-tabs");tabs.replaceChildren();
    for(const card of cards){const tab=document.createElement("button");tab.type="button";tab.role="tab";tab.className="virtual-card-tab";tab.dataset.card=card.id;tab.id="tab-"+card.id;tab.setAttribute("aria-selected",String(card.id===this.selected));tab.setAttribute("aria-controls","bridge-card-panel");tab.tabIndex=card.id===this.selected?0:-1;
      const image=document.createElement("img");image.src="/static/images/modules/e1682.png";image.alt="中瑞 E1682 产品图";const name=document.createElement("strong");name.className="virtual-card-name";name.textContent=card.name;const profile=document.createElement("span");profile.className="virtual-card-protocol";profile.textContent=(card.profile==="legacy"?"原版":"扩展 v1")+" · "+card.ai.length+" AI / "+card.ao.length+" AO";tab.append(image,name,profile);
      tab.onclick=()=>{this.selected=card.id;this.draw();document.getElementById("tab-"+card.id)?.focus();};tabs.append(tab);
    }
    const add=document.createElement("button");add.id="bridge-add";add.type="button";add.className="add-card";add.textContent="＋";add.setAttribute("aria-label","添加原版虚拟卡");add.title="添加原版虚拟卡，可在卡内修改协议";add.onclick=()=>this.add("legacy",false);tabs.append(add);wireTabKeys(tabs);this.revealSelected();
    const parent=document.getElementById("bridge-config");parent.replaceChildren();
    const card=cards.find(c=>c.id===this.selected);
    if(!card){const empty=document.createElement("div");empty.className="panel empty-state";empty.textContent="还没有虚拟卡。添加一张卡后配置通道和监听地址。";parent.append(empty);this.runtimeKey="";this.drawRuntime();this.renderControls();return;}
    const box=document.createElement("article");box.className="panel virtual-card";box.id="bridge-card-panel";box.role="tabpanel";box.setAttribute("aria-labelledby","tab-"+card.id);box.dataset.card=card.id;
    const fields=document.createElement("fieldset");fields.className="bridge-fields";fields.dataset.card=card.id;
    const group=document.createElement("div");group.className="form-grid";
    this.field(group,"名称",this.input(card.name,"text",v=>{card.name=v;const tab=document.getElementById("tab-"+card.id);if(tab)tab.querySelector(".virtual-card-name").textContent=v;}));
    this.field(group,"启用本卡",this.select([["yes","启用"],["no","停用"]],card.enabled?"yes":"no",v=>card.enabled=v==="yes"));
    this.field(group,"协议类型",this.select([["legacy","原版 8 AI＋2 AO"],["extended","扩展 v1 · 可变通道"]],card.profile,v=>{
      if([...this.quantityDrafts.values()].some(d=>d.card===card.id)){for(const[key,d]of this.quantityDrafts)if(d.card===card.id)this.quantityDrafts.delete(key);this.notify("协议类型已改变，已清除本卡尚未应用的通道数量输入");}
      if(v==="legacy"){const lost=card.ai.slice(8).filter(Boolean).length+card.ao.slice(2).filter(Boolean).length;card.ai=Array.from({length:8},(_,i)=>card.ai[i]??null);card.ao=Array.from({length:2},(_,i)=>card.ao[i]??null);card.ao_writable=Array.from({length:2},(_,i)=>card.ao_writable[i]??false);if(lost)this.notify("缩减为原版将移除 "+lost+" 个映射；保存前请核对");}
      card.profile=v;this.changed();this.draw();
    }));
    this.field(group,"监听地址",this.select(this.addresses.map(a=>[a,a]),card.host,v=>card.host=v));
    this.field(group,"TCP 端口",this.input(card.port,"number",v=>card.port=v,1024,65535));
    this.field(group,"允许 IPv4 / 网段",this.input(card.allow.join(", "),"text",v=>card.allow=v.split(",").map(s=>s.trim()).filter(Boolean)),"多个地址用逗号分隔；默认仅允许本机。");
    this.field(group,"客户端自动重新接管",this.select([["yes","允许 · 先归零"],["no","等待再次授权"]],card.auto_reauthorize?"yes":"no",v=>card.auto_reauthorize=v==="yes"));
    this.field(group,"原版 AO 量程",this.select([["original","中瑞 0–3.3 V"],["bipolar","映射 −10 至 +10 V"]],card.ao_range,v=>card.ao_range=v),"原版码值 0 在双极性映射下是 −10 V；人工清零始终请求物理 0 V。扩展输出直接使用物理电压。");
    this.field(group,"采样策略",this.select([["hold","兼容保持 · 补点"],["native","真实采样 · 不补点"]],card.sampling,v=>card.sampling=v),"保持补点不会增加实际测量细节。采样能力与有效源速率见本卡运行详情。");
    this.field(group,"发送批量 / ms",this.input(card.send_ms,"number",v=>card.send_ms=v,5,1000));
    this.field(group,"积压上限 / ms",this.input(card.backlog_ms,"number",v=>card.backlog_ms=v,100,10000));
    if(card.profile==="extended")for(const kind of["ai","ao"]){
      const key=card.id+"/"+kind,pending=this.quantityDrafts.get(key),input=this.input(pending?.text??card[kind].length,"number",()=>{},kind==="ai"?1:0,65535),error=document.createElement("span");error.className="field-error";error.setAttribute("role","status");
      const validate=()=>{const valid=input.checkValidity();error.textContent=valid?"":kind.toUpperCase()+" 数量必须为 "+(kind==="ai"?"1":"0")+"–65535 的整数，输入已保留。";input.setAttribute("aria-invalid",String(!valid));return valid;};
      input.oninput=()=>{this.quantityDrafts.set(key,{card:card.id,kind,text:input.value});validate();this.changed();};
      input.onchange=()=>{const v=input.valueAsNumber;if(!validate())return;const removed=card[kind].slice(v).filter(Boolean).length;card[kind]=Array.from({length:v},(_,i)=>card[kind][i]??null);if(kind==="ao")card.ao_writable=Array.from({length:v},(_,i)=>card.ao_writable[i]??false);this.quantityDrafts.delete(key);if(removed)this.notify("缩减通道会移除 "+removed+" 个映射；保存前请核对");this.changed();this.draw();};
      this.field(group,kind.toUpperCase()+" 数量",input,"数量还受 1 MiB 单帧与发送资源边界限制。");input.parentElement.append(error);validate();
    }
    fields.append(group);
    const maps=document.createElement("div");maps.className="bridge-mappings";
    for(const kind of["ai","ao"]){const col=document.createElement("div"),h=document.createElement("h4");h.textContent=kind.toUpperCase()+" 通道映射";col.append(h);
      const key=card.id+kind,pages=Math.max(1,Math.ceil(card[kind].length/32)),page=Math.min(this.pages.get(key)||0,pages-1);
      if(pages>1)col.append(this.select(Array.from({length:pages},(_,i)=>[String(i),"第 "+(i+1)+" / "+pages+" 页"]),String(page),v=>{this.pages.set(key,+v);this.draw();}));
      card[kind].slice(page*32,page*32+32).forEach((value,j)=>{const i=page*32+j,row=document.createElement("div");row.className="map-row";this.field(row,kind.toUpperCase()+(kind==="ai"?i:i+1),this.select([["",kind==="ai"?"未接入 · 固定零":"未映射"],...this.channels[kind].map(c=>[c.id,c.label])],value,v=>card[kind][i]=v||null));
        if(kind==="ao")this.field(row,"权限",this.select([["read","只读"],["write","唯一写入者"]],card.ao_writable[i]?"write":"read",v=>card.ao_writable[i]=v==="write")).setAttribute("aria-label","AO"+(i+1)+" 权限");col.append(row);
      });maps.append(col);
    }fields.append(maps);
    const actions=document.createElement("div");actions.className="group";
    const copy=document.createElement("button");copy.type="button";copy.textContent="复制为只读卡";copy.onclick=()=>{if(!this.checkQuantities(card.id))return;const c=structuredClone(card);c.id=crypto.randomUUID().replaceAll("-","").slice(0,12);c.name+=" 副本";c.port=this.nextPort(c.profile);c.ao_writable=c.ao.map(()=>false);c.start_on_boot=false;c.authorize_on_boot=false;this.draft.cards.push(c);this.selected=c.id;this.changed();this.draw();};
    const remove=document.createElement("button");remove.type="button";remove.className="danger";remove.textContent="删除本卡";remove.onclick=()=>{const index=this.draft.cards.indexOf(card);this.draft.cards.splice(index,1);for(const[key,d]of this.quantityDrafts)if(d.card===card.id)this.quantityDrafts.delete(key);this.selected=this.draft.cards[Math.min(index,this.draft.cards.length-1)]?.id||null;this.notify("将删除 "+card.name+" 及其 "+card.ai.filter(Boolean).length+" 个 AI、"+card.ao.filter(Boolean).length+" 个 AO 映射；保存前可放弃草稿");this.changed();this.draw();};
    actions.append(copy,remove);fields.append(actions);box.append(fields);
    const boot=document.createElement("div");boot.className="card-boot";boot.setAttribute("role","group");boot.setAttribute("aria-label","本卡下次启动选项");box.append(boot);
    if(this.settings.config?.bridge.cards.some(c=>c.id===card.id)){
      for(const[key,label]of[["start_on_boot","启动时监听本卡"],["authorize_on_boot","启动时允许 TCP 写入"]])this.settings.field(boot,["bridge","cards",card.id,key],label,"checkbox",{instant:true,help:"拨动即保存，下次冷启动生效。连接后先确认零值，不恢复旧输出。"});
    }else{const p=document.createElement("p");p.className="muted";p.textContent="保存本卡后可设置逐卡冷启动选项。";boot.append(p);}
    parent.append(box);this.runtimeKey="";this.drawRuntime();this.renderControls();this.settings.paint();
  }
  drawRuntime(){
    const runtime=document.getElementById("bridge-runtime"),card=this.state.bridge?.cards?.find(c=>c.id===this.selected),key=card?.id||"offline-"+this.selected;
    if(this.runtimeKey!==key){this.runtimeKey=key;runtime.replaceChildren();const box=document.createElement("article");box.className="panel bridge-status";box.innerHTML='<div class="section-head"><h3>本卡运行状态</h3><div class="group"><button class="start-card">开始监听本卡</button><button class="authorize">允许 TCP 控制本卡输出</button><button class="stop-card">停止本卡并清零</button></div></div><p class="owner-line"></p><p class="card-error warning"></p><details><summary>采样与发送详情</summary><p class="source-line muted"></p><p class="stats-line muted"></p></details>';box.querySelector(".start-card").onclick=()=>this.post("bridge_start",{card:this.selected});box.querySelector(".authorize").onclick=()=>this.post("bridge_authorize",{card:this.selected});box.querySelector(".stop-card").onclick=()=>this.post("bridge_stop",{card:this.selected});runtime.append(box);}
    const box=runtime.firstElementChild;if(!box)return;box.hidden=!this.selected;const saved=this.remote?.cards.find(c=>c.id===this.selected),writable=!!saved?.ao.some((id,i)=>id&&saved.ao_writable[i]);
    box.querySelector(".owner-line").textContent=card?(card.address+" · "+(card.controller?(writable?"控制端 ":"采集端 ")+card.controller:!writable?"仅采集 · 无可写 AO":card.authorized?"已授权，等待客户端":"输出未授权")+" · "+card.connections+" 个连接 · AO "+card.physical_ao.map(v=>Number.isFinite(v)?v.toFixed(3)+" V":"—").join(" / ")):"本卡尚未监听";
    box.querySelector(".card-error").textContent=card?.error||"";
    box.querySelector(".source-line").textContent="有效采集源 "+(Number.isFinite(this.state.bridge?.native_hz)?this.state.bridge.native_hz.toFixed(1)+" Hz":"—")+"；补点重复上一有效电压，不代表新增采样。";
    box.querySelector(".stats-line").textContent=(card?.sessions||[]).map(s=>(s.controller?(writable?"控制":"采集"):"只读")+" "+s.peer+" · "+(s.rate_hz??"—")+" 逻辑 Hz · 有效源 "+s.sources+" · 补点 "+s.held+" · 积压 "+s.queued_bytes+" B · 延迟 "+(s.latency_ms?.toFixed(1)??"—")+" ms").join("；")||"无客户端采集任务";
    const ok=this.state.controller&&!this.pending;box.querySelector(".start-card").disabled=!ok||!this.state.ready||this.dirty||!!card||!this.draft?.cards.find(c=>c.id===this.selected)?.enabled;box.querySelector(".authorize").textContent=writable?"允许 TCP 控制本卡输出":"无需 AO 写入授权";box.querySelector(".authorize").disabled=!ok||!this.state.ready||!card||!writable||!!card.controller||card.authorized;box.querySelector(".stop-card").disabled=!ok||!card;
  }
  renderControls(){
    const permitted=this.state.controller&&!this.pending,active=new Set((this.state.bridge?.cards||[]).map(c=>c.id));
    for(const f of document.querySelectorAll("#bridge-config fieldset")){f.disabled=false;const locked=!permitted||active.has(f.dataset.card);for(const n of f.querySelectorAll("input,select,button:not(.info)"))n.disabled=locked;}
    for(const id of["bridge-add","bridge-reload"]){const button=document.getElementById(id);if(button)button.disabled=!permitted;}
    document.getElementById("bridge-verify").disabled=!permitted||!this.state.fingerprint||!!this.state.bridge?.active;
    document.getElementById("bridge-save").disabled=!permitted||!this.draft||!this.dirty||this.conflict;
    document.getElementById("bridge-start").disabled=!permitted||!this.state.ready||this.dirty||!this.draft?.cards.some(c=>c.enabled&&!active.has(c.id));
    document.getElementById("bridge-stop").disabled=!permitted||!this.state.bridge?.active;
    const label=this.conflict?"后台同时修改了卡配置，草稿保留；请放弃草稿后重新核对。":this.dirty?"卡配置有未保存修改":this.draft?.fingerprint&&this.state.fingerprint&&this.draft.fingerprint!==this.state.fingerprint?"拓扑已改变，请核对映射":"";
    document.getElementById("bridge-config-state").textContent=label;
  }
  render(state,pending,config){
    this.state=state;this.pending=pending;this.channels=state.fingerprint?state.channels:(config?.topology?.channels||this.channels);
    const remote=state.bridge?.config||config?.bridge;if(remote){const redraw=this.merge(remote);this.remote=structuredClone(remote);if(redraw&&document.getElementById("bridge-config").contains(document.activeElement))this.redrawPending=true;else if(redraw||!document.querySelector("#bridge-config > *"))this.draw();}
    if(this.redrawPending&&!document.getElementById("bridge-config").contains(document.activeElement)){this.redrawPending=false;this.draw();}
    const saved=!!this.settings.config?.bridge.cards.find(c=>c.id===this.selected),bootKey="bridge/cards/"+this.selected+"/start_on_boot";
    if(saved&&!this.settings.controls.get(bootKey)?.row.isConnected)this.draw();
    this.drawRuntime();this.renderControls();
  }
}
