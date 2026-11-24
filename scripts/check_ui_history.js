/* 实际页面保存/恢复/模型视图函数，不模拟框架内部行为。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[2]||path.join(__dirname,'..');
const source=fs.readFileSync(path.join(root,'web/account-ui.js'),'utf8');
const page=fs.readFileSync(path.join(root,'web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes=new Map();let saved;
const env={account:{id:'offline'},accountEpoch:1,accountLoading:false,accountExpired:false,taskPollers:new Map(),
 document:{querySelectorAll:()=>[]},current:()=>null,render(){},api:async()=>({title:'restored',updated:1,revision:1,messages:saved.messages}),
 el(id){if(!nodes.has(id))nodes.set(id,{value:'',checked:false,hidden:false,textContent:'',addEventListener(){},replaceChildren(){}});return nodes.get(id);}};
vm.createContext(env);vm.runInContext(source,env);
vm.runInContext(page.slice(page.indexOf('function modelHistoryView('),page.indexOf("el('form').onsubmit=")),env);
async function main(){
 const before={title:'history',messages:[{role:'user',content:'failed turn',context:false,contextRevision:0},
  {role:'assistant',content:'valid answer',context:true,contextRevision:0}]};
 saved=env.historySnapshot(before);const restored={id:'same'};await env.loadHistoryChat(restored);
 assert.equal(restored.messages[0].context,false);assert.equal(restored.messages[1].context,true);
 assert.equal(restored.messages[1].contextRevision,0);
 assert.equal(env.modelHistoryView(restored,{revision:0}).history.length,1);
 assert.equal(env.modelHistoryView(restored,{revision:1}).history.length,0);
 saved={messages:[{role:'assistant',content:'legacy lacks eligibility'}]};await env.loadHistoryChat(restored);
 assert.equal(restored.messages[0].context,false);
 const original='😀'.repeat(3500),chat={messages:[{role:'assistant',content:original,context:true,contextRevision:0}]};
 const view=env.modelHistoryView(chat,{revision:0});assert.equal(Array.from(view.history[0].content).length,3000);
 assert.equal(chat.messages[0].content,original);assert.equal(view.excerpted,1);assert.ok(view.history[0].content.includes('已节选'));
 console.log(JSON.stringify({ui_history_checks:3,passed:3,model_api_calls:0,live_device_reads:0,visual_acceptance:false}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
