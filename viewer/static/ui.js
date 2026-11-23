"use strict";
const uiIcons={
  network:'<path d="M4 8h16v12H4zM7 8V4h10v4M8 16v4m4-4v4m4-4v4"/>',
  user:'<circle cx="12" cy="8" r="3"/><path d="M5 20v-3c0-5 14-5 14 0v3"/>',
  rack:'<path d="M3 4h5v16H3zm7 0h5v16h-5zm7 0h4v16h-4"/>',
  transfer:'<path d="M3 8h17m-4-4 4 4-4 4M21 16H4m4-4-4 4 4 4"/>',
  bell:'<path d="M5 16h14l-2-4V8a5 5 0 0 0-10 0v4zm5 4h4"/>',
  // Lucide rotate-cw（ISC）；对应原图与许可保存在 vendor/lucide/。
  refresh:'<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
};
function uiIcon(name){const box=document.createElement("span");box.className="ui-icon";box.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+(uiIcons[name]||uiIcons.rack)+'</svg>';return box;}
for(const box of document.querySelectorAll("[data-icon]"))box.replaceChildren(uiIcon(box.dataset.icon));
function infoButton(help){const b=document.createElement("button");b.type="button";b.className="info";b.textContent="i";b.dataset.help=help;b.setAttribute("aria-label","查看说明");return b;}
for(const button of document.querySelectorAll(".info[data-help]")){const text=button.parentElement.textContent.replace(/\s*i\s*$/,"").trim();button.setAttribute("aria-label","查看"+text+"说明");}
function moduleIdentity(position,name){return "s"+position+":"+name;}
function modulePicture(name){
  if(!["EK1100","EL3104","EL4134"].includes(name))return uiIcon("rack");
  const image=document.createElement("img");image.className="module-picture";image.src="/static/images/modules/"+name.toLowerCase()+".png";image.alt=name+" 产品图";return image;
}
function revealInStrip(strip,item){if(!strip||!item||!strip.getClientRects().length)return;const a=strip.getBoundingClientRect(),b=item.getBoundingClientRect();if(b.left<a.left+4)strip.scrollLeft+=b.left-a.left-4;else if(b.right>a.right-4)strip.scrollLeft+=b.right-a.right+4;}
function wireTabKeys(parent){parent.onkeydown=event=>{const current=event.target.closest('[role="tab"]');if(!current)return;const tabs=[...parent.querySelectorAll('[role="tab"]')],index=tabs.indexOf(current);let next;
  if(event.key==="ArrowRight")next=(index+1)%tabs.length;else if(event.key==="ArrowLeft")next=(index+tabs.length-1)%tabs.length;else if(event.key==="Home")next=0;else if(event.key==="End")next=tabs.length-1;else return;
  event.preventDefault();tabs[next].click();document.getElementById(tabs[next].id)?.focus();
};}
class Notifications{
  constructor(){this.items=[];this.active=new Map();this.next=0;const button=document.getElementById("notification-toggle"),panel=document.getElementById("notification-history");
    button.setAttribute("popovertarget",panel.id);button.setAttribute("aria-label","查看通知历史");
    panel.addEventListener("beforetoggle",event=>{if(event.newState==="open")this.placeHistory();});
    panel.addEventListener("toggle",event=>{button.setAttribute("aria-expanded",String(event.newState==="open"));if(event.newState==="open"){this.items.forEach(i=>i.read=true);this.history();}});
    document.getElementById("notification-history-close").onclick=()=>{panel.hidePopover();button.focus();};
    const reposition=()=>{if(panel.matches(":popover-open"))this.placeHistory();};window.addEventListener("resize",reposition);window.addEventListener("scroll",reposition,true);
  }
  placeHistory(){const panel=document.getElementById("notification-history"),button=document.getElementById("notification-toggle"),nav=document.querySelector(".main-nav");let top=Math.max(12,button.getBoundingClientRect().bottom+10,nav.getBoundingClientRect().bottom+10);
    for(const zero of document.querySelectorAll(".global-zero")){const r=zero.getBoundingClientRect();if(r.width&&r.bottom>0&&r.top<innerHeight)top=Math.max(top,r.bottom+10);}
    panel.style.top=Math.min(top,innerHeight-100)+"px";panel.style.maxHeight=Math.max(80,innerHeight-top-12)+"px";
  }
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
