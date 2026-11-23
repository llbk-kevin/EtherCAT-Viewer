"use strict";
(function(root){
  const clone=value=>value==null?null:JSON.parse(JSON.stringify(value));
  const same=(a,b)=>JSON.stringify(a??null)===JSON.stringify(b??null);
  function valueAt(config,path){let value=config;for(const key of path){value=Array.isArray(value)?value.find(c=>c.id===key):value?.[key];}return clone(value);}
  class DraftStore{
    constructor(config={}){this.config=config;this.entries=new Map();}
    entry(path,fallback=null){const key=path.join("/");if(!this.entries.has(key)){const base=valueAt(this.config,path);this.entries.set(key,{path:[...path],base,draft:base??clone(fallback),fallback:clone(fallback),dirty:false,conflict:false,serial:0});}return this.entries.get(key);}
    edit(path,value){const e=this.entry(path);e.draft=clone(value);e.dirty=!same(e.draft,e.base);e.conflict=!same(valueAt(this.config,path),e.base)&&!same(valueAt(this.config,path),e.draft);e.serial++;return e;}
    sync(config){this.config=config;for(const e of this.entries.values()){const current=valueAt(config,e.path);if(!e.dirty||same(current,e.draft)){e.base=current;e.draft=current??clone(e.fallback);e.dirty=false;e.conflict=false;}else e.conflict=!same(current,e.base);}}
    accepted(config,requests){for(const request of requests){const e=this.entry(request.path);e.base=valueAt(config,e.path);if(same(e.draft,request.value)){e.draft=e.base??clone(e.fallback);e.dirty=false;}else e.dirty=!same(e.draft,e.base);e.conflict=false;}this.sync(config);}
    discard(path){const e=this.entry(path);e.base=valueAt(this.config,path);e.draft=e.base??clone(e.fallback);e.dirty=false;e.conflict=false;e.serial++;}
    rebase(path){const e=this.entry(path);e.base=valueAt(this.config,path);e.dirty=!same(e.draft,e.base);e.conflict=false;e.serial++;}
    request(path){const e=this.entry(path);return{path:[...path],base:clone(e.base),value:clone(e.draft)};}
    get dirty(){return[...this.entries.values()].some(e=>e.dirty);}
  }
  root.DraftStore=DraftStore;root.settingValueAt=valueAt;root.settingSame=same;
  if(typeof module!=="undefined")module.exports={DraftStore,valueAt,same};
})(globalThis);
