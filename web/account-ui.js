// 账号历史包含正文和服务器签发的建议卡引用。
let historyQueue=Promise.resolve();
let accountSubmitting=false;
let labRevision=0,labSubmitting=false;
function historySnapshot(chat){
  const messages=chat.messages.filter(m=>!m.recoveredTask).map(m=>{
    const ids=(m.result?.proposed_changes||[]).map(card=>card.proposal_id).filter(Boolean);
    return {role:m.role,content:m.content,context:m.context===true,
      ...(Number.isInteger(m.contextRevision)&&m.contextRevision>=0?{contextRevision:m.contextRevision}:{}),
      ...(ids.length?{proposed_changes:ids}:{})};
  });
  return {title:chat.title,messages};
}
function resetIdentityView(){
  accountEpoch++;
  for(const id of [...taskPollers.keys()])stopWatchingTask(id);
  for(const run of chatRuns.values())run.finish?.();
  chatRuns.clear();answerReveal=null;
  chats=[];activeId=null;modeLoading=false;
  executionMode='approval';
  resetChatSpaces();
  el('question').value='';el('admin-password').value='';
  el('repair-admin-password').value='';
  for(const id of ['repair-permissions-list','repair-admin-list'])el(id).replaceChildren();
  for(const id of ['account-lab-password','lab-password','lab-password-confirm'])el(id).value='';
  labRevision=0;el('lab-password-note').textContent='';
  el('admin-login').hidden=false;el('admin-panel').hidden=true;
  for(const id of ['admin-list','skills-list','admin-experiences','comparison-body'])el(id).replaceChildren();
  for(const dialog of document.querySelectorAll('dialog'))if(dialog.open&&dialog.id!=='account-dialog')dialog.close();
  historyQueue=Promise.resolve();el('storage-note').textContent='';
  if(typeof resetMaterialsUI==='function')resetMaterialsUI();
  if(typeof resetWorkspaceUI==='function')resetWorkspaceUI();
  if(typeof resetTaskMemory==='function')resetTaskMemory();
}
function expireAccount(){
  resetIdentityView();account=null;accountExpired=true;accountLoading=false;
  el('status').textContent='个人账号会话已失效，请重新登录；未恢复旧权限。';render();
}
async function loadHistoryChat(chat){
  const data=await api('/api/history/'+encodeURIComponent(chat.id));
  chat.title=data.title;chat.updated=data.updated;chat.serverRevision=data.revision;
  chat.legacyHistory=data.messages.some(m=>typeof m.context!=='boolean');
  chat.messages=data.messages.map(m=>({role:m.role,content:m.content,context:m.context===true,
    ...(m.proposed_changes?.length?{result:{proposed_changes:m.proposed_changes}}:{}),
    ...(Number.isInteger(m.contextRevision)&&m.contextRevision>=0?{contextRevision:m.contextRevision}:{})}));
  chat.loaded=true;chat.restored=true;chat.lastQueued=JSON.stringify(historySnapshot(chat));
}
async function loadAccount(){
  accountLoading=true;
  try{
    const state=await api('/api/account');
    if(state.authenticated){
      resetIdentityView();account=state.account;accountExpired=false;
      const data=await api('/api/history');
      chats=data.chats.map(c=>({...c,serverRevision:c.revision,messages:[],loaded:false}));
      activeId=preferredChat('general')?.id||null;if(current())await loadHistoryChat(current());
      el('storage-note').textContent='个人文字历史保存在服务器。';
    }else if(state.expired){expireAccount();}
    else{account=null;accountExpired=false;}
  }catch(error){expireAccount();throw error;}
  finally{accountLoading=false;render();}
}
function queueHistorySaves(){
  if(!account||accountLoading||accountExpired)return;
  const epoch=accountEpoch;
  for(const chat of chats){
    if(chatRuns.has(chat.id)||chat.loaded===false||!chat.messages.length||chat.syncBlocked)continue;
    const snapshot=historySnapshot(chat),signature=JSON.stringify(snapshot);
    if(chat.lastQueued===signature)continue;
    chat.lastQueued=signature;el('storage-note').textContent='正在保存个人文字历史…';
    historyQueue=historyQueue.then(async()=>{
      if(epoch!==accountEpoch||chat.syncBlocked)return;
      try{
        const saved=await api('/api/history',{id:chat.id,...snapshot,expected_revision:chat.serverRevision||0});
        if(epoch!==accountEpoch)return;
        chat.serverRevision=saved.revision;chat.updated=Math.max(chat.updated,saved.updated);
        el('storage-note').textContent='个人文字历史已保存。';
      }catch(error){
        if(epoch!==accountEpoch)return;
        chat.lastQueued=null;if(error.status===409||error.status===404)chat.syncBlocked=true;
        el('storage-note').textContent='历史尚未保存：'+error.message+'。可从个人账号导出当前对话。';
      }
    });
  }
}
async function deleteHistoryChat(chat){
  await historyQueue;
  if(Number.isInteger(chat.serverRevision))await api('/api/history/'+encodeURIComponent(chat.id)+'/delete',{expected_revision:chat.serverRevision});
  chat.syncBlocked=true;
}
function showAccount(){
  el('account-panel').hidden=!(account||accountExpired);el('account-form').hidden=!!account&&!accountExpired;
  el('account-note').textContent=account?'当前账号：'+account.name:'登录后使用个人服务器历史；访客聊天仍只保存在本浏览器。';
  if(accountExpired)el('account-note').textContent='会话已失效，请重新登录。';
  el('account-reload').disabled=!account;el('account-export').disabled=!current();
  el('account-password').value='';el('account-storage').checked=false;
  el('account-lab-password').value='';el('lab-registration-settings').hidden=!account;
  if(account)void loadLabRegistration();
  if(!el('account-dialog').open)el('account-dialog').showModal();
}
async function submitAccount(register){
  if(accountSubmitting)return;
  if(!el('account-form').reportValidity())return;
  const data={username:el('account-name').value,password:el('account-password').value};
  if(register){
    if(!el('account-storage').checked){el('account-note').textContent='注册前请确认服务器文字历史保存范围。';return;}
    data.allow_storage=true;
    data.lab_password=el('account-lab-password').value;
    if(!data.lab_password){el('account-note').textContent='注册时请输入实验室口令';return;}
  }
  el('account-login').disabled=true;el('account-register').disabled=true;
  accountSubmitting=true;
  try{
    await api('/api/account/'+(register?'register':'login'),data);
    resetIdentityView();account=null;accountLoading=true;
    await loadAccount();await loadRegistry();await loadMode();
    el('account-dialog').close();el('status').textContent='已登录。';
  }catch(error){el('account-note').textContent=error.message;}
  finally{data.password='';data.lab_password='';el('account-password').value='';el('account-lab-password').value='';el('account-login').disabled=false;el('account-register').disabled=false;accountSubmitting=false;}
}
async function loadLabRegistration(){
  const epoch=accountEpoch;el('lab-password-save').disabled=true;
  try{const state=await api('/api/account/registration');if(epoch!==accountEpoch)return;labRevision=state.revision;el('lab-password-note').textContent=state.configured?'已设置；可直接保存新口令。':'尚未设置，请先保存实验室口令。';el('lab-password-save').disabled=false;}
  catch(error){if(epoch===accountEpoch)el('lab-password-note').textContent=error.message;}
}
el('lab-password-form').onsubmit=async event=>{
  event.preventDefault();if(labSubmitting||!account)return;
  if(!el('lab-password-form').reportValidity())return;
  const password=el('lab-password').value;
  if(password!==el('lab-password-confirm').value){el('lab-password-note').textContent='两次输入的口令不一致';return;}
  const epoch=accountEpoch;labSubmitting=true;el('lab-password-save').disabled=true;
  try{const state=await api('/api/account/registration',{password,expected_revision:labRevision});if(epoch!==accountEpoch)return;labRevision=state.revision;el('lab-password-note').textContent='已保存，新用户注册时使用此口令；已有账号不受影响。';}
  catch(error){if(epoch===accountEpoch){el('lab-password-note').textContent=error.message;if(error.status===409)await loadLabRegistration();}}
  finally{labSubmitting=false;if(epoch===accountEpoch){el('lab-password').value='';el('lab-password-confirm').value='';el('lab-password-save').disabled=false;}}
};
function exportCurrentHistory(){
  const chat=current();if(!chat)return;
  const transcript=historySnapshot(chat);
  transcript.messages=transcript.messages.map((message,index)=>({...message,
    content:message.role==='assistant'?answerCopyText(message.content,chat.messages[index]?.result):message.content}));
  const bytes=JSON.stringify({kind:'personal_text_transcript',id:chat.id,...transcript},null,2);
  const url=URL.createObjectURL(new Blob([bytes],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download='带派助手-对话-'+chat.id+'.json';
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
el('account-open').onclick=showAccount;
el('account-form').onsubmit=event=>{event.preventDefault();submitAccount(false);};
el('account-register').onclick=()=>submitAccount(true);
el('account-export').onclick=exportCurrentHistory;
el('account-reload').onclick=async()=>{
  if(!confirm('重新读取服务器历史会替换本页内容。请先导出尚未保存的对话。继续？'))return;
  try{await historyQueue;await loadAccount();await loadMode();el('account-dialog').close();}
  catch(error){el('account-note').textContent=error.message;}
};
el('account-logout').onclick=async()=>{
  try{
    await historyQueue;await api('/api/account/logout',{});
    resetIdentityView();account=null;accountExpired=false;accountLoading=false;
    el('account-dialog').close();render();await api('/api/session');await loadRegistry();
    el('status').textContent='已退出；未结束调查已取消。个人历史保留在服务器，重新登录后查看。';
  }catch(error){el('account-note').textContent=error.message;}
};
el('account-dialog').addEventListener('close',()=>{for(const id of ['account-password','account-lab-password','lab-password','lab-password-confirm'])el(id).value='';});
