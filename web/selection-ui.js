/* DAIPAI_SELECTION_SERVER_V1: 网页持有账号，本机组件只负责取词和浮窗。 */
(()=>{
  'use strict';
  if(document.getElementById('desktop-selection-switch'))return;
  const localOrigin='http://127.0.0.1:17875';
  const style=document.createElement('style');
  style.textContent='.desktop-selection{display:flex;align-items:center;gap:3px;padding:8px;margin:2px 0 8px;border-radius:10px;color:#58616c;font-size:13px;transition:background .15s}.desktop-selection.is-on{background:#eaf1ec;color:#456c55}.selection-row{position:relative;display:flex;align-items:center;gap:9px;flex:1;min-width:0;cursor:pointer}.selection-row>svg{width:17px;height:17px;stroke:currentColor;stroke-width:1.5;fill:none;stroke-linejoin:round}.selection-title{flex:1;white-space:nowrap;font-weight:500}.selection-state{max-width:64px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;color:#a0a7af}.selection-row input{position:absolute;right:0;width:30px;height:19px;margin:0;opacity:0;cursor:pointer;z-index:1}.selection-switch{position:relative;width:30px;height:18px;flex-shrink:0;border-radius:10px;background:#d4d9df;transition:background .18s}.selection-switch:after{content:"";position:absolute;top:3px;left:3px;width:12px;height:12px;border-radius:50%;background:white;box-shadow:0 1px 3px #0002;transition:transform .18s}.selection-row input:checked+.selection-switch{background:#659178}.selection-row input:checked+.selection-switch:after{transform:translateX(12px)}.selection-row input:focus-visible+.selection-switch{outline:2px solid #95b3a0;outline-offset:3px}.selection-row input:disabled+.selection-switch{opacity:.45}.selection-reconnect{display:grid;place-items:center;width:24px;height:24px;padding:0;flex-shrink:0;border:0;border-radius:6px;background:transparent;color:#9ba4ad;cursor:pointer}.selection-reconnect:hover{background:#e7ebef;color:#525e68}.selection-reconnect svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}.desktop-selection iframe{display:none}';
  document.head.append(style);
  const box=document.createElement('section');box.className='desktop-selection';
  box.innerHTML='<label class="selection-row"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6Z"/></svg><span class="selection-title">划词助手</span><span id="desktop-selection-status" class="selection-state" role="status">连接中</span><input id="desktop-selection-switch" type="checkbox" aria-label="跨软件划词解释" disabled><span class="selection-switch" aria-hidden="true"></span></label><button id="desktop-selection-connect" class="selection-reconnect" type="button" title="启动桌面组件后重新连接" aria-label="重新连接划词助手"><svg viewBox="0 0 24 24"><path d="M20 10a8 8 0 1 0-1 7M20 4v6h-6"/></svg></button>';
  (document.querySelector('.sidebar-footer')||document.body).prepend(box);
  const toggle=box.querySelector('input'),note=box.querySelector('[role=status]'),connectButton=box.querySelector('button');
  // 公网HTTP不能直接访问本机服务。使用用户主动打开的本机页面，组件直接转发到公网后端。
  if(location.origin==='http://203.0.113.83:18890'){
    note.textContent='桌面入口';note.title='先启动本机划词组件，再打开划词入口；不需要SSH隧道。';
    connectButton.hidden=false;connectButton.textContent='打开';connectButton.title='打开本机划词入口';
    connectButton.style.width='auto';connectButton.style.padding='0 6px';
    connectButton.onclick=()=>window.open(localOrigin+'/','_blank','noopener');
    return;
  }
  let frame=null,channel='',connected=false,enabled=false,revision=-1,pending=null,connectionTimer=null;
  let epoch=typeof accountEpoch==='number'?accountEpoch:0;
  const recentExplanations=new Map();
  let followupId=/^#selection-followup=([-a-z0-9]{36})$/i.exec(location.hash||'')?.[1]||'';
  async function requestFollowup(){
    if(!followupId)return;
    if(typeof assistantStartup!=='undefined'&&!(await assistantStartup)){el('status').textContent='助手尚未就绪，请刷新后接续划词。';return;}
    if(connected)send('followup',{id:followupId});
  }
  async function importFollowup(data){
    if(!followupId||data.id!==followupId)return;
    try{
      await window.createSelectionConversation(data);
      followupId='';history.replaceState(null,'',location.pathname+location.search);
    }catch(error){el('status').textContent='未能接续划词：'+error.message;}
  }
  function cancel(){pending?.abort();pending=null;}
  function send(action,data={}){frame?.contentWindow?.postMessage({type:'daipai-selection-v1',channel,action,...data},localOrigin);}
  function state(value){
    if(Number.isInteger(value.revision)&&value.revision<revision)return;
    if(Number.isInteger(value.revision))revision=value.revision;
    enabled=!!value.enabled;toggle.checked=enabled;toggle.disabled=!connected;
    box.className='desktop-selection'+(enabled?' is-on':'');connectButton.hidden=connected;
    note.textContent=value.error?'连接异常':enabled?'已开启':value.activeElsewhere?'其他窗口':'已关闭';
    note.title=value.error||(enabled?'选中文字，点击解释':value.activeElsewhere?'开启后切换到本页':'划词时不弹出工具条');
    if(!enabled)cancel();
  }
  function disconnected(message){connected=false;enabled=false;toggle.checked=false;toggle.disabled=true;cancel();box.className='desktop-selection';connectButton.hidden=false;note.textContent='未连接';note.title=message||'启动桌面组件后点击重连';}
  function connect(){
    send('state',{enabled:false});cancel();frame?.remove();connected=false;enabled=false;revision=-1;
    toggle.checked=false;toggle.disabled=true;connectButton.hidden=true;note.textContent='连接中';
    try{
      if(typeof crypto.randomUUID==='function')channel=crypto.randomUUID();
      else{const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);channel=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');}
      frame=document.createElement('iframe');frame.title='本机划词组件连接';frame.hidden=true;
      frame.src=localOrigin+'/__desktop/bridge?origin='+encodeURIComponent(location.origin)+'&channel='+encodeURIComponent(channel);
      box.append(frame);clearTimeout(connectionTimer);connectionTimer=setTimeout(()=>{if(!connected)disconnected();},5000);
    }catch(_){disconnected('浏览器未能建立本机连接，请通过桌面划词入口打开。');}
  }
  async function explain(data){
    if(!enabled||typeof data.text!=='string'||[...data.text].length>800||typeof data.id!=='string')return;
    cancel();const request=new AbortController();pending=request;const identity=typeof accountEpoch==='number'?accountEpoch:0;
    try {
      const active=()=>enabled&&!request.signal.aborted&&identity===(typeof accountEpoch==='number'?accountEpoch:0);
      const cached=recentExplanations.get(data.text);
      if(!data.refresh&&cached&&cached.identity===identity&&Date.now()-cached.time<300000){send('result',{id:data.id,answer:cached.answer});return;}
      const answer=await window.readSelectionExplanation(data.text,request.signal,text=>{if(active())send('partial',{id:data.id,answer:text});});
      if(!enabled||request.signal.aborted||identity!==(typeof accountEpoch==='number'?accountEpoch:0))return;
      if(!answer)throw Error('没有取得解释，请重新选择后再试。');send('result',{id:data.id,answer});
      recentExplanations.delete(data.text);recentExplanations.set(data.text,{answer,identity,time:Date.now()});
      while(recentExplanations.size>10)recentExplanations.delete(recentExplanations.keys().next().value);
    }catch(error){if(enabled&&!request.signal.aborted&&identity===(typeof accountEpoch==='number'?accountEpoch:0))send('result',{id:data.id,error:error.message});}
    finally{if(pending===request)pending=null;}
  }
  addEventListener('message',event=>{
    const data=event.data;if(event.origin!==localOrigin||event.source!==frame?.contentWindow||!data||data.type!=='daipai-selection-v1'||data.channel!==channel)return;
    if(data.event==='ready'){clearTimeout(connectionTimer);connected=true;state(data.state);void requestFollowup();}
    if(data.event==='state'&&connected)state(data.state);
    if(data.event==='error'){note.textContent=data.error||'操作未完成，请重新连接。';toggle.checked=enabled;toggle.disabled=!connected;}
    if(data.event==='disconnected')disconnected();
    if(data.event==='cancel')cancel();
    if(data.event==='explain')void explain(data);
    if(data.event==='followup'&&data.handoff)void importFollowup(data.handoff);
    if(data.event==='followup-error'&&followupId)el('status').textContent=data.error||'未能接续划词，请重新解释后再试。';
  });
  toggle.onchange=()=>{
    if(!connected)return;
    if(!toggle.checked)cancel();
    if(toggle.checked&&typeof accountLoading!=='undefined'&&(accountLoading||accountExpired)){toggle.checked=enabled;note.textContent='请先完成登录或刷新页面。';return;}
    toggle.disabled=true;send('state',{enabled:toggle.checked});
  };
  connectButton.onclick=connect;
  const identityTimer=setInterval(()=>{const now=typeof accountEpoch==='number'?accountEpoch:0;if(now!==epoch){epoch=now;recentExplanations.clear();cancel();if(connected)send('state',{enabled:false});}},500);
  addEventListener('pagehide',()=>{clearTimeout(connectionTimer);clearInterval(identityTimer);recentExplanations.clear();cancel();send('state',{enabled:false});frame?.remove();});
  connect();
})();
