"use strict";
class SettingsPanel {
  constructor(post,notify){this.post=post;this.notify=notify;this.config=null;this.dirty=false;this.loading=false;
    for(const button of document.querySelectorAll(".reload-config"))button.onclick=()=>this.load(true);
    document.getElementById("hardware-form").onsubmit=e=>{e.preventDefault();this.save(["hardware"]);};
    document.getElementById("hardware-apply").onclick=async()=>{if(await this.post("disconnect"))await this.save(["hardware"]);};
    document.getElementById("system-form").onsubmit=e=>{e.preventDefault();this.save(["startup","faults","hardware"]);};
    document.getElementById("calibration-form").onsubmit=e=>{e.preventDefault();this.save(["channels"]);};
    document.getElementById("import-reference").onclick=()=>this.importReference();
    document.getElementById("config-export").onclick=async()=>{const data=await fetch("/api/config").then(r=>r.json());const url=URL.createObjectURL(new Blob([JSON.stringify(data.config,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="EtherCAT-Viewer配置.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
    document.getElementById("config-import").onchange=async e=>{try{const data=JSON.parse(await e.target.files[0].text());if(data.version!==2)throw Error("配置版本必须为 2");const current=await fetch("/api/config").then(r=>r.json());data.revision=current.config.revision;this.config=data;this.dirty=true;this.draw();this.imported=true;this.notify("已载入导入草稿；检查网卡、拓扑和所有配置后点击保存。尚未应用。");}catch(error){this.notify(error.message,true);}e.target.value="";};
  }
  async load(force=false){if(this.loading||this.dirty&&!force)return;this.loading=true;try{const response=await fetch("/api/config");if(!response.ok)throw Error("读取配置失败");const data=await response.json();this.config=data.config;this.login=data.login;this.dirty=false;this.imported=false;this.draw();if(data.error)this.notify(data.error,true);}catch(error){this.notify(error.message,true);}finally{this.loading=false;}}
  async save(sections){if(!this.config)return;const latest=await fetch("/api/config").then(r=>r.json());if(latest.config.revision!==this.config.revision){this.notify("配置已被更新，请读取已保存配置后重新修改，避免覆盖其他页面。",true);return;}
    const merged=structuredClone(latest.config);for(const section of(this.imported?Object.keys(this.config):sections))merged[section]=structuredClone(this.config[section]);
    const result=await this.post("config_save",{config:merged,revision:this.config.revision});if(result){this.config=result.config;this.dirty=false;this.imported=false;await this.load(true);this.notify("配置已保存；下次启动继续使用这些设置。");}}
  field(parent,label,type,value,change,options){const box=document.createElement("label");box.append(document.createTextNode(label));let node;
    if(type==="select"){node=document.createElement("select");for(const[k,v]of options)node.add(new Option(v,k));node.value=String(value);}else{node=document.createElement("input");node.type=type;if(type==="checkbox")node.checked=value;else node.value=value;}
    node.dataset.field=label;
    node.onchange=()=>{this.dirty=true;change(type==="checkbox"?node.checked:type==="number"?node.valueAsNumber:node.value);};box.append(node);parent.append(box);return node;}
  draw(){if(!this.config)return;const c=this.config,h=c.hardware,f=c.faults;const hardware=document.getElementById("hardware-fields");hardware.replaceChildren();
    this.field(hardware,"目标通信周期 / ms","number",h.period_ms,v=>h.period_ms=v).min=1;
    this.field(hardware,"同步模式","select",h.sync,v=>{h.sync=v;if(v==="dc"||v==="sm")h.filter="off";if(v==="freerun"&&h.filter==="off")h.filter="fir50";this.draw();},[["current","保留设备当前模式"],["freerun","自由运行 · 有滤波"],["sm","帧同步 · 关闭滤波"],["dc","DC 分布式时钟 · 关闭滤波"]]);
    this.field(hardware,"EL3104 输入滤波（整片模块）","select",h.filter,v=>h.filter=v,[["current","保留当前滤波"],["off","关闭"],["fir50","50 Hz FIR"],["fir60","60 Hz FIR"],...Array.from({length:8},(_,i)=>["iir"+(i+1),"IIR "+(i+1)])]);
    this.field(hardware,"接收超时 / μs","number",h.receive_timeout_us,v=>h.receive_timeout_us=v);
    this.field(hardware,"硬件看门狗 / ms","number",h.watchdog_ms,v=>h.watchdog_ms=v);
    const startup=document.getElementById("startup-fields");startup.replaceChildren();
    for(const[key,label]of[["unattended","下次启动自动连接、发布并授权 TCP"],["open_browser","服务就绪后自动打开管理网页"],["minimized","控制台自动最小化到任务栏"],["login","当前用户登录 Windows 后自动启动"]])this.field(startup,label,"checkbox",c.startup[key],v=>c.startup[key]=v);
    document.getElementById("login-status").textContent=this.login?.enabled?"已注册登录启动"+(this.login.current?"":"，启动路径已变化，请关闭后重新启用"):"尚未注册 Windows 登录启动。控制台可按 O 随时重新打开网页。";
    const fault=document.getElementById("fault-fields");fault.replaceChildren();
    this.field(fault,"连续 WKC 异常阈值 / 次","number",f.consecutive,v=>f.consecutive=v);
    this.field(fault,"最长容错时间 / ms","number",f.duration_ms,v=>{f.duration_ms=v;h.watchdog_ms=Math.max(h.watchdog_ms,v+100);this.draw();});
    this.field(fault,"网页失联等待 / ms","number",f.web_wait_ms,v=>f.web_wait_ms=v);
    this.field(fault,"总线故障后自动重连","checkbox",f.auto_reconnect,v=>f.auto_reconnect=v);
    const policy=document.getElementById("policy-fields");policy.replaceChildren();const table=document.createElement("table");const head=table.createTHead().insertRow();for(const label of["真实通道","总线故障","网页失联","TCP 控制端失联"])head.insertCell().textContent=label;const body=table.createTBody();
    for(const channel of c.topology.channels.ao){const row=body.insertRow();row.insertCell().textContent=channel.label;for(const kind of["bus","web","tcp"]){const cell=row.insertCell();const rule=f.policies[kind][channel.id]??{action:kind==="web"?"continue":"zero",voltage:0};
      this.field(cell,"动作","select",rule.action,v=>{rule.action=v;f.policies[kind][channel.id]=rule;},[["zero","归零并锁定"],["hold","保持末值并撤销写入"],["value","指定电压并撤销写入"],...(kind==="web"?[["continue","后台继续任务"]]:[])]);
      this.field(cell,"指定电压 / V","number",rule.voltage??0,v=>{rule.voltage=v;f.policies[kind][channel.id]=rule;});}}policy.append(table);
    this.drawChannels();
  }
  drawChannels(){const parent=document.getElementById("channel-settings");parent.replaceChildren();for(const kind of["ai","ao"])for(const channel of this.config.topology.channels[kind]){
      let profile=this.config.channels[channel.id];if(!profile)profile=this.config.channels[channel.id]={name:"",calibration:{enabled:false,gain:1,offset:0},engineering:{enabled:false,unit:"",voltage1:0,value1:0,voltage2:10,value2:100},measurements:[]};
      const box=document.createElement("details");box.className="panel channel-profile";box.dataset.channel=channel.id;const summary=document.createElement("summary");summary.textContent=channel.label+(profile.name?" · "+profile.name:"");box.append(summary);
      const grid=document.createElement("div");grid.className="form-grid";this.field(grid,"通道名称","text",profile.name,v=>profile.name=v);
      this.field(grid,"启用电气校准","checkbox",profile.calibration.enabled,v=>profile.calibration.enabled=v);this.field(grid,"电气增益","number",profile.calibration.gain,v=>profile.calibration.gain=v).step="any";this.field(grid,"电气偏移 / V","number",profile.calibration.offset,v=>profile.calibration.offset=v).step="any";
      box.append(grid);const points=document.createElement("div");points.className="form-grid two-point";const values={x1:0,y1:0,x2:1,y2:1};for(const key of["x1","y1","x2","y2"])this.field(points,({x1:kind==="ao"?"第一次下发 / V":"第一次原始电压 / V",y1:"第一次参考实测 / V",x2:kind==="ao"?"第二次下发 / V":"第二次原始电压 / V",y2:"第二次参考实测 / V"})[key],"number",values[key],v=>values[key]=v).step="any";
      const compute=document.createElement("button");compute.type="button";compute.textContent="按两点计算校准";compute.onclick=async()=>{const result=await this.post("calibration_two_point",values);if(result){profile.calibration=result.calibration;profile.measurements.push({type:"two_point",date:new Date().toISOString(),...values});this.dirty=true;this.drawChannels();this.notify("两点校准已计算到草稿，保存后才生效。");}};box.append(points,compute);
      const eng=document.createElement("div");eng.className="form-grid engineering-fields";this.field(eng,"启用物理量映射","checkbox",profile.engineering.enabled,v=>profile.engineering.enabled=v);this.field(eng,"单位（如 MPa、mm）","text",profile.engineering.unit,v=>profile.engineering.unit=v);
      for(const[key,label]of[["voltage1","映射点 1 电压 / V"],["value1","映射点 1 物理量"],["voltage2","映射点 2 电压 / V"],["value2","映射点 2 物理量"]])this.field(eng,label,"number",profile.engineering[key],v=>profile.engineering[key]=v).step="any";box.append(eng);
      const records=document.createElement("p");records.className="muted";records.textContent="测量记录："+(profile.measurements?.map(v=>v.type==="historical"?"历史零指令实测 "+v.zero_mv+" mV":v.date+" 两点测量").join("；")||"暂无");box.append(records);parent.append(box);
    }if(!parent.children.length)parent.textContent="连接一次实机后显示真实通道；之后可离线编辑。";
  }
  importReference(){try{const data=JSON.parse(localStorage.getItem("ethercat-viewer-measurement-reference")||"null");if(!data)throw Error("当前浏览器没有旧测量记录");for(const[i,key]of[[0,"zero1"],[3,"zero4"]]){const ch=this.config.topology.channels.ao[i];if(ch&&Number.isFinite(data[key])){const profile=this.config.channels[ch.id];profile.measurements??=[];profile.measurements.push({type:"historical",zero_mv:data[key],note:"原浏览器记录；不自动启用校准"});}}this.dirty=true;this.drawChannels();this.notify("已迁入测量记录草稿，尚未保存，校准不会自动启用。");}catch(error){this.notify(error.message,true);}}
  render(state,pending){this.state=state;for(const form of document.querySelectorAll("#hardware-form,#system-form,#calibration-form"))for(const node of form.querySelectorAll("input,select,button"))node.disabled=!state.controller||pending;
    const stale=this.config&&this.config.revision!==state.config_revision;document.getElementById("config-warning").textContent=this.dirty?"有未保存配置"+(stale?"；后台已有新版本，请重新读取":""):stale?"后台配置已更新，可读取最新版本":"";
    if(stale&&!this.dirty&&!this.loading)this.load();
    document.getElementById("hardware-readback").textContent=(state.hardware_details||[]).map(d=>d.name+"：同步 "+({0:"自由运行",1:"帧同步",2:"DC SYNC0",3:"DC SYNC1"}[d.sync_mode]??d.sync_mode)+(d.kind==="ai"?" · "+(d.filter_enabled?"滤波索引 "+d.filter_index:"滤波关闭"):"")).join("；")+(state.dc_error_ns!=null?" · DC 相位偏差 "+Math.round(state.dc_error_ns)+" ns":"");
  }
}
