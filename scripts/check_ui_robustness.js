/* 执行真实页面函数；无浏览器、模型或设备，不宣称视觉验收。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
function element(){return {disabled:false,hidden:false,value:'',textContent:'',replaceChildren(){},style:{}};}
const elements=new Map();
const watched=[];
const message={role:'assistant',content:'正在调查',workflowTask:{id:'old-task',status:'running'}};
const chat={id:'restored',title:'刷新前的调查',messages:[message]};
const sandbox={chats:[chat],busy:false,requestBusy:false,modeLoading:false,registryReady:true,liveProgressState:null,
  account:null,accountLoading:false,accountExpired:false,
  activeSpace:'ha',sidebar(){},current:()=>chat,
  el:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);},
  taskOf:m=>m.workflowTask,watchTask:(message,id)=>watched.push(id),add:()=>element(),messageHeader(){},answerCopyText:text=>text,renderAnswer(){},evidence(){},taskCard(){},renderAgentDetails(){},
  interaction:()=>({mode:'diagnostic',scene:'ha_topology',revision:1}),
  document:{querySelectorAll:()=>[]}};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf('function render(){'),source.indexOf('function resizeQuestion(')),sandbox);
sandbox.render();
assert.equal(sandbox.el('send').disabled,true,'刷新后仍在运行的调查必须阻止重复提交');
message.workflowTask.status='completed';sandbox.render();assert.equal(sandbox.el('send').disabled,false);
sandbox.requestBusy=true;sandbox.render();assert.equal(sandbox.el('send').disabled,true);
sandbox.requestBusy=false;chat.messages=[];sandbox.chats.push({id:'background',messages:[message]});
message.workflowTask.status='running';sandbox.render();
assert.ok(watched.includes('old-task'),'其他对话的运行任务也须恢复轮询，避免永久锁住页面');
assert.equal(sandbox.el('send').disabled,false,'其他空间的调查不锁定当前对话');
console.log(JSON.stringify({ui_robustness_checks:4,passed:4,visual_acceptance:false,model_api_calls:0,device_reads:0}));
