/* 划词导入、普通历史接续和账号就绪顺序；无真实模型或设备。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..'),source=fs.readFileSync(path.join(root,'web/selection-followup.js'),'utf8');
const page=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
const historyFunction=page.slice(page.indexOf('function modelHistoryView('),page.indexOf("el('form').onsubmit="));
function environment(startup=Promise.resolve(true)){
  const nodes=new Map(),old={id:'old',title:'原对话',messages:[{role:'user',content:'原问题'}]};
  const context={assistantStartup:startup,accountLoading:false,accountExpired:false,chats:[old],activeId:'old',saved:[],render(){},closeSidebar(){},resizeQuestion(){},
    selectChat(chat){context.activeId=chat.id;},
    el:id=>{assert.notEqual(id,'allow-context');if(!nodes.has(id))nodes.set(id,{value:'',checked:false,textContent:'',focus(){this.focused=true;}});return nodes.get(id);},
    save(){context.saved.push(JSON.parse(JSON.stringify(context.chats)));}};
  context.window=context;vm.createContext(context);vm.runInContext(source+'\n'+historyFunction,context);return context;
}
const sample={id:'aa983c9d-afee-47d3-91a7-4587c57cf18b',text:'幂等性是什么？',answer:'**幂等性**：重复执行，结果相同。'};
(async()=>{
  let ready;const c=environment(new Promise(resolve=>ready=resolve));const pending=c.createSelectionConversation(sample);
  assert.equal(c.chats.length,1);ready(true);const id=await pending;
  assert.notEqual(id,'old');assert.equal(c.chats[0].messages[0].content,'原问题');assert.equal(c.activeId,id);
  const chat=c.chats[1];assert.equal(chat.messages[1].content,sample.answer);assert.match(chat.messages[0].content,/幂等性是什么/);
  assert.equal(c.el('question').focused,true);
  const history=c.modelHistoryView(chat,{revision:0}).history;assert.equal(history.length,2);assert.equal(history[1].content,sample.answer);
  await c.createSelectionConversation(sample);assert.equal(c.chats.length,2);
  const long=environment();await long.createSelectionConversation({...sample,answer:'答'.repeat(19000)});
  assert.equal(long.chats[1].messages.slice(1).map(m=>m.content).join(''),'答'.repeat(19000));assert.ok(long.chats[1].messages.every(m=>[...m.content].length<=16000));
  const expired=environment();expired.accountExpired=true;await assert.rejects(()=>expired.createSelectionConversation(sample));assert.equal(expired.chats.length,1);
  await assert.rejects(()=>c.createSelectionConversation({...sample,text:'x'.repeat(801)}));
  console.log(JSON.stringify({followup_ui_checks:5,passed:5,model_api_calls:0,device_reads:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
