// 在 ego-browser nodejs 中导入并调用；生产页面只使用正常 UI 操作。
import assert from "node:assert/strict";

export async function mixedCardCycles(page, rounds = 20) {
  const count = await page.evaluate(() => document.querySelectorAll("#bridge-config > details").length);
  const summaries = await page.evaluate(() => [...document.querySelectorAll("#bridge-config > details > summary")].map(n=>n.textContent));
  const savedCards=async()=>JSON.parse((await page.fetch("/api/config")).body).config.bridge.cards;
  const baseline=await savedCards();
  const save=async()=>{await page.click("#bridge-save");await page.waitForFunction(()=>document.getElementById("bridge-config-state").textContent.startsWith("配置已保存"));};
  const last = "#bridge-config > details:last-child";
  const clickAction=async(text)=>{await page.evaluate(label=>{const card=document.querySelector("#bridge-config > details:last-child");[...card.querySelectorAll("button")].find(b=>b.textContent===label).scrollIntoView({block:"center"});},text);await page.click(last+' button:text-is("'+text+'")');};
  for (let i=0;i<rounds;i++) {
    await page.click("#bridge-add");
    await page.selectOption(last+' [data-field="协议类型"]',"extended");
    await page.fill(last+' [data-field="AI 数量"]',"6");
    await page.press(last+' [data-field="AI 数量"]',"Tab");
    await page.fill(last+' [data-field="AO 数量"]',"3");
    await page.press(last+' [data-field="AO 数量"]',"Tab");
    await page.selectOption(last+' [data-field="AI0"]',{index:1});
    await page.selectOption(last+' [data-field="AO1"]',{index:1});
    await clickAction("复制为只读卡");
    await page.selectOption(last+' [data-field="协议类型"]',"legacy");
    const copied = await page.evaluate(() => {
      const n=document.querySelector("#bridge-config > details:last-child");
      return {ai:[...n.querySelectorAll("[data-field]")].filter(x=>/^AI\d+$/.test(x.dataset.field)).length,
              ao:[...n.querySelectorAll("[data-field]")].filter(x=>/^AO\d+$/.test(x.dataset.field)).length,
              permissions:[...n.querySelectorAll('[data-field="权限"]')].map(x=>x.value),
              firstAI:n.querySelector('[data-field="AI0"]').value,firstAO:n.querySelector('[data-field="AO1"]').value};
    });
    assert.equal(copied.ai,8);assert.equal(copied.ao,2);assert.deepEqual(copied.permissions,["read","read"]);
    assert.ok(copied.firstAI);assert.ok(copied.firstAO);
    const ids=await page.evaluate(()=>[...document.querySelectorAll("#bridge-config > details")].slice(-2).map(n=>n.dataset.id));
    await save();
    const persisted=(await savedCards()).slice(-2);
    assert.deepEqual(persisted.map(c=>c.id),ids);
    assert.equal(persisted[0].ai.length,6);assert.equal(persisted[0].ao.length,3);
    assert.equal(persisted[1].ai.length,8);assert.equal(persisted[1].ao.length,2);
    assert.equal(persisted[0].ai[0],copied.firstAI);assert.equal(persisted[0].ao[0],copied.firstAO);
    assert.ok(persisted[1].ao_writable.every(v=>!v));
    await clickAction("删除本卡");
    await clickAction("删除本卡");
    await save();
    assert.deepEqual(await savedCards(),baseline);
    assert.equal(await page.evaluate(()=>document.querySelectorAll("#bridge-config > details").length),count);
  }
  assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll("#bridge-config > details > summary")].map(n=>n.textContent)),summaries);
  return {rounds,baselineCards:count,passed:true};
}

export async function refreshKeepsConnection(page, repeats=3) {
  for(let i=0;i<repeats;i++) {
    await page.reload();
    await page.waitForFunction(()=>document.getElementById("connection").textContent.includes("OP")&&document.getElementById("management-state").textContent.includes("本页正在管理"));
    assert.ok(await page.evaluate(()=>document.querySelector("#adapters").value));
    assert.equal(await page.evaluate(()=>document.querySelector("#adapters").disabled),false);
  }
  return {repeats,passed:true};
}
