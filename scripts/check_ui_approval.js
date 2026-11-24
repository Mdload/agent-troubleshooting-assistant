/* 服务端接管新批准，旧批准兼容；真实脚本、确定性HTTP替身。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
const block=source.slice(source.indexOf('const automaticStarts='),source.indexOf('function previousSnapshotTask('));
function fixture(){
  const calls=[],updates=[],timers=new Map();let timer=0;
  const s={busy:false,modeLoading:false,accountLoading:false,accountExpired:false,registryReady:true,accountEpoch:1,requestBusy:false,
    taskPollers:new Map(),taskOf:m=>m.workflowTask,save(){},render(){},el:()=>({textContent:''}),deviceName:()=>'',checkLabels:{},
    document:{querySelectorAll:()=>[]},setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),
    api:async(url,body)=>{calls.push({url,body});return {id:'t',status:'completed'};},
    updateTask:(m,t)=>{m.workflowTask=t;updates.push(t);s.stopWatchingTask(t.id);}};
  vm.createContext(s);vm.runInContext(block,s);
  return {s,calls,updates,timers,tick:async()=>{const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();}};
}
function inlineFixture(){
  const calls=[],starts=[],elements=new Map();
  const s={accountEpoch:1,busy:false,taskOf:m=>m.workflowTask,taskStates:{pending:'等待批准'},
    taskScope(){},watchTask(){},previousSnapshotTask:()=>null,
    add(parent,tag,text='',className=''){const node={tag,textContent:text,className,children:[],disabled:false,setAttribute(){}};parent.children.push(node);return node;},
    el(id){if(!elements.has(id))elements.set(id,{open:false,textContent:'',showModal(){this.open=true;}});return elements.get(id);},
    api:async(url,body)=>{calls.push({url,body});return {id:'t',status:body?.approved?'approved':body?.text?'running':'cancelled',approval_flow:'single_scope_v1',data_use_approved:true,server_owned:true};},
    updateTask:(m,t)=>{m.workflowTask=t;},
    startApprovedTask:async(m,t)=>{starts.push(t.id);}};
  vm.createContext(s);
  vm.runInContext(source.slice(source.indexOf('function taskStateLabel('),source.indexOf('function updateTask(')),s);
  vm.runInContext(source.slice(source.indexOf('const approvalClicks='),source.indexOf('async function refreshAdmin(')),s);
  return {s,calls,starts,elements};
}
async function main(){
  const labels=inlineFixture().s;
  const measurementTask=(status,measurements)=>({status,result:{goal_assessment:{
    goal:'icmp_reachability',status:'unverified',measurements}}});
  assert.match(labels.taskStateLabel(measurementTask('incomplete',[{outcome:'unavailable'}])),/未测得/);
  assert.match(labels.taskStateLabel(measurementTask('incomplete',[{outcome:'measured',received:0},{outcome:'unavailable'}])),/部分方向未测得/);
  assert.match(labels.taskStateLabel(measurementTask('completed',[{outcome:'no_route'}])),/未发包/);
  const task={id:'t',status:'approved',approval_flow:'single_scope_v1',data_use_approved:true};
  const first=fixture();let release;
  first.s.api=async(url,body)=>{first.calls.push({url,body});return new Promise(resolve=>release=resolve);};
  const message={workflowTask:{...task}},pending=first.s.startApprovedTask(message,{...task});
  await first.s.startApprovedTask(message,{...task});
  assert.equal(first.calls.length,1);assert.equal(first.calls[0].url,'/api/tasks/t/start');
  assert.equal(JSON.stringify(first.calls[0].body),'{}','数据批准必须来自服务器任务记录，页面不填allow_live_data=true');
  release({id:'t',status:'completed'});await pending;assert.equal(first.updates.length,1);
  const denied=fixture();
  for(const t of [{...task,approval_flow:undefined},{...task,status:'pending'},{...task,data_use_approved:false},{...task,server_owned:true}])await denied.s.startApprovedTask({},t);
  assert.equal(denied.calls.length,0,'旧任务、未批准任务和缺数据用途的任务均不自动开始');
  const changed=fixture();let finish;
  changed.s.api=async()=>new Promise(resolve=>finish=resolve);
  const stale=changed.s.startApprovedTask({}, {...task});changed.s.accountEpoch++;
  finish({id:'t',status:'completed'});await stale;assert.equal(changed.updates.length,0,'迟到响应不写进新身份');
  const poll=fixture(),steps=[{...task,status:'pending'},task,{id:'t',status:'completed'}];
  poll.s.api=async(url,body)=>{poll.calls.push({url,body});return steps.shift();};
  const watched={workflowTask:{...task,status:'pending'}};poll.s.watchTask(watched,'t');
  await poll.tick();assert.equal(poll.calls.filter(c=>c.body!==undefined).length,0);
  await poll.tick();await Promise.resolve();await Promise.resolve();
  assert.equal(poll.calls.filter(c=>c.url==='/api/tasks/t/start').length,1);
  assert.equal(poll.updates.at(-1).status,'completed');

  const pendingTask={id:'t',status:'pending',approval_flow:'single_scope_v1',data_use:'inferera_diagnostic_v1',expires:Date.now()/1000+300};
  const admin=inlineFixture(),msg={workflowTask:{...pendingTask}},box={children:[]};
  admin.s.taskCard(box,msg);
  const actions=box.children[0].children.find(n=>n.className==='task-actions');
  const allow=actions.children.find(n=>n.textContent==='同意本次排障');
  assert.ok(allow);assert.ok(actions.children.find(n=>n.textContent==='暂不执行'));
  await allow.onclick();
  assert.equal(admin.calls.length,1);assert.equal(admin.calls[0].url,'/api/tasks/t/decision');
  assert.deepEqual(JSON.parse(JSON.stringify(admin.calls[0].body)),{approved:true,data_use:pendingTask.data_use,background:true});
  assert.deepEqual(admin.starts,[]);assert.equal(msg.workflowTask.status,'approved');

  const inbox=inlineFixture(),inboxBox={children:[]};
  const running={...task,status:'running',server_owned:true,task_inputs:[]};
  const attached={workflowTask:running};inbox.s.taskInput(inboxBox,attached,running);
  const input=inboxBox.children.find(n=>n.tag==='textarea');
  input.value='按你说的先看第一个。';input.oninput();
  await inboxBox.children.find(n=>n.textContent==='发送补充').onclick();
  assert.equal(inbox.calls.length,1);assert.equal(inbox.calls[0].url,'/api/tasks/t/input');
  assert.equal(inbox.calls[0].body.text,input.value);assert.ok(inbox.calls[0].body.message_id);

  assert.equal(admin.calls.some(c=>c.url.startsWith('/api/admin/')),false,'聊天确认不查询或登录管理员');
  assert.equal(admin.elements.has('admin-dialog'),false);

  const declining=inlineFixture(),declined={workflowTask:{...pendingTask}},declineBox={children:[]};
  declining.s.taskCard(declineBox,declined);
  const decline=declineBox.children[0].children.find(n=>n.className==='task-actions').children.find(n=>n.textContent==='暂不执行');
  await decline.onclick();assert.equal(declining.calls[0].url,'/api/tasks/t/cancel');assert.equal(declining.starts.length,0);

  const duplicate=inlineFixture();let decisionReady;
  duplicate.s.api=async(url,body)=>{duplicate.calls.push({url,body});return new Promise(resolve=>decisionReady=resolve);};
  const approving=duplicate.s.approveTaskInChat({}, {...pendingTask},{});
  await duplicate.s.approveTaskInChat({}, {...pendingTask},{});
  assert.equal(duplicate.calls.length,1);decisionReady({...task});await approving;
  assert.equal(duplicate.calls.filter(c=>c.body?.approved).length,1);assert.equal(duplicate.starts.length,0);

  const expired=inlineFixture();expired.s.api=async(url,body)=>{
    expired.calls.push({url,body});throw Object.assign(Error('本次申请已过期'),{status:409});};
  await expired.s.approveTaskInChat({}, {...pendingTask},{});
  assert.equal(expired.starts.length,0);assert.match(expired.s.el('status').textContent,/已过期/);

  const identity=inlineFixture();let resolveDecision;
  identity.s.api=async()=>new Promise(resolve=>resolveDecision=resolve);
  const oldIdentity=identity.s.approveTaskInChat({}, {...pendingTask},{});identity.s.accountEpoch++;
  resolveDecision({...task});await oldIdentity;assert.equal(identity.starts.length,0);
  const parent={workflowTask:{id:'parent',status:'running'}},chat={messages:[parent]};
  const continued={chats:[chat],current:()=>chat,save(){},render(){},taskStates:{},taskOf:m=>m.workflowTask,
    el:()=>({textContent:''}),watchTask(){},stopWatchingTask(){}};
  vm.createContext(continued);
  vm.runInContext(source.slice(source.indexOf('function updateTask('),source.indexOf('const automaticStarts=')),continued);
  const child={id:'new',status:'pending',scope:{'remote-primary':['interfaces']}};
  const result={id:'parent',status:'incomplete',result:{answer:'已有局端证据。',followup_task:child}};
  continued.updateTask(parent,result);
  continued.updateTask(parent,result);
  assert.equal(chat.messages.length,2,'同一个补查申请只展示一次');
  assert.equal(chat.messages[1].workflowTask,child);
  assert.equal(chat.messages[1].context,false);
  const localResult={id:'local',status:'completed',automatic_read:true,result:{status:'answered',answer:'服务状态只在本地展示。'}};
  chat.messages.push({role:'user',context:true},{role:'assistant',context:true});
  continued.updateTask(chat.messages.at(-1),localResult);
  assert.equal(chat.messages.at(-1).context,false,'本地只读结果不能作为后续模型历史发送');
  assert.equal(chat.messages.at(-2).context,false);
  const localInbox={children:[]};inbox.s.taskInput(localInbox,{}, {...running,automatic_read:true});
  assert.equal(localInbox.children.length,0);
  console.log(JSON.stringify({ui_approval_checks:16,passed:16,model_api_calls:0,device_reads:0,visual_acceptance:false}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
