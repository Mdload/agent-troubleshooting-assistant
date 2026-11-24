/* 阅读滚动及历史恢复迟到响应隔离，无真实浏览器/模型/设备。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/workspace-ui.js'),'utf8');
const page=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);new vm.Script(page);
function node(tag='div',text=''){return {tagName:tag.toUpperCase(),textContent:text,children:[],dataset:{},value:'',open:true,
 scrollTop:0,scrollHeight:1000,clientHeight:400,hidden:false,disabled:false,append(...items){this.children.push(...items);},
 replaceChildren(){this.children=[];},setAttribute(){},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},close(){this.open=false;this.listeners.close?.();},scrollIntoView(){}};}
function all(n){return [n,...n.children.flatMap(all)];}
function environment(api=async()=>({records:[],note:'历史记录'})){
  const elements=new Map(),frames=[];
  const sandbox={accountEpoch:1,accountLoading:false,accountExpired:false,busy:false,chat:{id:'one',messages:[{role:'user',content:'one'}]},
    current(){return sandbox.chat;},requestAnimationFrame:fn=>frames.push(fn),api,sidebar(){},window:{addEventListener(){}},
    taskStates:{completed:'已结束'},renderAnswer:(box,text)=>box.append(node('p',text)),evidence:(box,result)=>box.append(node('pre',result.evidence[0].output)),
    el:id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);},add(parent,tag,text){const item=node(tag,text);parent.children.push(item);return item;}};
  vm.createContext(sandbox);vm.runInContext(source,sandbox);sandbox.flush=()=>{for(const f of frames.splice(0))f();};return sandbox;
}
async function main(){
  const reading=environment();reading.syncWorkspace();reading.flush();
  assert.equal(reading.el('conversation-scroll').scrollTop,1000);
  const pane=reading.el('conversation-scroll');pane.scrollTop=100;pane.listeners.scroll();reading.chat.messages.push({content:'new reply'});
  reading.syncWorkspace();reading.flush();assert.equal(pane.scrollTop,100);assert.equal(reading.el('jump-latest').hidden,false);
  reading.jumpLatest();assert.equal(pane.scrollTop,1000);assert.equal(reading.el('jump-latest').hidden,true);

  const search=environment();search.chats=[{id:'a',title:'Croc异常',updated:1,messages:[]},{id:'b',title:'Python列表',updated:2,messages:[]},
    {id:'c',title:'热备现场',updated:3,messages:[],interaction:{mode:'diagnostic',scene:'ha_topology'}}];
  Object.assign(search,{activeId:'a',activeSpace:'general',lastChatInSpace:{general:'a',ha:null},chatDrafts:new Map(),
    requestBusy:false,modeLoading:false,save(){},render(){},resizeQuestion(){},closeSidebar(){}});
  search.el('question').focus=()=>{};search.el('history-search').value='CROC';
  vm.runInContext(page.slice(page.indexOf('function spaceOf('),page.indexOf('function save(){'))+
    page.slice(page.indexOf('function sidebar(){'),page.indexOf('function eventNote('))+
    page.slice(page.indexOf('function newChat('),page.indexOf('try{const data=JSON.parse(localStorage')),search);
  search.sidebar();assert.equal(search.el('history-count').textContent,'1');assert.ok(all(search.el('threads')).some(n=>n.textContent==='Croc异常'));
  assert.ok(!all(search.el('threads')).some(n=>n.textContent==='Python列表'));
  search.el('question').value='通用草稿';search.selectChat(search.preferredChat('ha'),'ha');search.sidebar();
  assert.equal(search.current().id,'c');assert.equal(search.el('question').value,'');
  assert.ok(all(search.el('threads')).some(n=>n.textContent==='热备现场'));
  assert.ok(!all(search.el('threads')).some(n=>n.textContent==='Croc异常'));
  search.selectChat(search.preferredChat('general'),'general');
  assert.equal(search.current().id,'a');assert.equal(search.el('question').value,'通用草稿');
  search.newChat('ha','局端和远端是通的吗？');
  assert.notEqual(search.current().id,'c');assert.equal(search.current().messages.length,0);
  assert.equal(search.el('question').value,'局端和远端是通的吗？');

  let complete;const late=environment(()=>new Promise(resolve=>complete=resolve));
  const pending=late.loadInvestigationRecords();late.accountEpoch++;late.resetWorkspaceUI();late.chat={id:'new',messages:[]};
  complete({note:'OLD_PRIVATE',records:[{id:'old',question:'private old task',status:'completed'}]});await pending;
  assert.equal(late.el('investigation-list').children.length,0);assert.equal(late.el('investigation-note').textContent,'');

  let finish;const closed=environment(()=>new Promise(resolve=>finish=resolve));const detail=closed.showInvestigation('old');
  closed.el('investigation-dialog').close();finish({note:'OLD',question:'old',result:{answer:'old',evidence:[{output:'private'}]}});await detail;
  assert.equal(closed.el('investigation-detail').children.length,0);

  const valid=environment(async()=>({note:'历史采集',question:'原问题',status:'completed',reads:2,started_at:10,
    result:{answer:'历史回答',evidence:[{output:'<script>untrusted original</script>'}]}}));
  await valid.showInvestigation('a');assert.ok(all(valid.el('investigation-detail')).some(n=>n.textContent==='<script>untrusted original</script>'));
  assert.equal(valid.chat.messages.length,1);assert.equal(valid.chat.messages[0].content,'one');
  assert.ok(!all(valid.el('investigation-detail')).some(n=>n.tagName==='BUTTON'));

  const switched=environment();switched.syncWorkspace();switched.el('investigation-dialog').open=true;switched.el('investigation-detail').append(node('p','old'));
  switched.chat={id:'two',messages:[]};switched.syncWorkspace();switched.flush();assert.equal(switched.el('investigation-dialog').open,false);
  assert.equal(switched.el('investigation-detail').children.length,0);
  console.log(JSON.stringify({ui_workspace_checks:6,passed:6,visual_acceptance:false,model_api_calls:0,device_reads:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
