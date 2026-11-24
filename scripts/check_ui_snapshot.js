/* Real page helpers: same-chat selection, historical labels, escaped raw output, stale response guard. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
function node(tag='',text=''){return {tagName:tag.toUpperCase(),textContent:text,children:[],open:false,
  append(...values){this.children.push(...values);},replaceChildren(){this.children=[];},showModal(){this.open=true;}};}
function all(n){return [n,...n.children.flatMap(all)];}
const elements=new Map(),chat={messages:[]};let requests=[];
const sandbox={current:()=>chat,taskOf:m=>m.workflowTask,
  deviceName:d=>d,checkLabels:{interfaces:'VIP'},
  add(parent,tag,text='',cls=''){const child=node(tag,text);child.className=cls;parent.children.push(child);return child;},
  el(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);},
  api:async(url,body)=>{requests.push({url,body});return {entries:[],comparable_fields:0,changed_fields:0,note:'历史比较，当前未核实'};}};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('function previousSnapshotTask('),source.indexOf('function taskCard(')),sandbox);
const earlier={workflowTask:{id:'a',scene:'ha_topology',status:'completed',result:{}}};
const running={workflowTask:{id:'b',scene:'ha_topology',status:'running',result:null}};
const after={workflowTask:{id:'c',scene:'ha_topology',status:'completed',result:{}}};
chat.messages=[earlier,running,after];assert.equal(sandbox.previousSnapshotTask(after).id,'a');
chat.messages=[after];assert.equal(sandbox.previousSnapshotTask(after),null);
const box=node();sandbox.renderSnapshotComparison(box,{note:'历史快照，不证明恢复',comparable_fields:1,changed_fields:1,
  entries:[{device:'local-primary',check:'interfaces',status:'changed',before:{text:'未见VIP',collected_at:'T1',ref:'OBS-1',raw_output:'<script>alert(1)</script>'},
    after:{text:'已见VIP',collected_at:'T2',ref:'OBS-1',raw_output:'raw'}}]});
assert.ok(all(box).some(n=>n.textContent.includes('历史快照')));assert.ok(all(box).some(n=>n.textContent.includes('T1')));
assert.ok(all(box).some(n=>n.textContent.includes('T2')));assert.equal(all(box).filter(n=>n.tagName==='SCRIPT').length,0);
assert.ok(all(box).some(n=>n.tagName==='PRE'&&n.textContent.includes('<script>')));
async function main(){
  await sandbox.showSnapshotComparison('a','c');assert.equal(requests.length,1);
  assert.equal(requests[0].url,'/api/task-comparison');assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),{before_task:'a',after_task:'c'});
  let resolve; sandbox.api=()=>new Promise(r=>resolve=r);
  const pending=sandbox.showSnapshotComparison('a','c');sandbox.el('comparison-dialog').open=false;
  resolve({entries:[],comparable_fields:99,changed_fields:99,note:'STALE_SHOULD_NOT_RENDER'});await pending;
  assert.ok(!all(sandbox.el('comparison-body')).some(n=>n.textContent.includes('STALE_SHOULD_NOT_RENDER')));
  console.log(JSON.stringify({ui_snapshot_checks:4,passed:4,model_api_calls:0,live_device_reads:0,visual_acceptance:false}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
