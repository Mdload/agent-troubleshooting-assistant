/* 执行账号页面函数，验证迟到响应、存储隔离和冲突；无真实浏览器/模型/设备。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/account-ui.js'),'utf8');
const page=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);new vm.Script(page);
function environment(api){
  const elements=new Map(),stopped=[];
  const sandbox={account:{id:'alice',name:'alice'},accountEpoch:1,accountLoading:false,accountExpired:false,
    chats:[],activeId:null,requestBusy:false,busy:false,modeLoading:false,
    taskPollers:new Map([['pending',{}]]),stopWatchingTask:id=>stopped.push(id),render(){},resetChatSpaces(){},experienceEditor:{private:'old'},
    document:{querySelectorAll:()=>[]},current(){return sandbox.chats.find(c=>c.id===sandbox.activeId);},
    api,el:id=>{assert.notEqual(id,'allow-context');if(!elements.has(id))elements.set(id,{value:'',checked:true,hidden:false,disabled:false,textContent:'',replaceChildren(){},addEventListener(){}});return elements.get(id);}}
  vm.createContext(sandbox);vm.runInContext(source,sandbox);sandbox.stopped=stopped;return sandbox;
}
async function main(){
  let requests=[];
  const ordered=environment(async(url,data)=>{requests.push(JSON.parse(JSON.stringify(data)));return {revision:data.expected_revision+1,updated:1};});
  const chat={id:'one',title:'one',messages:[{role:'user',content:'first'}],updated:1};ordered.chats=[chat];ordered.activeId='one';
  ordered.queueHistorySaves();chat.messages.push({role:'assistant',content:'second'});ordered.queueHistorySaves();
  await vm.runInContext('historyQueue',ordered);
  assert.deepEqual(requests.map(r=>r.expected_revision),[0,1]);assert.equal(chat.serverRevision,2);
  assert.deepEqual(Object.keys(requests[0]).sort(),['expected_revision','id','messages','title']);

  let finish;const late=environment(()=>new Promise(resolve=>finish=resolve));
  const old={id:'old',title:'old',messages:[{role:'user',content:'private old account'}],updated:1};late.chats=[old];
  late.queueHistorySaves();await Promise.resolve();const waiting=vm.runInContext('historyQueue',late);
  late.accountEpoch++;late.chats=[];late.el('storage-note').textContent='new account';finish({revision:99,updated:99});await waiting;
  assert.equal(old.serverRevision,undefined);assert.equal(late.el('storage-note').textContent,'new account');

  let attempted=0;const conflict=environment(async()=>{attempted++;throw Object.assign(Error('conflict'),{status:409});});
  conflict.chats=[{id:'conflict',title:'c',messages:[{role:'user',content:'unsaved'}]}];
  conflict.queueHistorySaves();await vm.runInContext('historyQueue',conflict);conflict.queueHistorySaves();
  assert.equal(attempted,1);assert.equal(conflict.chats[0].syncBlocked,true);
  assert.match(conflict.el('storage-note').textContent,/尚未保存/);

  const restored=environment(async()=>({title:'restored',updated:1,revision:3,messages:[{role:'assistant',content:'old OBS',workflowTask:{approved:true},result:{evidence:['fake']}}]}));
  const item={id:'restored'};await restored.loadHistoryChat(item);
  assert.equal(item.messages[0].workflowTask,undefined);assert.equal(item.messages[0].result,undefined);assert.equal(item.restored,true);
  restored.expireAccount();assert.equal(restored.chats.length,0);assert.equal(restored.account,null);
  assert.equal(restored.accountExpired,true);assert.deepEqual(restored.stopped,['pending']);

  let browserWrites=0,serverQueues=0;
  const privacy={account:{id:'alice'},accountLoading:false,accountExpired:false,localStorage:{setItem(){browserWrites++;}},
    queueHistorySaves(){serverQueues++;},el:()=>({textContent:''}),storageKey:'guest',chats:[],activeId:null,activeSpace:'general',lastChatInSpace:{}};
  vm.createContext(privacy);vm.runInContext(page.slice(page.indexOf('function save(){'),page.indexOf('const mobileSidebar=')),privacy);
  privacy.save();assert.equal(browserWrites,0);assert.equal(serverQueues,1);
  privacy.account=null;privacy.accountExpired=true;privacy.save();assert.equal(browserWrites,0);
  privacy.accountExpired=false;privacy.save();assert.equal(browserWrites,1);
  let expired=0;
  const authBoundary={accountEpoch:1,account:{id:'alice'},csrf:'offline-csrf',expireAccount(){expired++;},
    fetch:async()=>({ok:false,status:401,json:async()=>({error:'authentication required'})})};
  vm.createContext(authBoundary);vm.runInContext(page.slice(page.indexOf('async function api('),page.indexOf('function taskOf(')),authBoundary);
  await assert.rejects(authBoundary.api('/api/admin/tasks/t/decision',{approved:true}),{status:401});
  assert.equal(expired,0,'管理员会话失效不应退出个人账号');
  await assert.rejects(authBoundary.api('/api/history'),{status:401});assert.equal(expired,1);
  const activeTask={id:'running',status:'running',server_owned:true,question:'等待补充的调查',
    conversation_id:'ha',interaction_revision:4,progress:{stage:'awaiting_input'}};
  const activeMode={mode:'diagnostic',revision:4,active_tasks:[activeTask,
    {...activeTask,id:'wrong-chat',conversation_id:'elsewhere'},
    {...activeTask,id:'stale',interaction_revision:3},
    {...activeTask,id:'old-start',status:'approved',server_owned:false}]};
  let modeCalls=0;
  const reopened=environment(async url=>{modeCalls++;assert.equal(url,'/api/conversations/ha/mode');return activeMode;});
  const hot={id:'ha',space:'ha',messages:[{role:'user',content:'最初问题',context:true}],loaded:true};
  Object.assign(reopened,{activeId:'ha',activeSpace:'ha',lastChatInSpace:{},chats:[hot],save(){},
    taskOf:m=>m.workflowTask,taskStates:{},watchTask(){}});
  vm.runInContext(page.slice(page.indexOf('function attachActiveTasks('),page.indexOf('async function refreshOwnTasks('))+
    page.slice(page.indexOf('function updateTask('),page.indexOf('const automaticStarts='))+
    page.slice(page.indexOf('async function loadMode('),page.indexOf('async function loadRegistry(')),reopened);
  await reopened.loadMode();
  await reopened.loadMode();
  assert.equal(modeCalls,2);assert.equal(hot.messages.length,2);
  assert.equal(hot.messages[1].workflowTask,activeTask);
  assert.equal(hot.messages[1].context,false);
  assert.equal(hot.interaction.active_tasks,undefined);
  reopened.updateTask(hot.messages[1],{...activeTask,status:'completed',result:{answer:'旧调查结果',status:'answered'}});
  assert.equal(hot.messages[1].context,false);
  assert.equal(hot.messages[0].context,true);
  assert.equal(reopened.historySnapshot(hot).messages.length,1);
  console.log(JSON.stringify({ui_account_checks:7,passed:7,visual_acceptance:false,model_api_calls:0,device_reads:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
