// EGO 和 Edge 共用场景；只通过页面控件及公开只读接口核对结果。
import assert from "node:assert/strict";
const json=(page,url)=>page.evaluate(async u=>{const r=await fetch(u);if(!r.ok)throw Error("读取失败 "+r.status);return r.json();},url);
const cards=async page=>(await json(page,"/api/config")).config.bridge.cards;
export async function mixedCardCycles(page,rounds=20){
  await page.click("[data-page=bridge]");
  const baseline=await cards(page),count=baseline.length;
  const panel="#bridge-card-panel";
  const save=async()=>{await page.click("#bridge-save");await page.waitForFunction(()=>document.getElementById("bridge-save").disabled&&!document.getElementById("bridge-config-state").textContent.includes("未保存"));};
  const action=text=>page.click(panel+' button:text-is("'+text+'")');
  const selected=()=>page.evaluate(()=>document.querySelector("#bridge-card-panel")?.dataset.card);
  for(let i=0;i<rounds;i++){
    await page.click("#bridge-add");const original=await selected();
    await page.fill(panel+' [data-field="名称"]',"回归卡 "+i);
    await page.selectOption(panel+' [data-field="协议类型"]',"extended");
    for(const[kind,size]of[["AI",6],["AO",3]]){await page.fill(panel+' [data-field="'+kind+' 数量"]',String(size));await page.press(panel+' [data-field="'+kind+' 数量"]',"Tab");}
    await page.selectOption(panel+' [data-field="AI0"]',{index:1});await page.selectOption(panel+' [data-field="AO1"]',{index:1});
    await action("复制为只读卡");const copy=await selected();assert.notEqual(copy,original);
    await page.selectOption(panel+' [data-field="协议类型"]',"legacy");
    const mapped=await page.evaluate(()=>{const n=document.querySelector("#bridge-card-panel");return{ai:n.querySelectorAll('[data-field^="AI"]').length,ao:n.querySelectorAll('[data-field^="AO"]').length,permissions:[...n.querySelectorAll('[data-field="权限"]')].map(x=>x.value),firstAI:n.querySelector('[data-field="AI0"]').value,firstAO:n.querySelector('[data-field="AO1"]').value};});
    assert.equal(mapped.ai,8);assert.equal(mapped.ao,2);assert.deepEqual(mapped.permissions,["read","read"]);
    // 标签切换不能丢掉另一张卡未失焦的名称和映射。
    await page.click('#tab-'+original);assert.equal(await page.evaluate(()=>document.querySelector('[data-field="名称"]').value),"回归卡 "+i);
    await page.click('#tab-'+copy);await save();
    const persisted=(await cards(page)).slice(-2);assert.deepEqual(persisted.map(c=>c.id),[original,copy]);assert.equal(persisted[0].ai.length,6);assert.equal(persisted[0].ao.length,3);assert.equal(persisted[1].ai.length,8);assert.ok(persisted[1].ao_writable.every(v=>!v));assert.equal(persisted[0].ai[0],mapped.firstAI);assert.equal(persisted[0].ao[0],mapped.firstAO);
    await action("删除本卡");assert.equal(await selected(),original);await action("删除本卡");await save();
    assert.deepEqual(await cards(page),baseline);assert.equal(await page.evaluate(()=>document.querySelectorAll("#bridge-tabs [role=tab]").length),count);
  }
  return{rounds,baselineCards:count,passed:true};
}
export async function periodDraftRegression(page){
  await page.click("[data-page=diagnostics]");
  const before=(await json(page,"/api/config")).config.hardware.period_ms,target=before===2?3:2;
  await page.evaluate(()=>{window.__requests=[];if(!window.__originalFetch){window.__originalFetch=window.fetch;window.fetch=async(...args)=>{if(args[0]==="/api/commands"&&args[1]?.body)window.__requests.push(JSON.parse(args[1].body));return window.__originalFetch(...args);};}});
  const selector='[data-path="hardware/period_ms"] input',button='[data-path="hardware/period_ms"] .apply-setting';
  await page.fill(selector,String(target));await page.press(selector,"Tab");
  await page.click("[data-page=system]");await page.click("[data-page=diagnostics]");
  await page.waitForFunction(()=>{const c=document.querySelector('[data-path="hardware/period_ms"]');return c?.querySelector(".apply-setting")&&!c.querySelector(".apply-setting").disabled;});
  // 等待至少多次真实推送，而不是用人工修改 DOM 代替遥测。
  await page.waitForFunction(()=>window.__requests.filter(r=>r.action==="preview_settings").length>=1);
  assert.equal(await page.evaluate(()=>document.querySelector('[data-path="hardware/period_ms"] input').value),String(target));
  await page.click(button);await page.waitForFunction(t=>document.querySelector('[data-path="hardware/period_ms"] .setting-values').textContent.startsWith("已保存："+t),target);
  const request=await page.evaluate(()=>window.__requests.filter(r=>r.action==="apply_settings").at(-1));assert.equal(request.args.changes.length,1);assert.deepEqual(request.args.changes[0].path,["hardware","period_ms"]);assert.equal(request.args.changes[0].value,target);
  const after=(await json(page,"/api/config")).config;assert.equal(after.hardware.period_ms,target);
  // 键盘提交同样核对恢复值，且只修改周期。
  await page.fill(selector,String(before));await page.press(selector,"Tab");await page.waitForFunction(()=>!document.querySelector('[data-path="hardware/period_ms"] .apply-setting').disabled);
  await page.focus(button);await page.press(button,"Enter");await page.waitForFunction(t=>document.querySelector('[data-path="hardware/period_ms"] .setting-values').textContent.startsWith("已保存："+t),before);
  return{before,target,requestsChecked:2,passed:true};
}
export async function refreshKeepsConnection(page,repeats=3){
  const before=await json(page,"/api/status");
  for(let i=0;i<repeats;i++){await page.reload();await page.waitForFunction(()=>document.getElementById("management-state").textContent==="本页管理");await page.waitForFunction(()=>document.querySelector("#adapters").value!=="");assert.equal(await page.evaluate(()=>document.querySelector("#adapters").disabled),false);const after=await json(page,"/api/status");assert.equal(after.session,before.session);assert.equal(after.connection,before.connection);}
  return{repeats,passed:true};
}

export async function fieldIsolationAndRetry(page){
  await page.click("[data-page=diagnostics]");
  const before=(await json(page,"/api/config")).config,period=before.hardware.period_ms,timeout=before.hardware.receive_timeout_us,target=timeout===2100?2200:2100;
  const row='[data-path="hardware/receive_timeout_us"]',periodInput='[data-path="hardware/period_ms"] input';
  await page.fill(periodInput,String(period===11?12:11));await page.press(periodInput,"Tab");
  await page.fill(row+" input",String(target));await page.press(row+" input","Tab");
  await page.waitForFunction(()=>!document.querySelector('[data-path="hardware/receive_timeout_us"] .apply-setting').disabled);
  await page.evaluate(()=>{const normal=window.fetch;window.__failApplyOnce=true;window.fetch=async(...args)=>{if(window.__failApplyOnce&&args[0]==="/api/commands"&&JSON.parse(args[1]?.body||"{}").action==="apply_settings"){window.__failApplyOnce=false;return new Response(JSON.stringify({detail:"验收注入：请求暂时失败"}),{status:503,headers:{"content-type":"application/json"}});}return normal(...args);};});
  await page.click(row+" .apply-setting");
  await page.waitForFunction(()=>document.querySelector('[data-path="hardware/receive_timeout_us"] .field-error').textContent.includes("验收注入"));
  assert.equal(await page.evaluate(()=>document.querySelector('[data-path="hardware/receive_timeout_us"] input').value),String(target));
  await page.click(row+" .apply-setting");await page.waitForFunction(t=>document.querySelector('[data-path="hardware/receive_timeout_us"] .setting-values').textContent.startsWith("已保存："+t),target);
  const after=(await json(page,"/api/config")).config;assert.equal(after.hardware.period_ms,period);assert.equal(after.hardware.receive_timeout_us,target);
  await page.fill(row+" input",String(timeout));await page.press(row+" input","Tab");await page.waitForFunction(()=>!document.querySelector('[data-path="hardware/receive_timeout_us"] .apply-setting').disabled);await page.click(row+" .apply-setting");await page.waitForFunction(t=>document.querySelector('[data-path="hardware/receive_timeout_us"] .setting-values').textContent.startsWith("已保存："+t),timeout);
  await page.fill(periodInput,String(period));await page.press(periodInput,"Tab");
  return{unrelatedDraftPreserved:true,retryPassed:true,restored:true};
}

export async function twoPointPreviewRace(page){
  await page.click("[data-page=calibration]");const aiModule=(await json(page,"/api/config")).config.topology.channels.ai[0].id.split(":").slice(0,-1).join(":");await page.click('#module-tabs [data-module="'+aiModule+'"]');
  const id=await page.evaluate(()=>document.querySelector('#channel-settings .channel-profile:not([hidden])').dataset.channel),first='#channel-settings [data-channel="'+id+'"]';
  // 通过页面实际展开，不改生产状态；两点预览不应用校准。
  await page.click(first+' [data-function="calibration"]');await page.selectOption(first+" .calibration-method select","two-point");
  const box=first+" .two-point-calculation",values=[0,0,1,2];
  for(let i=0;i<4;i++)await page.fill(box+" input >> nth="+i,String(values[i]));
  await page.evaluate(()=>{window.__twoPointReply=false;const original=window.fetch;window.__twoPointOriginal=original;window.fetch=async(...args)=>{const response=await original(...args);if(args[0]==="/api/commands"&&JSON.parse(args[1]?.body||"{}").action==="calibration_two_point"){await new Promise(resolve=>setTimeout(resolve,1000));window.__twoPointReply=true;}return response;};});
  try{await page.click(box+' button:text-is("计算并预览")');await page.fill(box+" input >> nth=3","1");await page.waitForFunction(()=>window.__twoPointReply);
    assert.equal(await page.evaluate(()=>{const box=document.querySelector('#channel-settings .channel-profile:not([hidden]) .two-point-calculation');return [...box.querySelectorAll("button")].some(b=>b.textContent.includes("应用两点")&&!b.hidden);}),false);
    assert.ok((await page.evaluate(()=>document.querySelector('#channel-settings .channel-profile:not([hidden]) .two-point-calculation .setting-plan').textContent)).includes("重新计算"));
  }finally{await page.evaluate(()=>window.fetch=window.__twoPointOriginal);}
  return{lateReplyIgnored:true,configurationUnchanged:true};
}

export async function moduleCardsAndDrafts(page){
  const before=(await json(page,"/api/config")).config,channels=before.topology.channels.ai;
  assert.ok(channels.length);const module=channels[0].id.split(":").slice(0,-1).join(":"),prefix='#channel-settings [data-module="'+module+'"]';
  await page.click('[data-page="calibration"]');await page.click('#module-tabs [data-module="'+module+'"]');
  assert.equal(await page.evaluate(m=>[...document.querySelectorAll('.channel-profile')].filter(n=>!n.hidden&&n.dataset.module===m).length,module),channels.filter(c=>c.id.startsWith(module+":")).length);
  const first=prefix+' >> nth=0',second=prefix+' >> nth=1';
  // 四卡常驻、每卡独立功能页；切模块与主导航都不写入配置。
  const card0='#channel-settings [data-channel="'+channels[0].id+'"]',card1='#channel-settings [data-channel="'+channels[1].id+'"]';
  await page.click(card0+' [data-function="engineering"]');
  await page.fill(card0+' [data-field="物理量单位"]',"回归单位");
  await page.fill(card1+' [data-field="通道名称"]',"回归草稿");
  await page.click('[data-page="ethercat"]');await page.click('#topology [data-module="'+module+'"]');
  assert.equal(await page.evaluate(s=>document.querySelector(s+' [data-field="物理量单位"]').value,card0),"回归单位");
  assert.equal(await page.evaluate(s=>document.querySelector(s+' [data-field="通道名称"]').value,card1),"回归草稿");
  assert.deepEqual(await page.evaluate(s=>[...document.querySelectorAll(s+' [role=tab][aria-selected=true]')].map(n=>n.dataset.function),prefix),["engineering","calibration","calibration","calibration"]);
  assert.equal(await page.evaluate(()=>[...document.querySelectorAll('[data-view="calibration"] .setting-values')].filter(n=>n.getBoundingClientRect().height>0&&n.textContent.startsWith('已保存：')).length),0);
  await page.fill(card0+' [data-field="物理量单位"]',before.channels[channels[0].id].engineering.unit);await page.press(card0+' [data-field="物理量单位"]',"Tab");
  await page.fill(card1+' [data-field="通道名称"]',before.channels[channels[1].id].name);await page.press(card1+' [data-field="通道名称"]',"Tab");
  const coupler=before.topology.devices.findIndex(d=>d.name==="EK1100");if(coupler>=0){await page.click('#module-tabs [data-module="s'+(coupler+1)+':EK1100"]');assert.equal(await page.evaluate(()=>[...document.querySelectorAll('.channel-profile')].filter(n=>!n.hidden).length),0);}
  await page.click('#module-tabs [data-module="'+module+'"]');assert.equal((await json(page,"/api/config")).config.revision,before.revision);
  return{independentTabs:true,draftsPreserved:true,noConfigurationWrites:true};
}

export async function notificationHistoryInteractions(page){
  await page.click('[data-page="bridge"]');await page.click('#notification-toggle');
  assert.equal(await page.evaluate(()=>document.querySelector('#notification-history').matches(':popover-open')),true);
  const overlap=await page.evaluate(()=>{const a=document.querySelector('#notification-history').getBoundingClientRect(),b=[...document.querySelectorAll('.global-zero')].find(n=>n.getBoundingClientRect().width)?.getBoundingClientRect();return !!b&&a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;});assert.equal(overlap,false);
  await page.click('#notification-history-close');assert.equal(await page.evaluate(()=>document.activeElement.id),"notification-toggle");
  await page.click('#notification-toggle');await page.click('[data-page="system"]');assert.equal(await page.evaluate(()=>document.querySelector('#notification-history').matches(':popover-open')),false);
  await page.click('#notification-toggle');await page.press('#notification-toggle',"Escape");assert.equal(await page.evaluate(()=>document.querySelector('#notification-history').matches(':popover-open')),false);
  return{closeButton:true,outsideDismiss:true,escape:true,safetyActionVisible:true};
}

export async function instantSwitchInteractions(page){
  await page.click('[data-page="system"]');const initial=(await json(page,"/api/config")).config,original=initial.startup.open_browser,row='[data-path="startup/open_browser"]';
  await page.click(row+' .setting-label');assert.equal((await json(page,"/api/config")).config.revision,initial.revision);
  assert.equal(await page.evaluate(s=>document.querySelector(s+' input').checked,row),original);
  await page.evaluate(()=>{window.__instantOriginal=window.fetch;window.__instantMutations=[];window.__instantObserver=new MutationObserver(()=>{const row=document.querySelector('[data-path="startup/open_browser"]');if(row.classList.contains('has-draft')||!document.querySelector('#config-warning').hidden)window.__instantMutations.push('draft');});window.__instantObserver.observe(document.body,{subtree:true,attributes:true,childList:true});window.fetch=async(...args)=>{if(args[0]==='/api/commands'&&JSON.parse(args[1]?.body||'{}').action==='apply_settings')await new Promise(r=>setTimeout(r,250));return window.__instantOriginal(...args);};});
  try{
    await page.click(row+' .switch');await page.waitForFunction(()=>document.querySelector('[data-path="startup/open_browser"]').getAttribute('aria-busy')==='true');
    assert.equal(await page.evaluate(s=>document.querySelector(s+' input').disabled,row),true);
    await page.waitForFunction(()=>document.querySelector('[data-path="startup/open_browser"]').getAttribute('aria-busy')==='false');
    assert.equal((await json(page,"/api/config")).config.startup.open_browser,!original);
    assert.deepEqual(await page.evaluate(()=>window.__instantMutations),[]);
    // 注入请求失败；必须恢复后台确认值，并保留明确的重试入口。
    await page.evaluate(()=>{window.__failInstant=true;window.fetch=async(...args)=>{if(window.__failInstant&&args[0]==='/api/commands'&&JSON.parse(args[1]?.body||'{}').action==='apply_settings'){window.__failInstant=false;return new Response(JSON.stringify({detail:'即时保存回归注入'}),{status:503,headers:{'content-type':'application/json'}});}return window.__instantOriginal(...args);};});
    await page.click(row+' .switch');await page.waitForFunction(()=>document.querySelector('[data-path="startup/open_browser"] .field-error').textContent.includes('即时保存回归注入'));
    assert.equal(await page.evaluate(s=>document.querySelector(s+' input').checked,row),!original);
    await page.click(row+' .instant-retry');await page.waitForFunction(()=>document.querySelector('[data-path="startup/open_browser"]').getAttribute('aria-busy')==='false');
    assert.equal((await json(page,"/api/config")).config.startup.open_browser,original);
    assert.deepEqual(await page.evaluate(()=>window.__instantMutations),[]);
  }finally{await page.evaluate(()=>{window.fetch=window.__instantOriginal;window.__instantObserver.disconnect();});}
  return{capsuleOnly:true,noDraftFlash:true,retry:true,restored:true};
}

export async function managementTransfer(first,second){
  const before=await json(first,"/api/status");
  const manage=async page=>{if(await page.evaluate(()=>document.querySelector('#management-state').textContent==='本页只读'))await page.click('#management-button');await page.waitForFunction(()=>document.querySelector('#management-state').textContent==='本页管理');};
  await manage(first);await manage(second);await first.waitForFunction(()=>document.querySelector('#management-state').textContent==='本页只读');await manage(first);await second.waitForFunction(()=>document.querySelector('#management-state').textContent==='本页只读');
  const after=await json(first,"/api/status");assert.deepEqual(after.output_owners,before.output_owners);assert.deepEqual(after.targets,before.targets);assert.deepEqual(after.bridge.cards,before.bridge.cards);assert.equal(after.session,before.session);
  assert.equal(await first.evaluate(()=>document.querySelector('#claim')===null),true);
  return{singleManager:true,outputsAndTcpUnchanged:true};
}

export async function invalidCardCountDraft(page){
  await page.click('[data-page="bridge"]');const before=await cards(page);
  await page.click('#bridge-add');const panel='#bridge-card-panel';await page.selectOption(panel+' [data-field="协议类型"]',"extended");
  await page.fill(panel+' [data-field="AI 数量"]',"0");await page.press(panel+' [data-field="AI 数量"]',"Tab");
  await page.fill(panel+' [data-field="AO 数量"]',"1");await page.press(panel+' [data-field="AO 数量"]',"Tab");
  assert.equal(await page.evaluate(()=>document.querySelector('#bridge-card-panel [data-field="AI 数量"]').value),"0");
  assert.equal(await page.evaluate(()=>document.querySelector('#bridge-card-panel [data-field="AI 数量"]').getAttribute('aria-invalid')),"true");
  await page.click('#bridge-save');assert.deepEqual(await cards(page),before);
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.field),"AI 数量");
  // 切到另一张卡再回来，错误输入仍保留；修复后可以正常保存和删除。
  const id=await page.evaluate(()=>document.querySelector('#bridge-card-panel').dataset.card);await page.click('#tab-'+before[0].id);await page.click('#tab-'+id);
  assert.equal(await page.evaluate(()=>document.querySelector('#bridge-card-panel [data-field="AI 数量"]').value),"0");
  await page.fill(panel+' [data-field="AI 数量"]',"4");await page.press(panel+' [data-field="AI 数量"]',"Tab");await page.click('#bridge-save');
  await page.waitForFunction(()=>document.querySelector('#bridge-save').disabled&&!document.querySelector('#bridge-config-state').textContent.includes('未保存'));assert.equal((await cards(page)).find(c=>c.id===id).ai.length,4);
  await page.click(panel+' button:text-is("删除本卡")');await page.click('#bridge-save');await page.waitForFunction(()=>document.querySelector('#bridge-save').disabled&&!document.querySelector('#bridge-config-state').textContent.includes('未保存'));assert.deepEqual(await cards(page),before);
  return{invalidValuePreserved:true,saveBlocked:true,repairedAndRestored:true};
}

export async function selectedNavigationVisible(page){
  const origin=await page.evaluate(()=>location.origin);await page.goto(origin+'/#system');
  await page.waitForFunction(()=>{const strip=document.querySelector('.main-nav'),selected=strip.querySelector('[aria-current=page]'),a=strip.getBoundingClientRect(),b=selected.getBoundingClientRect();return selected.dataset.page==='system'&&b.left>=a.left&&b.right<=a.right;});
  await page.click('[data-page="ethercat"]');await page.waitForSelector('#topology .module-link');
  const id=await page.evaluate(()=>[...document.querySelectorAll('#topology .module-link')].at(-1).dataset.module),selector='#topology [data-module="'+id+'"]';
  await page.focus(selector);await page.press(selector,'Enter');
  await page.waitForFunction(id=>document.activeElement.id==='module-tab-'+id,id);
  assert.equal(await page.evaluate(()=>{const strip=document.querySelector('#module-tabs'),selected=strip.querySelector('[aria-selected=true]'),a=strip.getBoundingClientRect(),b=selected.getBoundingClientRect();return b.left>=a.left&&b.right<=a.right;}),true);
  await page.press('[id="module-tab-'+id+'"]','Tab');assert.equal(await page.evaluate(()=>document.activeElement.id==='import-reference'||!!document.activeElement.closest('.module-config,.channel-profile')),true);
  return{directUrlVisible:true,selectedModuleVisible:true,keyboardFocusTransferred:true};
}
