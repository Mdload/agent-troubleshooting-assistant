/* 具体方案一次确认后执行、重复点击/换账号不串状态；无网络或设备。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/repair-ui.js'),'utf8');
function environment(){
 const nodes=[],requests=[];let after;
 const plan={id:'a'.repeat(32),state:'pending',expires:Date.now()/1000+300,plan:{reason:'只启动备机热备服务'}};
 const message={result:{repair_plan:plan}};
 const c={accountEpoch:1,taskOf:()=>null,render(){},save(){},el:()=>({textContent:'',addEventListener(){}}),add(parent,tag,text,cls){const node={tag,text,cls};nodes.push(node);return node;},
  api:async(url,body)=>{if(body===undefined)return plan;requests.push({url,body});if(url.endsWith('decision'))return {...plan,state:'approved'};return new Promise(resolve=>{after=()=>resolve({...plan,state:'completed',result:{status:'service_restored'}});});}};
 vm.createContext(c);vm.runInContext(source,c);c.renderRepair({},message);return {c,nodes,requests,message,finish:()=>after()};
}
(async()=>{
 const a=environment(),button=a.nodes.find(n=>n.text==='同意并恢复'),pending=button.onclick();await new Promise(r=>setImmediate(r));
 assert.equal(a.requests.length,2);assert.ok(a.requests[0].url.endsWith('/decision'));assert.deepEqual(a.requests[0].body.approved,true);
 await button.onclick();assert.equal(a.requests.length,2);a.finish();await pending;assert.equal(a.message.repairPlan.state,'completed');
 const changed=environment(),run=changed.nodes.find(n=>n.text==='同意并恢复').onclick();await new Promise(r=>setImmediate(r));changed.c.accountEpoch++;changed.finish();await run;assert.notEqual(changed.message.repairPlan.state,'completed');
 console.log(JSON.stringify({repair_ui_checks:2,passed:2,device_writes:0,model_calls:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});
