"use strict";
class DiagnosticCharts {
  constructor(visible){this.visible=visible;this.rows=new Map();this.generation=null;this.requestEpoch=0;this.clearing=false;this.emptyMessage="";this.width=30;this.end=null;this.plots=[];this.busy=false;this.events=[];
    for(const[id,label,color]of[["interval-chart","间隔",traceColors[0]],["jitter-chart","偏差",traceColors[1]],["roundtrip-chart","耗时",traceColors[2]],["wkc-chart","WKC",traceColors[3]]]){
      const box=document.getElementById(id);const plot=new uPlot({width:Math.max(1,box.clientWidth),height:170,legend:{show:false},scales:{x:{time:true}},axes:[{stroke:"#354b64",values:(_u,values)=>values.map(v=>new Date(v*1000).toLocaleTimeString("zh-CN",{hour12:false})),grid:{stroke:"#e7edf4"}},{stroke:"#354b64",size:55,grid:{stroke:"#e7edf4"}}],series:[{},{label,stroke:color,width:1.5,spanGaps:false}],cursor:{drag:{x:true,y:false,setScale:false,dist:6}},hooks:{setSelect:[u=>{if(u.select.width<6)return;const a=u.posToVal(u.select.left,"x"),b=u.posToVal(u.select.left+u.select.width,"x");u.setSelect({left:0,top:0,width:0,height:0},false);this.width=Math.max(.05,b-a);this.end=b;this.update();}],draw:[u=>{const ctx=u.ctx;ctx.save();ctx.strokeStyle="#bc706780";for(const e of this.events){const x=u.valToPos(e.time_ms/1000,"x",true);if(x>=u.bbox.left&&x<=u.bbox.left+u.bbox.width){ctx.beginPath();ctx.moveTo(x,u.bbox.top);ctx.lineTo(x,u.bbox.top+u.bbox.height);ctx.stroke();}}ctx.restore();}]}},[[],[]],box);
      plot.over.addEventListener("wheel",e=>{e.preventDefault();const fraction=(e.clientX-plot.over.getBoundingClientRect().left)/Math.max(1,plot.over.clientWidth);this.zoom(e.deltaY<0?.8:1.25,fraction);},{passive:false});
      plot.over.addEventListener("dblclick",()=>this.follow());new ResizeObserver(()=>{if(box.clientWidth)plot.setSize({width:box.clientWidth,height:170});}).observe(box);this.plots.push(plot);
    }
    document.getElementById("diag-zoom-in").onclick=()=>this.zoom(.5);document.getElementById("diag-zoom-out").onclick=()=>this.zoom(2);document.getElementById("diag-follow").onclick=()=>this.follow();
    document.getElementById("event-filter").onchange=()=>this.renderEvents();document.getElementById("clear-diagnostics").onclick=()=>this.clear();setInterval(()=>{document.getElementById("clear-diagnostics").disabled=!canManage()||this.clearing;this.update();},1000);
  }
  follow(){this.end=null;this.width=30;this.update();}
  zoom(factor,anchor=.5){const end=this.end??Date.now()/1000,point=end-this.width+this.width*anchor;this.width=Math.max(.05,Math.min(1800,this.width*factor));this.end=point+this.width*(1-anchor);this.update();}
  async update(){if(!this.visible()||this.busy)return;this.busy=true;const epoch=this.requestEpoch;try{const end=this.end??Date.now()/1000,start=end-this.width;
      const response=await fetch('/api/diagnostics/history?start='+start*1000+'&end='+end*1000+'&limit=4000');if(!response.ok)throw Error("诊断历史读取失败");const data=await response.json();
      const x=data.rows.map(r=>r[0]/1000),ys=[data.rows.map(r=>r[1]),data.rows.map(r=>r[1]==null?null:r[1]-r[3]),data.rows.map(r=>r[2]),data.rows.map(r=>r[4])];
      this.plots.forEach((u,i)=>u.batch(()=>{u.setData([x,ys[i]],false);u.setScale("x",{min:start,max:end});u.setScale("y",{min:null,max:null});}));
      document.getElementById("diag-window").textContent=this.width.toFixed(this.width<1?2:0)+" 秒 · "+(this.end===null?"实时":"历史");
      const events=await fetch("/api/events").then(r=>r.json());if(epoch!==this.requestEpoch)return;if(this.generation!==events.generation){this.requestEpoch++;this.resetEvents();this.generation=events.generation;}this.events=events.events;this.renderEvents();document.getElementById("journal-error").textContent=events.error||"";
    }catch(error){document.getElementById("journal-error").textContent=error.message;}finally{this.busy=false;}}
  resetEvents(){for(const row of this.rows.values())row.clip?.destroy();this.rows.clear();this.events=[];document.getElementById("event-list").replaceChildren();}
  async clear(){
    if(this.clearing||!canManage())return;
    const dialog=document.getElementById("clear-diagnostics-dialog");if(dialog.open)return;dialog.returnValue="cancel";const confirmation=new Promise(resolve=>dialog.addEventListener("close",()=>resolve(dialog.returnValue==="clear"),{once:true}));dialog.showModal();if(!await confirmation||!canManage())return;
    this.clearing=true;this.requestEpoch++;document.getElementById("clear-diagnostics").disabled=true;
    try{const result=await requestSetting("clear_diagnostics");this.requestEpoch++;this.resetEvents();this.generation=result.diagnostics.generation;this.emptyMessage="诊断日志已清空。随后发生的新事件会继续记录。";document.getElementById("event-list").textContent=this.emptyMessage;document.getElementById("journal-error").textContent="";}
    catch(error){document.getElementById("journal-error").textContent="清空失败："+error.message+"；请重新读取确认。";}
    finally{this.clearing=false;document.getElementById("clear-diagnostics").disabled=!canManage();}
  }
  renderEvents(){
    const parent=document.getElementById("event-list"),filter=document.getElementById("event-filter").value;
    const retained=this.events.slice(-100),ids=new Set(retained.map(e=>e.event_id));
    for(const[id,row]of this.rows)if(!ids.has(id)){row.clip?.destroy();row.element.remove();this.rows.delete(id);}
    const wanted=retained.filter(e=>!filter||e.category===filter).reverse();
    if(!wanted.length){if(!parent.querySelector(".event"))parent.textContent=!this.events.length&&this.emptyMessage?this.emptyMessage:"当前分类暂无事件。";else for(const row of this.rows.values())row.element.hidden=true;return;}
    if(!parent.querySelector(".event"))parent.replaceChildren();
    for(const row of this.rows.values())row.element.hidden=true;
    for(let i=0;i<wanted.length;i++){
      const event=wanted[i];let row=this.rows.get(event.event_id);
      if(!row){const element=document.createElement("details"),summary=document.createElement("summary"),body=document.createElement("div");element.className="event severity-"+event.severity;element.dataset.eventId=event.event_id;element.append(summary,body);row={element,summary,body,event,loadedRevision:null,loading:false,clip:null};this.rows.set(event.event_id,row);element.addEventListener("toggle",()=>{if(element.open){this.load(row);row.clip?.resize();}});}
      row.event=event;row.element.hidden=false;row.summary.textContent=new Date(event.time_ms).toLocaleTimeString()+" · "+({connection:"连接",runtime:"运行",alarm:"报警",control:"控制"}[event.category]||event.category)+" · "+event.message+(event.fragment_state==="collecting"?" · 片段采集中":"");
      if(parent.children[i]!==row.element)parent.insertBefore(row.element,parent.children[i]||null);
      if(row.element.open)this.load(row);
    }
  }
  async load(row,force=false){
    if(row.loading||!force&&row.loadedRevision===row.event.revision)return;
    const epoch=this.requestEpoch,generation=this.generation,revision=row.event.revision;row.loading=true;
    if(!row.loadedRevision)row.body.textContent="正在加载事件详情…";
    try{const response=await fetch("/api/events/"+encodeURIComponent(row.event.event_id)+"?generation="+encodeURIComponent(generation),{signal:AbortSignal.timeout(10000)});const result=await response.json();if(!response.ok)throw Error(result.detail||"读取失败");if(epoch!==this.requestEpoch||result.generation!==this.generation)return;
      const event=result.event;row.loadedRevision=revision;
      if(!row.content){row.body.replaceChildren();row.content=document.createElement("div");row.body.append(row.content);}row.content.replaceChildren();
      const line=(text)=>{if(!text)return;const p=document.createElement("p");p.className="muted";p.textContent=text;row.content.append(p);};
      line(event.time+" · "+(event.wkc!=null?"WKC "+event.wkc+" / "+event.expected_wkc:""));
      line(event.nic?.items?.map(i=>i.name+" "+(i.status==="Up"?"链路正常":"链路断开")+" "+i.speed).join("；"));
      line(event.devices?.map(d=>d.name+" "+({"0x08":"运行 OP","0x04":"安全运行 SAFEOP","0x02":"预运行 PREOP","0x01":"初始化 INIT"}[d.state]||d.state)).join(" → "));
      if(event.parameter){const p=event.parameter,parts=p.path.split("/"),key=parts.at(-1),label={filter:"输入滤波",watchdog_ms:"硬件看门狗",sync:"同步模式"}[key]||"失联策略";const format=v=>v==null?"未知":typeof v==="object"?({zero:"归零",hold:"保持",value:"指定电压",continue:"继续任务"}[v.action]||v.action)+(v.action==="value"?" "+v.voltage+" V":""):String(v);line(parts.filter(s=>s.startsWith("s")&&s.includes(":" )).join(" · ")+" · "+label+" · 期望："+format(event.expected));line(p.error?"读取失败："+p.error+"；最后成功值："+format(p.last_success_value)+(p.last_success_ns?"（单调时钟 "+(p.last_success_ns/1e9).toFixed(3)+" 秒）":"（尚无成功记录）"):"实际回读："+format(p.value));}
      for(const slave of event.diagnostic?.slaves||[])line(slave.name+" · CRC／接收错误 "+(slave.errors?slave.errors.slice(0,8).join(" / "):"未知")+" · Lost Link "+(slave.lost_link?.join(" / ")??"未知"));
      line(event.reason);line(event.related?"原记录关联说明："+event.related:"");
      if(event.before||event.after||event.fragment_state!=="none"){
        if(!row.clip){row.clip=new FaultFragment(row.body);}
        row.clip.update(event);
      }
    }catch(error){if(epoch!==this.requestEpoch)return;row.body.replaceChildren();row.content=null;row.clip?.destroy();row.clip=null;const p=document.createElement("p"),retry=document.createElement("button");p.textContent="事件详情加载失败："+error.message;retry.textContent="重试加载";retry.onclick=()=>this.load(row,true);row.body.append(p,retry);}
    finally{row.loading=false;}
  }
}

class FaultFragment {
  constructor(parent){this.box=document.createElement("section");this.box.className="fault-fragment";this.caption=document.createElement("p");this.controls=document.createElement("div");this.controls.className="toolbar";this.grid=document.createElement("div");this.grid.className="fault-fragment-grid";this.box.append(this.caption,this.controls,this.grid);parent.append(this.box);this.plots=[];this.window=null;
    const reset=document.createElement("button");reset.textContent="显示完整片段";reset.onclick=()=>{this.window=null;this.draw();};this.controls.append(reset);
    for(const[label,color]of[["实际周期 / ms",traceColors[0]],["周期偏差 / ms",traceColors[1]],["收发耗时 / ms",traceColors[2]],["WKC",traceColors[3]]]){const box=document.createElement("div"),title=document.createElement("p"),target=document.createElement("div");title.textContent=label;box.append(title,target);this.grid.append(box);const plot=new uPlot({width:Math.max(200,target.clientWidth),height:150,legend:{show:false},scales:{x:{time:true}},series:[{},{label,stroke:color,width:1.5,spanGaps:false}],axes:[{stroke:"#354b64",values:(_u,values)=>values.map(v=>new Date(v*1000).toLocaleTimeString("zh-CN",{hour12:false}))},{stroke:"#354b64",size:50}],cursor:{drag:{x:true,y:false,setScale:false,dist:6}},hooks:{setSelect:[u=>{if(u.select.width<6)return;this.window=[u.posToVal(u.select.left,"x"),u.posToVal(u.select.left+u.select.width,"x")];u.setSelect({left:0,top:0,width:0,height:0},false);this.draw();}]}},[[],[]],target);plot.over.addEventListener("wheel",e=>{e.preventDefault();if(!this.data?.[0].length)return;const [a,b]=this.window||[this.data[0][0],this.data[0].at(-1)],fraction=(e.clientX-plot.over.getBoundingClientRect().left)/Math.max(1,plot.over.clientWidth),anchor=a+(b-a)*fraction,width=Math.max(.01,(b-a)*(e.deltaY<0?.8:1.25));this.window=[anchor-width*fraction,anchor+width*(1-fraction)];this.draw();},{passive:false});this.plots.push(plot);}
    this.observer=new ResizeObserver(()=>this.resize());this.observer.observe(this.grid);
  }
  update(event){const rows=[...(event.before?.rows||[]),...(event.after?.rows||[])].sort((a,b)=>a[0]-b[0]);const unique=rows.filter((r,i)=>!i||r[0]!==rows[i-1][0]);const data=[[],[],[],[],[]];let gaps=0,previous=null;for(const r of unique){if(previous&&r[0]-previous[0]>Math.max(100,(r[3]||10)*3)){for(let i=0;i<5;i++)data[i].push(i===0?(previous[0]+r[0])/2000:null);gaps++;}data[0].push(r[0]/1000);data[1].push(r[1]);data[2].push(r[1]==null?null:r[1]-r[3]);data[3].push(r[2]);data[4].push(r[4]);previous=r;}this.data=data;const state={collecting:"故障后片段采集中（约 5 秒）",incomplete:"服务退出或重启前片段尚未采集完成",complete:"片段采集完成"}[event.fragment_state]||"历史片段";this.caption.textContent=state+" · "+unique.length+" 个显示样本"+(unique.length?" · "+new Date(unique[0][0]).toLocaleTimeString()+" 至 "+new Date(unique.at(-1)[0]).toLocaleTimeString():" · 覆盖范围内暂无周期样本")+" · "+(gaps?gaps+" 处时间缺口，曲线已断开":"无可识别时间缺口")+(event.before&&!event.after?" · 当前仅有故障前数据":"");this.grid.hidden=!unique.length;this.controls.hidden=!unique.length;this.draw();}
  draw(){if(!this.data?.[0].length)return;this.plots.forEach((plot,i)=>plot.batch(()=>{plot.setData([this.data[0],this.data[i+1]],false);plot.setScale("x",{min:this.window?.[0]??this.data[0][0],max:this.window?.[1]??Math.max(this.data[0][0]+.001,this.data[0].at(-1))});plot.setScale("y",{min:null,max:null});}));}
  resize(){this.plots.forEach(plot=>{const width=plot.root.parentElement.clientWidth;if(width)plot.setSize({width,height:150});});}
  destroy(){this.observer.disconnect();for(const plot of this.plots)plot.destroy();this.box.remove();}
}
