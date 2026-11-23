"""运行实际前端即时保存逻辑，覆盖超时、回读和并发请求边界。"""
import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {DraftStore,valueAt,same}=require('./viewer/static/drafts.js');
const context={settingValueAt:valueAt,settingSame:same,document:{activeElement:null,body:{}}};
vm.runInNewContext(fs.readFileSync('./viewer/static/settings.js','utf8')+'\nglobalThis.Panel=SettingsPanel;',context);
function setup(){
  const server={startup:{open_browser:false},hardware:{period_ms:10}};
  const panel=Object.create(context.Panel.prototype);
  panel.config=structuredClone(server);panel.store=new DraftStore(panel.config);
  panel.managed=()=>true;panel.snapshots=[];panel.commands=[];
  panel.paintControl=c=>panel.snapshots.push({dirty:c.entry.dirty,busy:c.busy});
  panel.accept=config=>{panel.config=structuredClone(config);panel.store.sync(panel.config);};
  panel.load=async()=>{panel.accept(server);return{config:structuredClone(server)};};
  panel.send=async(action,args)=>{panel.commands.push({action,args});if(action==='preview_settings')return{plan:{changes:args.changes}};server.startup.open_browser=args.changes[0].value;return{config:structuredClone(server)};};
  const c={entry:panel.store.entry(['startup','open_browser']),definition:{instant:true},busy:false};
  return{panel,c,server};
}
"""


def run_js(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 运行前端状态机回归")
    script = HARNESS + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    result = subprocess.run(
        [node, "-e", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_instant_save_does_not_create_drafts_or_submit_unrelated_edits():
    run_js(r"""
const {panel,c}=setup();panel.store.edit(['hardware','period_ms'],20);
await panel.applyInstant(c,true);
assert.equal(c.entry.dirty,false);assert.ok(panel.snapshots.every(s=>!s.dirty));
assert.equal(panel.config.startup.open_browser,true);assert.equal(c.busy,false);
assert.equal(panel.store.entry(['hardware','period_ms']).draft,20);
const request=panel.commands.at(-1).args.changes;assert.equal(request.length,1);
assert.equal(request[0].path.join('/'),'startup/open_browser');assert.equal(request[0].base,false);
""")


def test_instant_save_restores_keyboard_focus_without_stealing_new_focus():
    run_js(r"""
const {panel,c}=setup();let focused=0;
c.input={isConnected:true,disabled:false,getClientRects:()=>[{}],focus:()=>{focused++;context.document.activeElement=c.input;}};
context.document.activeElement=c.input;
panel.paintControl=c=>{c.input.disabled=c.busy;if(c.busy)context.document.activeElement=context.document.body;};
await panel.applyInstant(c,true);assert.equal(focused,1);assert.equal(context.document.activeElement,c.input);
const send=panel.send,other={};panel.send=async(...args)=>{context.document.activeElement=other;return send(...args);};
await panel.applyInstant(c,false);assert.equal(focused,1);assert.equal(context.document.activeElement,other);
""")


def test_instant_failure_restores_confirmed_value_and_allows_retry():
    run_js(r"""
const {panel,c}=setup(),send=panel.send;let fail=true;
panel.send=async(action,args)=>{if(action==='apply_settings'&&fail){fail=false;throw Error('暂时失败');}return send(action,args);};
await panel.applyInstant(c,true);assert.match(c.failure,/暂时失败/);
assert.equal(c.entry.draft,false);assert.equal(c.entry.dirty,false);assert.equal(c.uncertain,false);
await panel.applyInstant(c,c.instantLastValue);assert.equal(panel.config.startup.open_browser,true);assert.equal(c.failure,'');
""")


def test_instant_timeout_uses_readback_to_detect_completed_save():
    run_js(r"""
const {panel,c,server}=setup();panel.send=async(action,args)=>{if(action==='preview_settings')return{plan:{changes:args.changes}};server.startup.open_browser=true;throw Error('响应超时');};
await panel.applyInstant(c,true);assert.equal(c.failure,'');assert.equal(c.entry.draft,true);
assert.equal(c.entry.dirty,false);assert.equal(c.uncertain,false);
""")


def test_instant_unreachable_readback_remains_unknown_until_retry_confirms_base():
    run_js(r"""
const {panel,c}=setup(),load=panel.load,send=panel.send;panel.load=async()=>null;
panel.send=async(action,args)=>{if(action==='apply_settings')throw Error('连接中断');return send(action,args);};
await panel.applyInstant(c,true);assert.equal(c.uncertain,true);assert.match(c.failure,/状态无法确认/);
panel.load=load;panel.send=send;await panel.applyInstant(c,c.instantLastValue);
assert.equal(c.uncertain,false);assert.equal(c.failure,'');assert.equal(c.entry.draft,true);
""")


def test_instant_duplicate_request_is_ignored_while_pending():
    run_js(r"""
const {panel,c}=setup(),send=panel.send;let release;const gate=new Promise(r=>release=r);
panel.send=async(action,args)=>{if(action==='apply_settings')await gate;return send(action,args);};
const first=panel.applyInstant(c,true);await Promise.resolve();await panel.applyInstant(c,false);
assert.equal(c.busy,true);release();await first;
assert.equal(panel.commands.filter(c=>c.action==='apply_settings').length,1);assert.equal(c.entry.draft,true);
""")


def test_readonly_page_cannot_submit_instant_settings():
    run_js(r"""
const {panel,c}=setup();panel.managed=()=>false;await panel.applyInstant(c,true);
assert.equal(panel.commands.length,0);assert.equal(c.entry.draft,false);
""")


def test_regular_field_reports_success_and_retry_in_the_field_only():
    run_js(r"""
const {panel,server}=setup(),path=['hardware','period_ms'],entry=panel.store.edit(path,12);
const c={entry,definition:{path},busy:false};
panel.valid=()=>true;
const notifications=[];panel.notify=(...args)=>notifications.push(args);
let fail=true;
panel.send=async(action,args)=>{
  assert.equal(args.changes.length,1);
  if(action==='preview_settings')return{plan:{mode:'offline',changes:args.changes}};
  if(fail)throw Error('参数校验失败');
  server.hardware.period_ms=args.changes[0].value;return{config:server};
};
await panel.apply(c);assert.match(c.failure,/参数校验失败/);assert.equal(c.entry.draft,12);
fail=false;await panel.apply(c);assert.equal(c.failure,'');assert.equal(c.applied,true);
assert.equal(panel.config.hardware.period_ms,12);assert.equal(c.entry.dirty,false);
assert.deepEqual(notifications,[]);
""")


def test_two_point_record_renders_coefficients_from_saved_measurements():
    run_js(r"""
const result=context.measurementText({type:'two_point',x1:0,y1:-0.01,x2:1,y2:1.99});
assert.match(result,/增益 2 · 偏移 -0\.01 V/);
assert.doesNotMatch(result,/undefined|NaN/);
""")


def test_unsupported_import_clears_previously_selected_configuration():
    run_js(r"""
const {panel}=setup(),nodes=new Map();
context.document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);}};
let message='';panel.notify=text=>{message=text;};
const select=version=>panel.importFile({target:{files:[{text:async()=>JSON.stringify({
  version,hardware:{interface:'test'},bridge:{cards:[]}
})}]}});
await select(3);assert.equal(panel.importDraft.version,3);assert.equal(nodes.get('import-preview').hidden,false);
await select(2);assert.equal(panel.importDraft,null);assert.equal(nodes.get('import-preview').hidden,true);
assert.match(message,/配置版本必须为 3/);
""")


def test_inherited_default_policy_matches_readback_but_real_drift_is_visible():
    run_js(r"""
const {panel}=setup(),path=['faults','policies','bus','s3:EL4134:AO3'],key=path.join('/');
panel.controls=new Map([[key,{entry:{fallback:{action:'zero',voltage:0}}}]]);
panel.state={parameter_readback:{connected:true,values:{[key]:{action:'zero',voltage:0}}}};
assert.equal(panel.drift(path),false);
panel.state.parameter_readback.values[key]={action:'hold',voltage:0};assert.equal(panel.drift(path),true);
panel.config.faults={policies:{bus:{'s3:EL4134:AO3':{action:'hold',voltage:0}}}};
assert.equal(panel.drift(path),false);
""")
