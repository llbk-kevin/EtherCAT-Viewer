"use strict";
const profileDefault={name:"",calibration:{enabled:false,gain:1,offset:0},engineering:{enabled:false,unit:"",voltage1:0,value1:0,voltage2:10,value2:100},measurements:[]};
const filterChoices=[["current","保留设备设置"],["off","关闭"],["fir50","50 Hz FIR"],["fir60","60 Hz FIR"],...Array.from({length:8},(_,i)=>["iir"+(i+1),"IIR "+(i+1)])];
const syncChoices=[["current","保留设备模式"],["freerun","自由运行"],["sm","帧同步"],["dc","DC 分布式时钟"]];
class SettingsPanel{
  constructor(send,notify,managed){this.send=send;this.notify=notify;this.managed=managed;this.store=new DraftStore();this.controls=new Map();this.loading=false;this.state={};this.schema="";this.ready=false;this.config=null;this.module=null;
    document.getElementById("module-selector").onchange=e=>{this.module=e.target.value;this.showModule();};
    document.getElementById("config-export").onclick=()=>this.export();
    document.getElementById("config-copy").onclick=async()=>{try{await navigator.clipboard.writeText(this.path);this.notify("已复制配置路径");}catch(e){this.notify("复制失败："+e.message,true);}};
    document.getElementById("config-open").onclick=async()=>{try{await this.send("open_config_directory",{});this.notify("已打开配置文件夹");}catch(e){this.notify(e.message,true);}};
    document.getElementById("config-import").onchange=e=>this.importFile(e);document.getElementById("config-import-label").onkeydown=e=>{if((e.key==="Enter"||e.key===" ")&&this.managed()){e.preventDefault();document.getElementById("config-import").click();}};
    document.getElementById("config-import-cancel").onclick=()=>{this.importDraft=null;document.getElementById("import-preview").hidden=true;};
    document.getElementById("config-import-apply").onclick=()=>this.importApply();
    document.getElementById("import-reference").onclick=()=>this.importReference();
  }
  async load(){if(this.loading)return;this.loading=true;try{const response=await fetch("/api/config",{signal:AbortSignal.timeout(10000)});if(!response.ok)throw Error("配置读取失败");const data=await response.json();this.accept(data.config);this.capabilities=data.capabilities;this.login=data.login;this.path=data.path;document.getElementById("config-location").textContent=data.path;document.getElementById("login-status").textContent=data.login?.enabled?"已注册当前用户登录启动"+(data.login.current?"":" · 启动路径已变化"):"未启用登录启动";if(data.error&&this.configError!==data.error){this.notify(data.error,true);this.configError=data.error;}}catch(e){this.notify(e.message,true);}finally{this.loading=false;}}
  accept(config,requests){const ids=new Set(config.bridge.cards.map(c=>c.id));for(const[key,e]of this.store.entries)if(e.path[0]==="bridge"&&e.path[1]==="cards"&&!ids.has(e.path[2])){this.store.entries.delete(key);this.controls.delete(key);}if(requests)this.store.accepted(config,requests);else this.store.sync(config);this.config=config;const schema=JSON.stringify(config.topology);if(schema!==this.schema){this.schema=schema;this.build();}this.ready=true;this.paint();}
  definition(path){return this.controls.get(path.join("/"))?.definition;}
  format(value,def){if(value==null)return"未知";if(value==="mixed")return"模块模式不一致";if(def.type==="checkbox")return value?"开启":"关闭";if(def.options)return def.options.find(([v])=>v===String(value))?.[1]||String(value);if(def.type==="policy")return({zero:"归零并锁定",hold:"保持末值",continue:"继续任务",value:"指定 "+value.voltage+" V"})[value.action]||"未知";if(Array.isArray(value))return value.length+" 条";return String(typeof value==="number"?Number(value.toPrecision(12)):value)+(def.unit?" "+def.unit:"");}
  field(parent,path,label,type="number",options={}){
    const key=path.join("/"),fallback=options.fallback??(type==="checkbox"?false:type==="number"?0:"");
    const entry=this.store.entry(path,fallback),row=document.createElement("div");row.className="setting-row";row.dataset.path=key;
    const title=document.createElement("label");title.className="setting-label";title.textContent=label;if(options.help)title.append(" ",infoButton(options.help));
    const line=document.createElement("div");line.className="setting-edit";let input;
    if(type==="select"){input=document.createElement("select");for(const[v,t]of options.options||[])input.add(new Option(t,v));}
    else if(type==="policy"){input=document.createElement("div");input.className="policy-editor";const select=document.createElement("select");for(const[v,t]of[["zero","归零并锁定"],["hold","保持末值"],["value","指定电压"],...(options.web?[["continue","后台继续任务"]]:[])])select.add(new Option(t,v));const voltage=document.createElement("input");voltage.type="number";voltage.required=true;voltage.step="any";voltage.min="-10";voltage.max="10";voltage.setAttribute("aria-label",label+" 指定电压 / V");select.setAttribute("aria-label",label+"动作");input.append(select,voltage);}
    else{input=document.createElement("input");input.type=type;if(type==="number"){input.required=true;input.step=options.step||"any";if(options.min!=null)input.min=options.min;if(options.max!=null)input.max=options.max;}}
    input.dataset.field=label;input.id="setting-"+key.replace(/[^a-z0-9]/gi,"-");title.htmlFor=input.id;input.setAttribute("aria-label",label);
    if(type==="checkbox"){input.className="switch-input";const switchLabel=document.createElement("label");switchLabel.className="switch";switchLabel.append(input);const track=document.createElement("span");track.className="switch-track";switchLabel.append(track);line.append(switchLabel);}else line.append(input);
    const apply=document.createElement("button");apply.type="button";apply.className="apply-setting";apply.textContent="应用";if(!options.instant)line.append(apply);
    const saved=document.createElement("div");saved.className="setting-values";const details=document.createElement("div");details.className="setting-plan";const error=document.createElement("p");error.className="field-error";error.setAttribute("role","status");
    const conflict=document.createElement("div");conflict.className="field-conflict";const discard=document.createElement("button");discard.type="button";discard.textContent="使用后台值";const rebase=document.createElement("button");rebase.type="button";rebase.textContent="保留草稿并重新核对";conflict.append(discard,rebase);
    row.append(title,line,saved,details,error,conflict);parent.append(row);
    const control={definition:{path,label,type,...options},entry,row,input,apply,saved,details,error,conflict,busy:false,plan:null,timer:null,planSerial:0};
    this.controls.set(key,control);
    const edited=()=>{let value;if(type==="checkbox")value=input.checked;else if(type==="number")value=input.valueAsNumber;else if(type==="policy"){const[a,v]=input.children;value={action:a.value,voltage:a.value==="value"?v.valueAsNumber:0};}else value=input.value;
      this.store.edit(path,value);control.plan=null;control.failure="";this.paintControl(control);clearTimeout(control.timer);
      if(options.instant)this.apply(control,true);else control.timer=setTimeout(()=>this.preview(control),220);
    };
    if(type==="policy"){for(const node of input.children)node.addEventListener("input",edited);}else input.addEventListener(type==="select"||type==="checkbox"?"change":"input",edited);
    apply.onclick=()=>this.apply(control);discard.onclick=()=>{this.store.discard(path);control.plan=null;control.failure="";this.paintControl(control);};rebase.onclick=()=>{this.store.rebase(path);control.failure="";this.preview(control);};
    this.paintControl(control);return control;
  }
  valid(control){const inputs=control.input.matches("input,select")?[control.input]:[...control.input.querySelectorAll("input,select")];return inputs.filter(n=>!n.hidden).every(n=>n.checkValidity())&&JSON.stringify(control.entry.draft)!=="null";}
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
  paintControl(c){if(!c.row.isConnected&&this.ready)return;const e=c.entry,d=c.definition,value=e.draft;
    if((!e.dirty||c.forcePaint)&&!c.input.contains(document.activeElement)&&document.activeElement!==c.input){if(d.type==="checkbox")c.input.checked=!!value;else if(d.type==="policy"){c.input.children[0].value=value?.action||"zero";c.input.children[1].value=value?.voltage??0;}else{const display=typeof value==="number"?Number(value.toPrecision(12)):value??"";if(String(c.input.value)!==String(display))c.input.value=display;}}
    c.forcePaint=false;
    if(d.type==="policy")c.input.children[1].hidden=value?.action!=="value";
    const permitted=this.managed()&&!c.busy;for(const input of c.input.matches("input,select")?[c.input]:c.input.querySelectorAll("input,select"))input.disabled=!permitted;
    c.apply.disabled=!permitted||e.conflict||(!e.dirty&&!this.drift(e.path))||(!c.plan&&!c.failure);
    c.apply.textContent=c.busy?"正在应用…":c.failure?"重试":c.plan?.mode==="reconnect"?"清零并重连应用":c.plan?.mode==="offline"?"保存参数":"应用";
    const actual=this.state.parameter_readback,observed=actual?.connected&&Object.hasOwn(actual.values||{},e.path.join("/"))?actual.values[e.path.join("/")]:undefined;
    const readError=actual?.fields?.[e.path.join("/")]?.error;const saved=settingValueAt(this.config||{},e.path),message="已保存："+this.format(saved??e.fallback,d)+(readError?" · 实际读取失败":observed!==undefined?" · 实际："+this.format(observed,d):"")+(e.dirty?" · 未应用草稿":"");
    if(c.saved.textContent!==message)c.saved.textContent=message;
    c.error.textContent=e.conflict?"后台现值已变化，草稿保留。请先选择处理方式。":c.failure||"";c.conflict.hidden=!e.conflict;c.row.classList.toggle("has-draft",e.dirty);c.row.classList.toggle("has-error",!!c.error.textContent);
    const plan=c.plan;if(plan&&e.dirty){const texts=[];for(const change of plan.changes.filter(v=>v.related)){const label=this.controls.get(change.path.join("/"))?.definition.label||change.path.join(" / ");texts.push("同时调整："+label+" → "+this.format(change.value,this.controls.get(change.path.join("/"))?.definition||{})+"（"+change.reason+"）");}
      if(plan.mode==="reconnect")texts.push("将停止发布并确认全路零输出；重连后保持锁定"+(plan.impacts?.cards?.length?" · 影响 "+plan.impacts.cards.length+" 张虚拟卡":""));
      const text=texts.join("\n");if(c.details.textContent!==text)c.details.textContent=text;
    }else c.details.textContent="";
  }
  drift(path){const values=this.state.parameter_readback?.values,key=path.join("/");const saved=settingValueAt(this.config||{},path);return this.state.parameter_readback?.connected&&Object.hasOwn(values||{},key)&&saved!=="current"&&!settingSame(values[key],saved);}
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
  buildModules(){const c=this.config,byModule=new Map();for(const kind of["ai","ao"])for(const channel of c.topology.channels[kind]){const id=channel.id.split(":").slice(0,-1).join(":");if(!byModule.has(id))byModule.set(id,{id,name:channel.module,position:channel.slave_position,channels:[]});byModule.get(id).channels.push({kind,...channel});}
    const select=document.getElementById("module-selector");select.replaceChildren();for(const module of[...byModule.values()].sort((a,b)=>a.position-b.position)){select.add(new Option("从站 "+module.position+" · "+module.name,module.id));this.buildModule(module);}
    if(!byModule.has(this.module))this.module=byModule.keys().next().value||"";select.value=this.module;this.showModule();
    if(!byModule.size)document.getElementById("module-settings").textContent="连接并核对从站后，可查看模块与通道配置。";
  }
  buildModule(module){const box=document.createElement("article");box.className="panel module-config";box.dataset.module=module.id;const heading=document.createElement("h3");heading.textContent="从站 "+module.position+" · "+module.name;box.append(heading);document.getElementById("module-settings").append(box);
    if(module.name==="EL3104")this.field(box,["modules",module.id,"filter"],"输入滤波","select",{options:filterChoices,help:"按整片 EL3104 设置，并非每通道独立。启用滤波需要自由运行模式。"});
    if(module.name==="EL4134")this.field(box,["modules",module.id,"watchdog_ms"],"硬件看门狗 / ms","number",{min:10,max:20000,step:"1",fallback:600,help:"该片模块的过程数据看门狗；需覆盖软件容错时间和至少两个通信周期的余量。"});
    for(const channel of module.channels){const details=document.createElement("details");details.className="panel channel-profile";details.dataset.channel=channel.id;details.dataset.module=module.id;const summary=document.createElement("summary");summary.textContent=channel.label;details.append(summary);document.getElementById("channel-settings").append(details);
      this.field(details,["channels",channel.id,"name"],"通道名称","text",{fallback:""});
      if(channel.kind==="ao"){const h=document.createElement("h4");h.textContent="失联策略";details.append(h);for(const[kind,label]of[["bus","总线故障"],["web","网页失联"],["tcp","TCP 控制端失联"]])this.field(details,["faults","policies",kind,channel.id],label,"policy",{web:kind==="web",fallback:{action:kind==="web"?"continue":"zero",voltage:0},help:kind==="bus"?"总线策略同时配置硬件替代值；人工清零和正常退出始终请求物理 0 V。":"只影响该通道，其他控制者的通道继续运行。"});}
      const calibration=document.createElement("h4");calibration.textContent="电气校准";details.append(calibration);
      for(const[key,label,type,fallback]of[["enabled","启用电气校准","checkbox",false],["gain","电气增益","number",1],["offset","电气偏移 / V","number",0]])this.field(details,["channels",channel.id,"calibration",key],label,type,{fallback,min:key==="gain"?.000001:undefined,help:key==="enabled"?"AI：原始电压乘增益加偏移；AO：按校准关系反算 DAC 指令。":undefined});
      this.buildTwoPoint(details,channel);
      const engineering=document.createElement("h4");engineering.textContent="物理量映射";details.append(engineering);
      for(const[key,label,type,value]of[["enabled","启用物理量映射","checkbox",false],["unit","物理量单位","text",""],["voltage1","映射点 1 电压 / V","number",0],["value1","映射点 1 物理量","number",0],["voltage2","映射点 2 电压 / V","number",10],["value2","映射点 2 物理量","number",100]])this.field(details,["channels",channel.id,"engineering",key],label,type,{fallback:value,help:key==="enabled"?"与电气校准独立。原版中瑞协议保持电压语义；网页可反算物理量输入。":undefined});
      const records=document.createElement("details");records.className="measurement-records";const title=document.createElement("summary");title.textContent="测量记录";records.append(title);const p=document.createElement("p");p.className="records-text muted";p.dataset.channel=channel.id;records.append(p);details.append(records);
    }
  }
  buildTwoPoint(parent,channel){
    const box=document.createElement("details");box.className="two-point-calculation";const title=document.createElement("summary");title.textContent="按两点计算校准";box.append(title);
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
    apply.onclick=async()=>{if(!requests||!this.managed())return;apply.disabled=true;try{const data=await this.send("apply_settings",{changes:requests,approved:plan.changes});this.accept(data.config,requests);apply.hidden=true;note.textContent="两点参数与测量记录已保存；校准开关保持原值";this.notify("两点参数与测量记录已应用");}catch(e){this.notify(e.message,true);note.textContent=e.message+"；请重新计算以核对当前参数";apply.hidden=true;}finally{apply.disabled=false;}};
    actions.append(calculate,apply);box.append(actions,note);parent.append(box);
  }
  showModule(){for(const box of document.querySelectorAll("#module-settings [data-module],#channel-settings [data-module]"))box.hidden=box.dataset.module!==this.module;}
  render(state){this.state=state;this.updateSyncOptions();for(const id of["import-reference","config-open","config-import","config-import-apply"])document.getElementById(id).disabled=!this.managed();const importLabel=document.getElementById("config-import-label");importLabel.classList.toggle("is-disabled",!this.managed());importLabel.setAttribute("aria-disabled",String(!this.managed()));importLabel.tabIndex=this.managed()?0:-1;for(const node of document.querySelectorAll("[data-managed]"))node.disabled=!this.managed();if(!this.config||(state.config_revision!=null&&state.config_revision!==this.config.revision))this.load();for(const control of this.controls.values()){this.paintControl(control);if((control.entry.dirty||this.drift(control.entry.path))&&!control.plan&&!control.entry.conflict&&!control.failure&&!control.busy&&!control.previewing&&!control.timer&&this.managed()){control.timer=setTimeout(()=>this.preview(control),120);}}
    for(const records of document.querySelectorAll(".records-text")){const rows=this.config?.channels?.[records.dataset.channel]?.measurements||[];const text=rows.map(r=>r.type==="historical"?"历史测量："+r.zero_mv+" mV":r.date+" · 两点测量").join("\n")||"暂无测量记录";if(records.textContent!==text)records.textContent=text;}
  }
  async export(){try{const data=await fetch("/api/config").then(r=>r.json());const url=URL.createObjectURL(new Blob([JSON.stringify(data.config,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="EtherCAT-Viewer配置.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){this.notify(e.message,true);}}
  async importFile(event){try{const incoming=JSON.parse(await event.target.files[0].text());if(![2,3].includes(incoming.version))throw Error("配置版本必须为 2 或 3");this.importDraft=incoming;this.importRevision=this.config.revision;document.getElementById("import-summary").textContent="将替换本机配置：网卡 "+incoming.hardware.interface+"，"+incoming.bridge.cards.length+" 张虚拟卡。请先核对；导入会清零并断开当前连接。";document.getElementById("import-preview").hidden=false;}catch(e){this.notify(e.message,true);}event.target.value="";}
  async importApply(){if(!this.importDraft)return;try{await this.send("disconnect",{});const result=await this.send("config_save",{config:this.importDraft,revision:this.importRevision});this.accept(result.config);await this.load();this.importDraft=null;document.getElementById("import-preview").hidden=true;this.notify("配置已导入，重新核对网卡后手动连接");}catch(e){this.notify(e.message,true);}}
  async importReference(){try{
    const data=JSON.parse(localStorage.getItem("ethercat-viewer-measurement-reference")||"null");if(!data)throw Error("此浏览器没有旧测量记录");
    const changes=[];for(const[i,key]of[[0,"zero1"],[3,"zero4"]]){const ch=this.config.topology.channels.ao[i];if(ch&&Number.isFinite(data[key])){const path=["channels",ch.id,"measurements"],base=settingValueAt(this.config,path)||[],record={type:"historical",zero_mv:data[key],note:"原浏览器记录；不自动启用校准"};
      if(!base.some(r=>r.type===record.type&&r.zero_mv===record.zero_mv))changes.push({path,base,value:[...base,record]});}}
    if(!changes.length){this.notify("没有需要迁入的新测量记录");return;}
    const plan=(await this.send("preview_settings",{changes})).plan,dataSaved=await this.send("apply_settings",{changes,approved:plan.changes});this.accept(dataSaved.config,changes);this.notify("已迁入历史测量记录，校准开关保持原值");
  }catch(e){this.notify(e.message,true);}}

}
