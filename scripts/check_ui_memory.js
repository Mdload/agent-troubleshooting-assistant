/* 执行实际记忆页面函数：身份/对话/迟到响应、按次选用及选择数量。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/memory-ui.js'),'utf8');new vm.Script(source);
function node(tag='div',text=''){return {tagName:tag.toUpperCase(),textContent:text,children:[],checked:false,value:'',hidden:false,open:true,disabled:false,listeners:{},append(x){this.children.push(x);},replaceChildren(){this.children=[];},setAttribute(){},addEventListener(k,f){this.listeners[k]=f;},close(){this.open=false;this.listeners.close?.();},showModal(){this.open=true;}};}
function environment(api=async()=>({})){const elements=new Map(),toolbar=node();const e={account:{id:'a'},accountEpoch:1,accountLoading:false,accountExpired:false,busy:false,historyQueue:Promise.resolve(),chat:{id:'one',messages:[]},api,
 current(){return e.chat;},interaction(c){return {mode:(c||e.chat)?.mode||'qa'};},document:{querySelector:()=>toolbar,createElement:node},
 el:id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);},add(p,t,text='',cls=''){const x=node(t,text);x.className=cls;p.children.push(x);return x;}};
 vm.createContext(e);vm.runInContext(source,e);return e;}
async function main(){
 const selected=environment();vm.runInContext("memoryRecord={state:'active',revision:3};",selected);selected.syncTaskMemory();
 assert.equal(selected.el('memory-consent').hidden,false);assert.deepEqual(JSON.parse(JSON.stringify(selected.selectedTaskMemory())),{});
 selected.el('use-task-memory').checked=true;assert.equal(selected.selectedTaskMemory().memory_revision,3);
 selected.chat.mode='diagnostic';selected.syncTaskMemory();assert.equal(selected.el('use-task-memory').checked,false);assert.equal(selected.el('memory-consent').hidden,true);

 const reset=environment();vm.runInContext("memoryRecord={state:'active',revision:3};",reset);reset.el('use-task-memory').checked=true;reset.resetTaskMemory();assert.equal(reset.el('use-task-memory').checked,false);assert.equal(reset.el('memory-entries').children.length,0);

 let done;const late=environment(()=>new Promise(r=>done=r));late.el('memory-dialog').open=true;const loading=late.loadTaskMemory();await new Promise(setImmediate);assert.equal(typeof done,'function',late.el('memory-note').textContent);late.accountEpoch++;late.resetTaskMemory();done({state:'active',entries:[],revision:1});await loading;assert.equal(late.el('memory-entries').children.length,0);

 const closed=environment(async url=>url.endsWith('/draft')?{history_revision:1,candidates:[]}:{state:'empty',revision:0,entries:[]});closed.el('memory-dialog').close();await closed.loadTaskMemory();assert.equal(closed.el('memory-entries').children.length,0);

 let calls=0;const limit=environment(async()=>{calls++;return {};});vm.runInContext("memoryRecord={revision:1};memoryDraft={history_revision:1};memoryRows=Array.from({length:7},()=>({check:{checked:true},kind:{value:'goal'},item:{message_index:0,quote:'x'}}));",limit);
 limit.el('memory-confirm').checked=true;await limit.changeTaskMemory(false);assert.equal(calls,0);assert.ok(limit.el('memory-note').textContent.includes('1至6'));

 const save=environment(async()=>({state:'active',revision:2,entries:[]}));vm.runInContext("memoryRecord={revision:1};memoryDraft={history_revision:1};memoryRows=[{check:{checked:true},kind:{value:'goal'},item:{message_index:0,quote:'<script>user text</script>'}}];",save);
 save.el('memory-dialog').open=true;save.el('memory-confirm').checked=true;await save.changeTaskMemory(false);assert.equal(save.el('use-task-memory').checked,false);assert.equal(save.el('memory-confirm').checked,false);
 const toggles=[];const rolling=environment(async(url,body)=>{toggles.push({url,body});return {state:body.enabled?'active':'off',revision:body.expected_revision+1,entries:body.enabled?[{kind:'goal',message_index:0,quote:'<script>quoted user goal</script>'}]:[]};});
 rolling.el('memory-dialog').open=true;rolling.renderRollingContext({state:'off',revision:0,entries:[]});await rolling.changeRollingContext();
 assert.equal(toggles.length,1);assert.deepEqual(JSON.parse(JSON.stringify(toggles[0].body)),{enabled:true,expected_revision:0});
 assert.match(rolling.el('rolling-toggle').textContent,/关闭并清除/);assert.equal(rolling.el('use-task-memory').checked,false);
 assert.equal(rolling.el('rolling-entries').children[0].children[1].textContent,'<script>quoted user goal</script>');
 rolling.busy=true;rolling.syncTaskMemory();assert.equal(rolling.el('rolling-toggle').disabled,false);
 await rolling.changeRollingContext();assert.equal(toggles[1].body.enabled,false);assert.equal(rolling.el('rolling-entries').children.length,0);
 let finishRolling;const staleRolling=environment(()=>new Promise(resolve=>finishRolling=resolve));
 staleRolling.el('memory-dialog').open=true;staleRolling.renderRollingContext({state:'off',revision:0,entries:[]});const pending=staleRolling.changeRollingContext();
 staleRolling.accountEpoch++;staleRolling.resetTaskMemory();finishRolling({state:'active',revision:1,entries:[{quote:'PRIVATE OLD USER'}]});await pending;
 assert.equal(staleRolling.el('rolling-entries').children.length,0);assert.equal(staleRolling.el('rolling-toggle').disabled,true);
 console.log(JSON.stringify({ui_memory_checks:8,passed:8,model_api_calls:0,live_device_reads:0,visual_acceptance:false}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
