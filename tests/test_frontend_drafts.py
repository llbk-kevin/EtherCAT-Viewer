"""前端草稿状态机使用实际 JavaScript；没有 Node 时仅跳过该可选运行器。"""
import shutil
import subprocess
from pathlib import Path
import pytest


def test_frontend_drafts_keep_local_edits_and_merge_unrelated_updates():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 运行前端状态机回归")
    script = r"""
const assert=require('node:assert/strict');
const {DraftStore}=require('./viewer/static/drafts.js');
const data={hardware:{period_ms:1,receive_timeout_us:2000},bridge:{cards:[{id:'stable',start_on_boot:false}]}};
const s=new DraftStore(data),p=['hardware','period_ms'];
s.entry(p);s.edit(p,2);
for(let i=0;i<20;i++)s.sync(structuredClone(data));
assert.equal(s.entry(p).draft,2);assert.equal(s.entry(p).conflict,false);
const other=structuredClone(data);other.hardware.receive_timeout_us=3000;s.sync(other);
assert.equal(s.entry(p).draft,2);assert.equal(s.request(p).base,1);
const conflict=structuredClone(other);conflict.hardware.period_ms=5;s.sync(conflict);
assert.equal(s.entry(p).draft,2);assert.equal(s.entry(p).conflict,true);
s.rebase(p);assert.equal(s.request(p).base,5);
const request=s.request(p);s.edit(p,3);
const accepted=structuredClone(conflict);accepted.hardware.period_ms=2;s.accepted(accepted,[request]);
assert.equal(s.entry(p).draft,3);assert.equal(s.entry(p).dirty,true);assert.equal(s.entry(p).conflict,false);
s.discard(p);assert.equal(s.entry(p).draft,2);assert.equal(s.dirty,false);
const boot=['bridge','cards','stable','start_on_boot'];s.entry(boot);s.edit(boot,true);assert.equal(s.request(boot).base,false);
"""
    subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True, encoding="utf-8", timeout=10)
