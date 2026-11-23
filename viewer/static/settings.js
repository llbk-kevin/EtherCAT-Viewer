"use strict";
const profileDefault={name:"",calibration:{enabled:false,gain:1,offset:0},engineering:{enabled:false,unit:"",voltage1:0,value1:0,voltage2:10,value2:100},measurements:[]};
const filterChoices=[["current","保留设备设置"],["off","关闭"],["fir50","50 Hz FIR"],["fir60","60 Hz FIR"],...Array.from({length:8},(_,i)=>["iir"+(i+1),"IIR "+(i+1)])];
const syncChoices=[["current","保留设备模式"],["freerun","自由运行"],["sm","帧同步"],["dc","DC 分布式时钟"]];
class SettingsPanel{
  constructor(send,notify,managed){this.send=send;this.notify=notify;this.managed=managed;this.store=new DraftStore();this.controls=new Map();this.loading=false;this.state={};this.schema="";this.ready=false;this.config=null;this.module=null;
    this.channelTabs=new Map();this.calibrationMethods=new Map();
    try{this.module=sessionStorage.getItem("viewer-module")||null;}catch{}
    document.getElementById("config-export").onclick=()=>this.export();
    document.getElementById("config-copy").onclick=async()=>{try{await navigator.clipboard.writeText(this.path);this.notify("已复制配置路径");}catch(e){this.notify("复制失败："+e.message,true);}};
    document.getElementById("config-open").onclick=async()=>{try{await this.send("open_config_directory",{});this.notify("已打开配置文件夹");}catch(e){this.notify(e.message,true);}};
    document.getElementById("config-import").onchange=e=>this.importFile(e);document.getElementById("config-import-label").onkeydown=e=>{if((e.key==="Enter"||e.key===" ")&&this.managed()){e.preventDefault();document.getElementById("config-import").click();}};
    document.getElementById("config-import-cancel").onclick=()=>{this.importDraft=null;document.getElementById("import-preview").hidden=true;};
    document.getElementById("config-import-apply").onclick=()=>this.importApply();
    document.getElementById("import-reference").onclick=()=>this.importReference();
  }
  async load({quiet=false}={}){if(this.loadTask)return this.loadTask;this.loading=true;this.loadTask=(async()=>{try{const response=await fetch("/api/config",{signal:AbortSignal.timeout(10000)});if(!response.ok)throw Error("配置读取失败");const data=await response.json();this.accept(data.config);this.capabilities=data.capabilities;this.login=data.login;this.path=data.path;document.getElementById("config-location").textContent=data.path;
      const login=document.getElementById("login-status"),wanted=!!data.config.startup.login;login.textContent=data.login?.enabled&&!data.login.current?"登录启动路径已变化，请重新应用登录启动设置。":data.login?.supported&&wanted!==!!data.login.enabled?"登录启动设置与 Windows 注册结果不一致，请重新核对。":"";login.hidden=!login.textContent;
      if(data.error&&this.configError!==data.error&&!quiet){this.notify(data.error,true);this.configError=data.error;}return data;
    }catch(e){if(!quiet)this.notify(e.message,true);return null;}finally{this.loading=false;this.loadTask=null;}})();return this.loadTask;}
  accept(config,requests){const ids=new Set(config.bridge.cards.map(c=>c.id));for(const[key,e]of this.store.entries)if(e.path[0]==="bridge"&&e.path[1]==="cards"&&!ids.has(e.path[2])){this.store.entries.delete(key);this.controls.delete(key);}if(requests)this.store.accepted(config,requests);else this.store.sync(config);this.config=config;const schema=JSON.stringify(config.topology);if(schema!==this.schema){this.schema=schema;this.build();}this.ready=true;this.paint();}
  definition(path){return this.controls.get(path.join("/"))?.definition;}
  format(value,def){if(value==null)return"未知";if(value==="mixed")return"模块模式不一致";if(def.type==="checkbox")return value?"开启":"关闭";if(def.options)return def.options.find(([v])=>v===String(value))?.[1]||String(value);if(def.type==="policy")return({zero:"归零并锁定",hold:"保持末值",continue:"继续任务",value:"指定 "+value.voltage+" V"})[value.action]||"未知";if(Array.isArray(value))return value.length+" 条";return String(typeof value==="number"?Number(value.toPrecision(12)):value)+(def.unit?" "+def.unit:"");}
  field(parent,path,label,type="number",options={}){
    const key=path.join("/"),fallback=options.fallback??(type==="checkbox"?false:type==="number"?0:"");
    const existing=this.controls.get(key);if(options.instant&&existing?.definition.instant&&!existing.row.isConnected){parent.append(existing.row);this.paintControl(existing);return existing;}
    const entry=this.store.entry(path,fallback),row=document.createElement("div");row.className="setting-row";row.dataset.path=key;
    const title=document.createElement(type==="checkbox"?"span":"label");title.className="setting-label";title.textContent=label;if(options.help)title.append(" ",infoButton(options.help));
    const line=document.createElement("div");line.className="setting-edit";let input;
    if(type==="select"){input=document.createElement("select");for(const[v,t]of options.options||[])input.add(new Option(t,v));}
    else if(type==="policy"){input=document.createElement("div");input.className="policy-editor";const select=document.createElement("select");for(const[v,t]of[["zero","归零并锁定"],["hold","保持末值"],["value","指定电压"],...(options.web?[["continue","后台继续任务"]]:[])])select.add(new Option(t,v));const voltage=document.createElement("input");voltage.type="number";voltage.required=true;voltage.step="any";voltage.min="-10";voltage.max="10";voltage.setAttribute("aria-label",label+" 指定电压 / V");select.setAttribute("aria-label",label+"动作");input.append(select,voltage);}
    else{input=document.createElement("input");input.type=type;if(type==="number"){input.required=true;input.step=options.step||"any";if(options.min!=null)input.min=options.min;if(options.max!=null)input.max=options.max;}}
    input.dataset.field=label;input.id="setting-"+key.replace(/[^a-z0-9]/gi,"-");title.id=input.id+"-label";if(type!=="checkbox")title.htmlFor=input.id;input.setAttribute("aria-label",label);
    if(type==="checkbox"){input.className="switch-input";const switchLabel=document.createElement("label");switchLabel.className="switch";switchLabel.append(input);const track=document.createElement("span");track.className="switch-track";switchLabel.append(track);line.append(switchLabel);}else line.append(input);
    const apply=document.createElement("button");apply.type="button";apply.className="apply-setting";apply.textContent="应用";if(!options.instant)line.append(apply);
    const progress=document.createElement("span");progress.className="instant-status";progress.setAttribute("role","status");const retry=document.createElement("button");retry.type="button";retry.className="instant-retry";retry.textContent="重试保存";retry.hidden=true;if(options.instant){row.classList.add("instant-setting");line.append(progress,retry);}
    const saved=document.createElement("div");saved.className="setting-values";const details=document.createElement("div");details.className="setting-plan";const error=document.createElement("p");error.className="field-error";error.setAttribute("role","status");
    const conflict=document.createElement("div");conflict.className="field-conflict";const discard=document.createElement("button");discard.type="button";discard.textContent="使用后台值";const rebase=document.createElement("button");rebase.type="button";rebase.textContent="保留草稿并重新核对";conflict.append(discard,rebase);
    row.append(title,line,saved,details,error,conflict);parent.append(row);
    const control={definition:{path,label,type,...options},entry,row,input,apply,saved,details,error,conflict,progress,retry,busy:false,plan:null,timer:null,planSerial:0};
    this.controls.set(key,control);
    const edited=()=>{let value;if(type==="checkbox")value=input.checked;else if(type==="number")value=input.valueAsNumber;else if(type==="policy"){const[a,v]=input.children;value={action:a.value,voltage:a.value==="value"?v.valueAsNumber:0};}else value=input.value;
      if(options.instant){this.applyInstant(control,value);return;}
      this.store.edit(path,value);control.plan=null;control.failure="";this.paintControl(control);clearTimeout(control.timer);control.timer=setTimeout(()=>this.preview(control),220);
    };
    if(type==="policy"){for(const node of input.children)node.addEventListener("input",edited);}else input.addEventListener(type==="select"||type==="checkbox"?"change":"input",edited);
    apply.onclick=()=>this.apply(control);discard.onclick=()=>{this.store.discard(path);control.plan=null;control.failure="";this.paintControl(control);};rebase.onclick=()=>{this.store.rebase(path);control.failure="";this.preview(control);};
    retry.onclick=()=>this.applyInstant(control,control.instantLastValue);
    this.paintControl(control);return control;
  }
  valid(control){const inputs=control.input.matches("input,select")?[control.input]:[...control.input.querySelectorAll("input,select")];return inputs.filter(n=>!n.hidden).every(n=>n.checkValidity())&&JSON.stringify(control.entry.draft)!=="null";}
  async confirmConfig(){if(this.loadTask)await this.loadTask;return this.load({quiet:true});}
  async applyInstant(c,value){if(c.busy||!this.managed())return;c.busy=true;c.instantValue=value;c.instantLastValue=value;c.failure="";c.forcePaint=true;this.paintControl(c);
    try{if(c.uncertain){const confirmed=await this.confirmConfig();if(!confirmed)throw Error("尚不能确认后台值，请恢复连接后重试");c.uncertain=false;}
      const request={path:[...c.entry.path],base:settingValueAt(this.config,c.entry.path),value};
      const plan=(await this.send("preview_settings",{changes:[request]})).plan;
      const result=await this.send("apply_settings",{changes:[request],approved:plan.changes});this.accept(result.config);c.failure="";
      if(c.entry.path.join("/")==="startup/login")await this.load({quiet:true});
    }catch(e){const confirmed=await this.confirmConfig();c.uncertain=!confirmed;
      if(!confirmed||!settingSame(settingValueAt(confirmed.config,c.entry.path),value))c.failure="保存未完成："+e.message+(confirmed?"。已显示后台确认值，可重试。":"。当前状态无法确认，可恢复连接后重试。");
    }finally{c.busy=false;c.forcePaint=true;this.paintControl(c);}
  }
  async preview(control){control.timer=null;if(control.busy||control.previewing||!this.managed())return;control.previewing=true;const key=control.entry.path.join("/"),serial=++control.planSerial,editSerial=control.entry.serial;
    if(!this.valid(control)){control.failure="请输入有效值";control.plan=null;control.previewing=false;this.paintControl(control);return;}
    try{const result=await this.send("preview_settings",{changes:[this.store.request(control.entry.path)]});if(this.controls.get(key)!==control||serial!==control.planSerial||editSerial!==control.entry.serial){control.previewing=false;return;}control.plan=result.plan;control.failure="";}catch(e){if(serial===control.planSerial&&editSerial===control.entry.serial)control.failure=e.message;}control.previewing=false;this.paintControl(control);
  }
  async apply(control,instant=false){if(control.busy||!this.managed())return;
    if(!this.valid(control)){control.failure="请输入有效值";this.paintControl(control);return;}
    const request=this.store.request(control.entry.path);control.failure="";control.busy=true;this.paintControl(control);
    try{let plan=control.plan;if(!plan){plan=(await this.send("preview_settings",{changes:[request]})).plan;control.plan=plan;if(!instant&&(plan.mode==="reconnect"||plan.changes.some(c=>c.related))){control.busy=false;this.paintControl(control);return;}}
      const result=await this.send("apply_settings",{changes:[request],approved:plan.changes});this.accept(result.config,[request]);if(control.entry.path.join("/")==="startup/login")await this.load();control.plan=null;control.failure="";this.notify(result.noop?"设置已是此值":control.definition.instant?"已保存，下次启动生效":result.mode==="offline"?"参数已保存，连接时应用":"参数已应用");
    }catch(e){control.failure=e.message;control.plan=null;this.notify(e.message,true);}finally{control.busy=false;this.paintControl(control);}
  }
  paintControl(c){if(!c.row.isConnected&&this.ready)return;const e=c.entry,d=c.definition,value=d.instant&&c.busy?c.instantValue:e.draft;
    if((!e.dirty||c.forcePaint)&&(d.instant||(!c.input.contains(document.activeElement)&&document.activeElement!==c.input))){if(d.type==="checkbox"){c.input.checked=!!value;c.input.indeterminate=!!c.uncertain;}else if(d.type==="policy"){c.input.children[0].value=value?.action||"zero";c.input.children[1].value=value?.voltage??0;}else{const display=typeof value==="number"?Number(value.toPrecision(12)):value??"";if(String(c.input.value)!==String(display))c.input.value=display;}}
    c.forcePaint=false;
    if(d.type==="policy")c.input.children[1].hidden=value?.action!=="value";
    const permitted=this.managed()&&!c.busy;for(const input of c.input.matches("input,select")?[c.input]:c.input.querySelectorAll("input,select"))input.disabled=!permitted;
    c.apply.disabled=!permitted||e.conflict||(!e.dirty&&!this.drift(e.path))||(!c.plan&&!c.failure);
    c.apply.textContent=c.busy?"正在应用…":c.failure?"重试":c.plan?.mode==="reconnect"?"清零并重连应用":c.plan?.mode==="offline"?"保存参数":"应用";
    const actual=this.state.parameter_readback,observed=actual?.connected&&Object.hasOwn(actual.values||{},e.path.join("/"))?actual.values[e.path.join("/")]:undefined;
    const readError=actual?.fields?.[e.path.join("/")]?.error;const saved=settingValueAt(this.config||{},e.path)??e.fallback,message="已保存："+this.format(saved,d)+(readError?" · 实际读取失败":observed!==undefined?" · 实际："+this.format(observed,d):"")+(e.dirty?" · 未应用草稿":"");
    if(c.saved.textContent!==message)c.saved.textContent=message;
    const compact=!!c.row.closest(".module-config,.channel-profile");c.saved.hidden=compact&&!readError&&!this.drift(e.path);if(compact&&!c.saved.hidden)c.saved.textContent=readError?"设备参数读取失败，请重试或查看通信诊断。":"设备回读为 "+this.format(observed,d)+"，与已保存值 "+this.format(saved,d)+" 不一致。";
    if(d.instant){c.progress.textContent=c.busy?"保存中…":"";c.retry.hidden=!c.failure;c.retry.disabled=!permitted;if(c.row.closest(".card-boot"))c.saved.hidden=true;c.row.setAttribute("aria-busy",String(c.busy));}
    c.error.textContent=e.conflict?"后台现值已变化，草稿保留。请先选择处理方式。":c.failure||"";c.conflict.hidden=!e.conflict;c.row.classList.toggle("has-draft",e.dirty&&!d.instant);c.row.classList.toggle("has-error",!!c.error.textContent);
    const plan=c.plan;if(plan&&e.dirty){const texts=[];for(const change of plan.changes.filter(v=>v.related)){const label=this.controls.get(change.path.join("/"))?.definition.label||change.path.join(" / ");texts.push("同时调整："+label+" → "+this.format(change.value,this.controls.get(change.path.join("/"))?.definition||{})+"（"+change.reason+"）");}
      if(plan.mode==="reconnect")texts.push("将停止发布并确认全路零输出；重连后保持锁定"+(plan.impacts?.cards?.length?" · 影响 "+plan.impacts.cards.length+" 张虚拟卡":""));
      const text=texts.join("\n");if(c.details.textContent!==text)c.details.textContent=text;
    }else c.details.textContent="";
  }
  drift(path){const values=this.state.parameter_readback?.values,key=path.join("/");const saved=settingValueAt(this.config||{},path)??this.controls.get(key)?.entry.fallback;return this.state.parameter_readback?.connected&&Object.hasOwn(values||{},key)&&saved!=="current"&&!settingSame(values[key],saved);}
  paint(){for(const c of this.controls.values())this.paintControl(c);}
  updateSyncOptions(){const control=this.controls.get("hardware/sync");if(!control)return;const choices=this.syncOptions(),key=JSON.stringify(choices);if(key===this.syncKey)return;this.syncKey=key;const value=control.entry.draft;control.input.replaceChildren();for(const[v,t]of choices)control.input.add(new Option(t,v));if(!choices.some(([v])=>v===value)){const option=new Option("当前草稿不受设备支持",value);option.disabled=true;control.input.add(option);}control.input.value=value;}
  syncOptions(){const devices=this.state.hardware_details?.length?this.state.hardware_details:this.capabilities||[];if(!devices.length)return syncChoices;return syncChoices.filter(([mode])=>mode==="current"||devices.every(d=>d.supported_modes&({freerun:1,sm:2,dc:12}[mode])));}
  build(){
    if(!this.config)return;this.controls.clear();const c=this.config;
    for(const id of["communication-settings","fault-settings","startup-settings","bridge-boot","module-settings","channel-settings"])document.getElementById(id).replaceChildren();
    const comm=document.getElementById("communication-settings");
    this.field(comm,["hardware","period_ms"],"通信周期 / ms","number",{min:1,max:50,step:"1",fallback:10,help:"主站计划的 PDO 交换间隔。实际间隔与独立 ADC 采样率不同；DC 模式改周期需要重连。"});
    this.field(comm,["hardware","sync"],"同步模式","select",{options:this.syncOptions(),help:"模式由模块能力决定。帧同步和 DC 需要关闭输入滤波，关联变化会在应用前列出。"});
    this.field(comm,["hardware","receive_timeout_us"],"收包超时 / μs","number",{min:100,max:20000,step:"1",fallback:2000,help:"等待一轮 PDO 回应的最长时间，太短可能增加接收超时。"});
    const fault=document.getElementById("fault-settings");
    this.field(fault,["faults","consecutive"],"连续 WKC 异常 / 次","number",{min:1,max:10000,step:"1",fallback:10,help:"连续异常达到阈值时执行故障策略；次数与持续时间任一达到即触发。"});
    this.field(fault,["faults","duration_ms"],"最长容错时间 / ms","number",{min:1,max:10000,step:"1",fallback:500,help:"容错期间冻结确认目标、暂停输入发送。增加该时间可能需要同时增加输出模块看门狗。"});
    this.field(fault,["faults","web_wait_ms"],"网页失联等待 / ms","number",{min:0,max:3600000,step:"1",fallback:5000});
    this.field(fault,["faults","auto_reconnect"],"总线故障后自动重连","checkbox",{fallback:true,help:"手动断开会停止重连。恢复后先确认零输出，不重放旧任务。"});
    const start=document.getElementById("startup-settings");
    for(const[key,label]of[["login","登录 Windows 后启动"],["minimized","控制台自动最小化"],["open_browser","自动打开管理网页"]])this.field(start,["startup",key],label,"checkbox",{instant:true,help:key==="login"?"注册或撤销当前用户登录启动项。":"拨动即保存，下次应用启动生效。"});
    this.field(document.getElementById("bridge-boot"),["startup","auto_connect"],"启动时自动连接固定网卡","checkbox",{instant:true,help:"使用已保存的网卡与拓扑，零输出稳定后按每张卡的启动选项发布；不恢复旧的非零输出。"});
    this.buildModules();
  }
  buildModules(){const byModule=new Map();
    for(const[d,i]of(this.config.topology.devices||[]).map((d,i)=>[d,i])){const id=moduleIdentity(i+1,d.name);byModule.set(id,{id,name:d.name,position:i+1,channels:[]});}
    for(const kind of["ai","ao"])for(const channel of this.config.topology.channels[kind]){const id=channel.id.split(":").slice(0,-1).join(":");if(!byModule.has(id))byModule.set(id,{id,name:channel.module,position:channel.slave_position,channels:[]});byModule.get(id).channels.push({kind,...channel});}
    const modules=[...byModule.values()].sort((a,b)=>a.position-b.position),tabs=document.getElementById("module-tabs");tabs.replaceChildren();
    if(!byModule.has(this.module))this.module=modules.find(m=>m.channels.length)?.id||modules[0]?.id||"";
    for(const module of modules){const b=document.createElement("button");b.type="button";b.role="tab";b.className="module-tab";b.id="module-tab-"+module.id;b.dataset.module=module.id;b.setAttribute("aria-controls","module-panel-"+module.id);b.append(modulePicture(module.name));const label=document.createElement("strong");label.textContent="从站 "+module.position+" · "+module.name;const desc=document.createElement("span");desc.textContent=module.channels.length?module.channels.length+" 路模拟量" :module.name==="EK1100"?"EtherCAT 耦合器":"设备信息";b.append(label,desc);b.onclick=()=>this.selectModule(module.id);tabs.append(b);this.buildModule(module);}
    wireTabKeys(tabs);this.showModule();if(!modules.length)document.getElementById("module-settings").textContent="连接并核对从站后，可查看模块与通道配置。";
  }
  selectModule(id,{focus=false}={}){this.module=id;this.showModule();this.revealModule(focus);}
  revealModule(focus=false){requestAnimationFrame(()=>{const tab=document.getElementById("module-tab-"+this.module),strip=document.getElementById("module-tabs");if(!tab||!strip.getClientRects().length)return;revealInStrip(strip,tab);if(focus)tab.focus({preventScroll:true});});}
  buildModule(module){const box=document.createElement("article");box.className="panel module-config";box.dataset.module=module.id;box.id="module-panel-"+module.id;box.role="tabpanel";box.setAttribute("aria-labelledby","module-tab-"+module.id);document.getElementById("module-settings").append(box);
    if(!module.channels.length){const heading=document.createElement("h3");heading.textContent=module.name==="EK1100"?"EtherCAT 耦合器":"设备信息";const info=document.createElement("p");info.textContent=module.name==="EK1100"?"连接专用以太网接口与后续 E-bus 模块。本从站没有模拟量通道，无需设置电气校准或物理量映射。":"此从站没有可配置的模拟量通道。";box.append(heading,info);}
    if(module.name==="EL3104")this.field(box,["modules",module.id,"filter"],"输入滤波","select",{options:filterChoices,help:"按整片 EL3104 设置，并非每通道独立。50/60 Hz FIR 用于抑制相应工频干扰；IIR 级别越高平滑越强、响应越慢。启用滤波需要自由运行模式。"});
    if(module.name==="EL4134")this.field(box,["modules",module.id,"watchdog_ms"],"硬件看门狗 / ms","number",{min:10,max:20000,step:"1",fallback:600,help:"该片模块的过程数据看门狗；需覆盖软件容错时间和至少两个通信周期的余量。"});
    for(const channel of module.channels)this.buildChannel(module,channel);
  }
  buildChannel(module,channel){const card=document.createElement("article");card.className="panel channel-profile";card.dataset.channel=channel.id;card.dataset.module=module.id;
    const heading=document.createElement("h3");heading.textContent=channel.kind.toUpperCase()+channel.channel;card.append(heading);document.getElementById("channel-settings").append(card);
    this.field(card,["channels",channel.id,"name"],"通道名称","text",{fallback:""});
    const tabs=document.createElement("div");tabs.className="channel-tabs";tabs.role="tablist";tabs.setAttribute("aria-label",channel.label+" 功能");card.append(tabs);
    const pages=new Map(),choices=[["calibration","电气校准"],["engineering","物理量映射"],...(channel.kind==="ao"?[["policies","失联策略"]]:[])];
    const choose=key=>{this.channelTabs.set(channel.id,key);for(const tab of tabs.children){const selected=tab.dataset.function===key;tab.setAttribute("aria-selected",String(selected));tab.tabIndex=selected?0:-1;}for(const[k,panel]of pages)panel.hidden=k!==key;};
    for(const[key,label]of choices){const id="channel-"+channel.id+"-"+key,tab=document.createElement("button"),panel=document.createElement("div");tab.type="button";tab.role="tab";tab.id=id+"-tab";tab.dataset.function=key;tab.textContent=label;tab.setAttribute("aria-controls",id);panel.id=id;panel.role="tabpanel";panel.className="channel-function";panel.setAttribute("aria-labelledby",tab.id);tab.onclick=()=>choose(key);tabs.append(tab);card.append(panel);pages.set(key,panel);}
    wireTabKeys(tabs);const calibration=pages.get("calibration");
    this.field(calibration,["channels",channel.id,"calibration","enabled"],"启用电气校准","checkbox",{fallback:false,help:"AI：原始电压乘增益加偏移；AO：按已建立的校准关系反算 DAC 指令。两点计算不会自动启用校准。"});
    const methodLabel=document.createElement("label");methodLabel.className="calibration-method";methodLabel.textContent="校准方式";const method=document.createElement("select");method.setAttribute("aria-label",channel.label+" 校准方式");method.add(new Option("直接输入参数","direct"));method.add(new Option("两点计算","two-point"));methodLabel.append(method);calibration.append(methodLabel);
    const direct=document.createElement("div"),two=document.createElement("div");direct.className="calibration-direct";two.className="calibration-two";calibration.append(direct,two);
    for(const[key,label,fallback]of[["gain","电气增益",1],["offset","电气偏移 / V",0]])this.field(direct,["channels",channel.id,"calibration",key],label,"number",{fallback,min:key==="gain"?.000001:undefined});
    this.buildTwoPoint(two,channel);const chooseMethod=()=>{this.calibrationMethods.set(channel.id,method.value);direct.hidden=method.value!=="direct";two.hidden=method.value!=="two-point";};method.value=this.calibrationMethods.get(channel.id)||"direct";method.onchange=chooseMethod;chooseMethod();
    const records=document.createElement("details");records.className="measurement-records";const title=document.createElement("summary");title.textContent="校准记录";const help=document.createElement("p");help.className="muted";help.textContent="保存测量值与计算结果用于追溯；记录本身不改变校准开关或输出。";const entries=document.createElement("div");entries.className="records-text muted";entries.dataset.channel=channel.id;records.append(title,help,entries);calibration.append(records);
    const engineering=pages.get("engineering");for(const[key,label,type,value]of[["enabled","启用物理量映射","checkbox",false],["unit","物理量单位","text",""],["voltage1","映射点 1 电压 / V","number",0],["value1","映射点 1 物理量","number",0],["voltage2","映射点 2 电压 / V","number",10],["value2","映射点 2 物理量","number",100]])this.field(engineering,["channels",channel.id,"engineering",key],label,type,{fallback:value,help:key==="enabled"?"与电气校准独立。原版中瑞协议保持电压语义；网页可反算物理量输入。":undefined});
    if(channel.kind==="ao")for(const[kind,label]of[["bus","总线故障"],["web","网页失联"],["tcp","TCP 控制端失联"]])this.field(pages.get("policies"),["faults","policies",kind,channel.id],label,"policy",{web:kind==="web",fallback:{action:kind==="web"?"continue":"zero",voltage:0},help:kind==="bus"?"总线策略同时配置硬件替代值；人工清零和正常退出始终请求物理 0 V。":"只影响该通道，其他控制者的通道继续运行。"});
    choose(this.channelTabs.get(channel.id)||"calibration");
  }
  buildTwoPoint(parent,channel){
    const box=document.createElement("div");box.className="two-point-calculation";const title=document.createElement("p");title.className="muted";title.textContent="输入两组电压后先计算预览，再应用参数；保留当前校准开关。";box.append(title);
    const grid=document.createElement("div");grid.className="form-grid";const inputs={};
    for(const[key,label,value]of[["x1",channel.kind==="ao"?"第一次下发 / V":"第一次原始电压 / V",0],["y1","第一次参考实测 / V",0],["x2",channel.kind==="ao"?"第二次下发 / V":"第二次原始电压 / V",1],["y2","第二次参考实测 / V",1]]){const labelNode=document.createElement("label");labelNode.textContent=label;const input=document.createElement("input");input.type="number";input.step="any";input.required=true;input.value=value;input.dataset.field=label;input.setAttribute("aria-label",channel.label+" · "+label);input.dataset.managed="true";labelNode.append(input);grid.append(labelNode);inputs[key]=input;}box.append(grid);
    const actions=document.createElement("div");actions.className="group";const calculate=document.createElement("button");calculate.type="button";calculate.textContent="计算并预览";calculate.dataset.managed="true";
    const apply=document.createElement("button");apply.type="button";apply.hidden=true;apply.dataset.managed="true";const note=document.createElement("p");note.className="setting-plan";
    let requests=null,plan=null,inputVersion=0;for(const input of Object.values(inputs))input.addEventListener("input",()=>{inputVersion++;requests=null;plan=null;apply.hidden=true;note.textContent="输入已改变，请重新计算";});
    calculate.onclick=async()=>{const version=inputVersion;try{
      const values=Object.fromEntries(Object.entries(inputs).map(([k,n])=>[k,n.valueAsNumber]));if(Object.values(values).some(v=>!Number.isFinite(v)))throw Error("请输入有效的两点电压");
      const data=await this.send("calibration_two_point",values),profile=this.config.channels[channel.id];if(version!==inputVersion)return;
      const measurement={date:new Date().toISOString(),type:"two_point",...values,gain:data.calibration.gain,offset:data.calibration.offset};
      const desired=[["calibration",{...profile.calibration,...data.calibration,enabled:profile.calibration.enabled}],["measurements",[...(profile.measurements||[]),measurement]]];
      requests=desired.map(([key,value])=>({path:["channels",channel.id,key],base:settingValueAt(this.config,["channels",channel.id,key]),value}));
      const preview=await this.send("preview_settings",{changes:requests});if(version!==inputVersion)return;plan=preview.plan;apply.hidden=false;apply.textContent=plan.mode==="reconnect"?"清零并重连应用两点参数":"应用两点参数与记录";
      note.textContent="增益 "+data.calibration.gain.toPrecision(7)+" · 偏移 "+data.calibration.offset.toPrecision(7)+" V · 保留当前校准开关"+(plan.mode==="reconnect"?"。将停止发布、确认零值并重连，完成后保持锁定。":"");
    }catch(e){this.notify(e.message,true);}};
    apply.onclick=async()=>{if(!requests||!this.managed())return;apply.disabled=true;try{const data=await this.send("apply_settings",{changes:requests,approved:plan.changes});this.accept(data.config,requests);apply.hidden=true;note.textContent="两点参数与校准记录已保存；校准开关保持原值";this.notify("两点参数与校准记录已应用");}catch(e){this.notify(e.message,true);note.textContent=e.message+"；请重新计算以核对当前参数";apply.hidden=true;}finally{apply.disabled=false;}};
    actions.append(calculate,apply);box.append(actions,note);parent.append(box);
  }
  showModule(){document.getElementById("import-reference").hidden=!this.config?.topology.channels.ao.some(c=>c.id.startsWith(this.module+":"));for(const box of document.querySelectorAll("#module-settings [data-module],#channel-settings [data-module]"))box.hidden=box.dataset.module!==this.module;for(const tab of document.querySelectorAll("#module-tabs [role=tab]")){const selected=tab.dataset.module===this.module;tab.setAttribute("aria-selected",String(selected));tab.tabIndex=selected?0:-1;}try{if(this.module)sessionStorage.setItem("viewer-module",this.module);}catch{}this.revealModule();}
  render(state){this.state=state;this.updateSyncOptions();for(const id of["import-reference","config-open","config-import","config-import-apply"])document.getElementById(id).disabled=!this.managed();const importLabel=document.getElementById("config-import-label");importLabel.classList.toggle("is-disabled",!this.managed());importLabel.setAttribute("aria-disabled",String(!this.managed()));importLabel.tabIndex=this.managed()?0:-1;for(const node of document.querySelectorAll("[data-managed]"))node.disabled=!this.managed();if(!this.config||(state.config_revision!=null&&state.config_revision!==this.config.revision))this.load();for(const control of this.controls.values()){this.paintControl(control);if(!control.definition.instant&&(control.entry.dirty||this.drift(control.entry.path))&&!control.plan&&!control.entry.conflict&&!control.failure&&!control.busy&&!control.previewing&&!control.timer&&this.managed()){control.timer=setTimeout(()=>this.preview(control),120);}}
    for(const records of document.querySelectorAll(".records-text")){const rows=this.config?.channels?.[records.dataset.channel]?.measurements||[];const text=rows.map(r=>r.type==="historical"?"历史零点测量："+r.zero_mv+" mV"+(r.note?"\n"+r.note:""):(r.date?new Date(r.date).toLocaleString():"时间未记录")+"\n测量点 1："+r.x1+" V → "+r.y1+" V\n测量点 2："+r.x2+" V → "+r.y2+" V\n增益 "+r.gain+" · 偏移 "+r.offset+" V").join("\n\n")||"暂无校准记录";if(records.textContent!==text)records.textContent=text;}
  }
  async export(){try{const data=await fetch("/api/config").then(r=>r.json());const url=URL.createObjectURL(new Blob([JSON.stringify(data.config,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="EtherCAT-Viewer配置.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){this.notify(e.message,true);}}
  async importFile(event){try{const incoming=JSON.parse(await event.target.files[0].text());if(![2,3].includes(incoming.version))throw Error("配置版本必须为 2 或 3");this.importDraft=incoming;this.importRevision=this.config.revision;document.getElementById("import-summary").textContent="将替换本机配置：网卡 "+incoming.hardware.interface+"，"+incoming.bridge.cards.length+" 张虚拟卡。请先核对；导入会清零并断开当前连接。";document.getElementById("import-preview").hidden=false;}catch(e){this.notify(e.message,true);}event.target.value="";}
  async importApply(){if(!this.importDraft)return;try{await this.send("disconnect",{});const result=await this.send("config_save",{config:this.importDraft,revision:this.importRevision});this.accept(result.config);await this.load();this.importDraft=null;document.getElementById("import-preview").hidden=true;this.notify("配置已导入，重新核对网卡后手动连接");}catch(e){this.notify(e.message,true);}}
  async importReference(){try{
    const data=JSON.parse(localStorage.getItem("ethercat-viewer-measurement-reference")||"null");if(!data)throw Error("此浏览器没有旧测量记录");
    const changes=[];for(const[i,key]of[[0,"zero1"],[3,"zero4"]]){const ch=this.config.topology.channels.ao[i];if(ch&&Number.isFinite(data[key])){const path=["channels",ch.id,"measurements"],base=settingValueAt(this.config,path)||[],record={type:"historical",zero_mv:data[key],note:"原浏览器记录；不自动启用校准"};
      if(!base.some(r=>r.type===record.type&&r.zero_mv===record.zero_mv))changes.push({path,base,value:[...base,record]});}}
    if(!changes.length){this.notify("没有需要迁入的新校准记录");return;}
    const plan=(await this.send("preview_settings",{changes})).plan,dataSaved=await this.send("apply_settings",{changes,approved:plan.changes});this.accept(dataSaved.config,changes);this.notify("已迁入历史校准记录，校准开关保持原值");
  }catch(e){this.notify(e.message,true);}}

}
