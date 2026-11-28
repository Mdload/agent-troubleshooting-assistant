/* 直接执行网页提交函数：首次提问默认调用模型，材料与现场批准仍独立。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..'),page=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
const submit=page.slice(page.indexOf("el('form').onsubmit="),page.indexOf('// 说明由用户主动'));
const history=page.slice(page.indexOf('function modelHistoryView('),page.indexOf("el('form').onsubmit="));
function fixture({material,answerStatus='answered',modelTrace,evidenceScope}={}){
 const nodes=new Map(),requests=[],chat={id:'test-chat',title:'新对话',messages:[],material};
 const sandbox={busy:false,modeLoading:false,accountLoading:false,accountExpired:false,account:null,accountEpoch:1,requestBusy:false,csrf:'offline-token',
  current:()=>chat,spaceOf:()=> 'general',interaction:()=>({mode:'qa',revision:0}),inputLength:s=>[...s].length,
  el:id=>{assert.notEqual(id,'allow-context','移除勾选后仍访问旧控件');if(!nodes.has(id))nodes.set(id,{value:'',checked:false,textContent:'',disabled:false,focus(){}});return nodes.get(id);},
  save(){},render(){},resizeQuestion(){},openMaterialEditor(){throw Error('unexpected material conversion');},
  fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,status:200,json:async()=>({status:answerStatus,answer:'offline fixture',model_trace:modelTrace,evidence_scope:evidenceScope})};}};
 vm.createContext(sandbox);vm.runInContext(history+'\n'+submit,sandbox);sandbox.el('question').value='Python列表是什么';
 return {sandbox,requests,chat,send:()=>sandbox.el('form').onsubmit({preventDefault(){}})};
}
(async()=>{
 assert.equal(/id="allow-context"/.test(page),false);
 const instructions=page.slice(page.indexOf('<dialog id="privacy-dialog"'),page.indexOf('</dialog>',page.indexOf('<dialog id="privacy-dialog"')));
 assert.equal(/type="checkbox"|确认并发送/.test(instructions),false);
 const first=fixture();await first.send();assert.equal(first.requests.length,1);assert.equal(first.requests[0].body.allow_context,true);
 assert.equal(first.requests[0].body.chat_space,'general');
 assert.equal(first.chat.messages[0].context,true);assert.equal(first.chat.messages[1].content,'offline fixture');
 const material=fixture({material:{id:'material-fixture',name:'notes.txt'}});await material.send();assert.equal(material.requests.length,0);assert.match(material.sandbox.el('status').textContent,/确认所选材料/);
 material.sandbox.el('allow-materials').checked=true;await material.send();assert.equal(material.requests.length,1);assert.equal(material.requests[0].body.allow_context,true);assert.equal(material.requests[0].body.allow_materials,true);
 const diagnostic=fixture({answerStatus:'approval_required'});await diagnostic.send();assert.equal(diagnostic.requests.length,1);assert.equal(diagnostic.requests[0].body.approval_flow,'single_scope_v1');assert.match(diagnostic.sandbox.el('status').textContent,/确认是否同意本次检查/);
 assert.ok(diagnostic.chat.messages.every(message=>!message.context));
 const clarify=fixture({answerStatus:'clarify',modelTrace:[{action:'clarify'}]});
 clarify.sandbox.el('question').value='画面忽明忽暗';await clarify.send();
 clarify.sandbox.el('question').value='接收端';await clarify.send();
 assert.deepEqual(clarify.requests[1].body.history,[{role:'user',content:'画面忽明忽暗'},{role:'assistant',content:'offline fixture'}]);
 const historical=fixture({evidenceScope:{kind:'historical',current_reads:0}});
 historical.sandbox.el('question').value='刚才的接口地址是什么？';await historical.send();
 assert.ok(historical.chat.messages.every(message=>!message.context));
 historical.sandbox.el('question').value='下一步怎么排查？';await historical.send();
 assert.deepEqual(historical.requests[1].body.history,[]);
 console.log(JSON.stringify({default_model_ui_checks:5,passed:5,real_model_calls:0,device_reads:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
