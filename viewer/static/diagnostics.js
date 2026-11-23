"use strict";
class DiagnosticCharts {
  constructor(visible){this.visible=visible;this.width=30;this.end=null;this.plots=[];this.busy=false;this.events=[];
    for(const[id,label,color]of[["interval-chart","间隔",traceColors[0]],["jitter-chart","偏差",traceColors[1]],["roundtrip-chart","耗时",traceColors[2]],["wkc-chart","WKC",traceColors[3]]]){
      const box=document.getElementById(id);const plot=new uPlot({width:Math.max(1,box.clientWidth),height:170,legend:{show:false},scales:{x:{time:true}},axes:[{stroke:"#354b64",values:(_u,values)=>values.map(v=>new Date(v*1000).toLocaleTimeString("zh-CN",{hour12:false})),grid:{stroke:"#e7edf4"}},{stroke:"#354b64",size:55,grid:{stroke:"#e7edf4"}}],series:[{},{label,stroke:color,width:1.5,spanGaps:false}],cursor:{drag:{x:true,y:false,setScale:false,dist:6}},hooks:{setSelect:[u=>{if(u.select.width<6)return;const a=u.posToVal(u.select.left,"x"),b=u.posToVal(u.select.left+u.select.width,"x");u.setSelect({left:0,top:0,width:0,height:0},false);this.width=Math.max(.05,b-a);this.end=b;this.update();}],draw:[u=>{const ctx=u.ctx;ctx.save();ctx.strokeStyle="#bc706780";for(const e of this.events){const x=u.valToPos(e.time_ms/1000,"x",true);if(x>=u.bbox.left&&x<=u.bbox.left+u.bbox.width){ctx.beginPath();ctx.moveTo(x,u.bbox.top);ctx.lineTo(x,u.bbox.top+u.bbox.height);ctx.stroke();}}ctx.restore();}]}},[[],[]],box);
      plot.over.addEventListener("wheel",e=>{e.preventDefault();const fraction=(e.clientX-plot.over.getBoundingClientRect().left)/Math.max(1,plot.over.clientWidth);this.zoom(e.deltaY<0?.8:1.25,fraction);},{passive:false});
      plot.over.addEventListener("dblclick",()=>this.follow());new ResizeObserver(()=>{if(box.clientWidth)plot.setSize({width:box.clientWidth,height:170});}).observe(box);this.plots.push(plot);
    }
    document.getElementById("diag-zoom-in").onclick=()=>this.zoom(.5);document.getElementById("diag-zoom-out").onclick=()=>this.zoom(2);document.getElementById("diag-follow").onclick=()=>this.follow();
    document.getElementById("event-filter").onchange=()=>this.renderEvents();setInterval(()=>this.update(),1000);
  }
  follow(){this.end=null;this.width=30;this.update();}
  zoom(factor,anchor=.5){const end=this.end??Date.now()/1000,point=end-this.width+this.width*anchor;this.width=Math.max(.05,Math.min(1800,this.width*factor));this.end=point+this.width*(1-anchor);this.update();}
  async update(){if(!this.visible()||this.busy)return;this.busy=true;try{const end=this.end??Date.now()/1000,start=end-this.width;
      const response=await fetch('/api/diagnostics/history?start='+start*1000+'&end='+end*1000+'&limit=4000');if(!response.ok)throw Error("诊断历史读取失败");const data=await response.json();
      const x=data.rows.map(r=>r[0]/1000),ys=[data.rows.map(r=>r[1]),data.rows.map(r=>r[1]==null?null:r[1]-r[3]),data.rows.map(r=>r[2]),data.rows.map(r=>r[4])];
      this.plots.forEach((u,i)=>u.batch(()=>{u.setData([x,ys[i]],false);u.setScale("x",{min:start,max:end});u.setScale("y",{min:null,max:null});}));
      document.getElementById("diag-window").textContent=this.width.toFixed(this.width<1?2:0)+" 秒 · "+(this.end===null?"实时":"历史");
      const events=await fetch("/api/events").then(r=>r.json());this.events=events.events;this.renderEvents();document.getElementById("journal-error").textContent=events.error||"";
    }catch(error){document.getElementById("journal-error").textContent=error.message;}finally{this.busy=false;}}
  renderEvents(){const parent=document.getElementById("event-list"),filter=document.getElementById("event-filter").value,rows=this.events.filter(e=>!filter||e.category===filter).slice(-100).reverse();
    const key=JSON.stringify(rows.map(e=>e.id))+filter;if(parent.dataset.key===key)return;parent.dataset.key=key;parent.replaceChildren();
    for(const e of rows){const row=document.createElement("details");row.className="event severity-"+e.severity;const summary=document.createElement("summary");summary.textContent=new Date(e.time_ms).toLocaleTimeString()+" · "+({connection:"连接",runtime:"运行",alarm:"报警",control:"控制"}[e.category]||e.category)+" · "+e.message;row.append(summary);
      const p=document.createElement("p");p.className="muted";p.textContent=(e.wkc!=null?"WKC "+e.wkc+" / "+e.expected_wkc+" · ":"")+(e.nic?.items?.map(i=>i.name+" "+(i.status==="Up"?"链路正常":"链路断开")+" "+i.speed).join("；")||"")+(e.devices?" · 从站："+e.devices.map(d=>d.name+" "+({"0x08":"运行 OP","0x04":"安全运行 SAFEOP","0x02":"预运行 PREOP","0x01":"初始化 INIT"}[d.state]||"状态异常")).join(" → "):"");row.append(p);
      for(const slave of e.diagnostic?.slaves||[]){const d=document.createElement("p");d.className="muted";d.textContent=slave.name+" · CRC/接收错误 "+(slave.errors?slave.errors.slice(0,8).join(" / "):"未知")+" · Lost Link "+(slave.lost_link?.join(" / ")??"未知");row.append(d);}parent.append(row);
    }if(!rows.length)parent.textContent="当前分类暂无事件。";
  }
}
