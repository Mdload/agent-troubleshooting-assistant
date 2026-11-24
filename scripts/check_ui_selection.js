/* 网页与本机组件的消息边界、QA请求和关闭/账号切换；不操作真实桌面或模型。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/selection-ui.js'),'utf8');new vm.Script(source);
const streamSource=fs.readFileSync(path.join(__dirname,'../web/selection-stream.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
function environment(options={}){
  const toggle={checked:false,disabled:true},note={textContent:''},button={style:{}},listeners={},timers=[],intervals=[],sent=[],requests=[],opened=[];
  let frame;
  const box={querySelector:s=>s==='input'?toggle:s==='button'?button:note,append(value){frame=value;}};
  const imported=[],urls=[];
  const sandbox={AbortController,location:{origin:options.origin||'http://127.0.0.1:17869',pathname:'/',search:'',hash:options.followupId?'#selection-followup='+options.followupId:''},history:{replaceState:(...args)=>urls.push(args[2])},window:{createSelectionConversation:async data=>imported.push(data),open:(...args)=>opened.push(args)},el:()=>note,assistantStartup:Promise.resolve(true),crypto:options.crypto||require('node:crypto'),accountEpoch:1,accountLoading:false,accountExpired:false,csrf:'synthetic-csrf',
    document:{getElementById:()=>null,head:{append(){}},body:{prepend(){}},querySelector:()=>({prepend(){}}),
      createElement:tag=>tag==='section'?box:tag==='iframe'?{contentWindow:{postMessage:(data,target)=>sent.push({data,target})},remove(){}}:{}},
    addEventListener:(type,handler)=>listeners[type]=handler,setTimeout:fn=>{timers.push(fn);return 1;},clearTimeout(){},setInterval:fn=>{intervals.push(fn);return 1;},clearInterval(){},
    TextDecoder,Uint8Array,
    fetch:async(url,options)=>{requests.push({url,options,body:JSON.parse(options.body)});return new Promise(resolve=>sandbox.reply=answer=>{let sent=false;resolve({ok:true,body:{getReader:()=>({async read(){if(sent)return {done:true};sent=true;return {value:new TextEncoder().encode('event: delta\ndata: '+JSON.stringify({text:answer})+'\n\nevent: done\ndata: '+JSON.stringify({answer})+'\n\n'),done:false};},cancel:async()=>{}})}});});}};
  vm.createContext(sandbox);vm.runInContext(streamSource,sandbox);vm.runInContext(source,sandbox);
  function event(type,data={},overrides={}){listeners.message({origin:'http://127.0.0.1:17875',source:frame.contentWindow,
    data:{type:'daipai-selection-v1',channel:new URL(frame.src).searchParams.get('channel'),event:type,...data},...overrides});}
  function activate(){event('ready',{state:{enabled:false,revision:0}});toggle.checked=true;toggle.onchange();event('state',{state:{enabled:true,revision:1}});}
  return {sandbox,toggle,note,button,listeners,timers,intervals,sent,requests,event,activate,imported,urls,opened};
}
(async()=>{
  const missing=environment();assert.equal(missing.toggle.disabled,true);missing.timers[0]();assert.match(missing.note.textContent,/未连接/);assert.equal(missing.requests.length,0);
  const boundary=environment();boundary.event('ready',{state:{enabled:true}},{origin:'https://evil.invalid'});assert.equal(boundary.toggle.disabled,true);
  boundary.event('ready',{state:{enabled:true}},{source:{}});assert.equal(boundary.toggle.disabled,true);
  boundary.event('ready',{state:{enabled:true}},{data:{type:'daipai-selection-v1',channel:'wrong',event:'ready',state:{enabled:true}}});assert.equal(boundary.toggle.disabled,true);
  const chat=environment();chat.activate();assert.equal(chat.sent.at(-1).target,'http://127.0.0.1:17875');assert.equal(chat.requests.length,0);
  chat.event('explain',{id:'synthetic-id',text:'VRRP'});await tick();assert.equal(chat.requests.length,1);
  assert.equal(chat.requests[0].url,'/api/selection/explain');assert.equal(chat.requests[0].body.text,'VRRP');assert.equal(chat.requests[0].body.allow_context,true);assert.equal(Object.keys(chat.requests[0].body).length,2);
  chat.sandbox.reply('合成解释');await tick();assert.equal(chat.sent.at(-1).data.action,'result');assert.equal(chat.sent.at(-1).data.answer,'合成解释');
  chat.event('explain',{id:'cached',text:'VRRP'});await tick();assert.equal(chat.requests.length,1);assert.equal(chat.sent.at(-1).data.answer,'合成解释');
  chat.event('explain',{id:'retry',text:'VRRP',refresh:true});await tick();assert.equal(chat.requests.length,2);chat.sandbox.reply('新解释');await tick();
  chat.event('explain',{id:'next',text:'Python'});await tick();chat.toggle.checked=false;chat.toggle.onchange();assert.equal(chat.requests[2].options.signal.aborted,true);
  chat.event('state',{state:{enabled:false,revision:2}});chat.sandbox.reply('迟到答案');await tick();assert.notEqual(chat.sent.at(-1).data.answer,'迟到答案');
  chat.event('state',{state:{enabled:true,revision:1}});assert.equal(chat.toggle.checked,false);
  const identity=environment();identity.activate();identity.sandbox.accountEpoch++;identity.intervals[0]();assert.equal(identity.sent.at(-1).data.enabled,false);
  identity.listeners.pagehide();assert.equal(identity.sent.at(-1).data.enabled,false);
  const id='aa983c9d-afee-47d3-91a7-4587c57cf18b',continued=environment({followupId:id});
  continued.event('ready',{state:{enabled:false,revision:0,activeElsewhere:true}});await tick();assert.equal(continued.sent.at(-1).data.action,'followup');assert.equal(continued.sent.at(-1).data.id,id);
  continued.event('followup',{handoff:{id:'wrong',text:'错误',answer:'不导入'}});await tick();assert.equal(continued.imported.length,0);
  continued.event('followup',{handoff:{id,text:'幂等性',answer:'已有解释'}});await tick();assert.equal(continued.imported.length,1);assert.equal(continued.imported[0].answer,'已有解释');assert.deepEqual(continued.urls,['/']);assert.equal(continued.requests.length,0);
  const publicPage=environment({origin:'http://203.0.113.83:18890',crypto:{}});
  assert.equal(publicPage.note.textContent,'桌面入口');assert.equal(publicPage.timers.length,0);assert.equal(publicPage.requests.length,0);assert.equal(publicPage.toggle.disabled,true);
  publicPage.button.onclick();assert.deepEqual(publicPage.opened,[['http://127.0.0.1:17875/','_blank','noopener']]);
  const fallback=environment({crypto:{getRandomValues:require('node:crypto').randomFillSync}});assert.equal(fallback.timers.length,1);fallback.activate();assert.equal(fallback.toggle.checked,true);
  const unavailable=environment({crypto:{}});assert.equal(unavailable.note.textContent,'未连接');assert.equal(unavailable.toggle.disabled,true);
  console.log(JSON.stringify({selection_ui_checks:8,passed:8,model_api_calls:0,device_reads:0,native_mouse_acceptance:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
