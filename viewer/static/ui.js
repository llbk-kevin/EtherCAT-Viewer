"use strict";
const uiIcons={
  network:'<path d="M4 8h16v12H4zM7 8V4h10v4M8 16v4m4-4v4m4-4v4"/>',
  user:'<circle cx="12" cy="8" r="3"/><path d="M5 20v-3c0-5 14-5 14 0v3"/>',
  rack:'<path d="M3 4h5v16H3zm7 0h5v16h-5zm7 0h4v16h-4"/>',
  transfer:'<path d="M3 8h17m-4-4 4 4-4 4M21 16H4m4-4-4 4 4 4"/>',
  bell:'<path d="M5 16h14l-2-4V8a5 5 0 0 0-10 0v4zm5 4h4"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M19 12a7 7 0 0 0-12-5L4 10m16 4-3 3A7 7 0 0 1 5 12"/>'
};
function uiIcon(name){const box=document.createElement("span");box.className="ui-icon";box.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+(uiIcons[name]||uiIcons.rack)+'</svg>';return box;}
for(const box of document.querySelectorAll("[data-icon]"))box.replaceChildren(uiIcon(box.dataset.icon));
function infoButton(help){const b=document.createElement("button");b.type="button";b.className="info";b.textContent="i";b.dataset.help=help;b.setAttribute("aria-label","查看说明");return b;}
class Notifications{
  constructor(){this.items=[];this.active=new Map();this.next=0;document.getElementById("notification-toggle").onclick=()=>{const panel=document.getElementById("notification-history");panel.hidden=!panel.hidden;if(!panel.hidden){this.items.forEach(i=>i.read=true);this.history();}};}
  show(message,error=false){if(!message)return;const key=(error?"error":"success")+message;let record=this.active.get(key);
    if(record){record.count++;record.time=Date.now();record.text.textContent=message+"（"+record.count+" 次）";this.history();return;}
    record={id:++this.next,message,error,count:1,time:Date.now(),read:false};this.items.push(record);if(this.items.length>100)this.items.shift();
    const node=document.createElement("div");node.className="notification "+(error?"notification-error":"notification-success");node.setAttribute("role",error?"alert":"status");
    const mark=document.createElement("span");mark.className="notification-mark";mark.textContent=error?"!":"✓";
    const text=document.createElement("span");text.textContent=message;const close=document.createElement("button");close.type="button";close.className="notification-close";close.setAttribute("aria-label","关闭通知");close.textContent="×";
    const remove=()=>{node.remove();this.active.delete(key);};close.onclick=remove;node.append(mark,text,close);document.getElementById("notifications").append(node);
    record.node=node;record.text=text;this.active.set(key,record);if(!error)setTimeout(remove,4000);this.history();
  }
  history(){const badge=document.getElementById("notification-count"),unread=this.items.filter(i=>!i.read).length;badge.textContent=String(unread);badge.hidden=!unread;const parent=document.getElementById("notification-items");parent.replaceChildren();
    for(const i of this.items.slice(-30).reverse()){const row=document.createElement("p");row.className=i.error?"notification-error-text":"";row.textContent=new Date(i.time).toLocaleTimeString()+" · "+i.message+(i.count>1?"（"+i.count+" 次）":"");parent.append(row);}if(!parent.children.length)parent.textContent="暂无通知";
  }
}
