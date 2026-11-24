// 页面状态仅控制阅读；历史调查独立取自服务器，不放入模型短历史。
let workspaceChat='',workspaceSignature='',followLatest=true,recordRevision=0;
function syncScrollButton(){
  const pane=el('conversation-scroll');
  el('jump-latest').hidden=pane.scrollHeight-pane.scrollTop-pane.clientHeight<100;
}
function jumpLatest(){
  followLatest=true;const pane=el('conversation-scroll');pane.scrollTop=pane.scrollHeight;syncScrollButton();
}
function syncWorkspace(){
  const c=current(),key=accountEpoch+'/'+(c?.id||''),signature=(c?.messages||[]).map(m=>m.content.length).join(',')+'/'+busy;
  el('investigation-open').disabled=!c||accountLoading||accountExpired;
  if(key!==workspaceChat){workspaceChat=key;followLatest=true;recordRevision++;
    el('investigation-list').replaceChildren();el('investigation-detail').replaceChildren();
    el('investigation-note').textContent='';if(el('investigation-dialog').open)el('investigation-dialog').close();}
  if(signature!==workspaceSignature||key!==el('conversation-scroll').dataset.chat){
    workspaceSignature=signature;el('conversation-scroll').dataset.chat=key;
    requestAnimationFrame(()=>{if(key===workspaceChat){if(followLatest)jumpLatest();else syncScrollButton();}});
  }else syncScrollButton();
}
function recordDate(value){return value?new Date(value*1000).toLocaleString():'时间未记录';}
async function showInvestigation(id){
  const c=current();if(!c)return;const revision=++recordRevision,epoch=accountEpoch,conversation=c.id;
  el('investigation-detail').replaceChildren();el('investigation-note').textContent='正在核对原调查的归属与状态…';
  try{
    const record=await api('/api/conversations/'+encodeURIComponent(conversation)+'/investigations/'+id);
    if(revision!==recordRevision||epoch!==accountEpoch||current()?.id!==conversation||!el('investigation-dialog').open)return;
    el('investigation-note').textContent=record.note;
    const box=add(el('investigation-detail'),'section','','historical-answer');
    add(box,'h3',record.question);add(box,'p',recordDate(record.started_at)+' · '+(taskStates[record.status]||record.status)+' · '+record.reads+'项读取','experience-note');
    if(record.result){const targets=new Map();renderAnswer(add(box,'div','','message'),record.result.answer||'原调查没有回答正文。',record.result,targets);evidence(box,record.result,targets);}
    else add(box,'p','此申请没有记录结果；未重新执行调查。');
    box.scrollIntoView({block:'start',behavior:'smooth'});
  }catch(error){if(revision===recordRevision&&epoch===accountEpoch)el('investigation-note').textContent=error.message;}
}
async function loadInvestigationRecords(){
  const c=current();if(!c)return;const revision=++recordRevision,epoch=accountEpoch,conversation=c.id;
  el('investigation-note').textContent='正在读取本对话的已结束调查…';el('investigation-list').replaceChildren();el('investigation-detail').replaceChildren();
  try{
    const data=await api('/api/conversations/'+encodeURIComponent(conversation)+'/investigations');
    if(revision!==recordRevision||epoch!==accountEpoch||current()?.id!==conversation||!el('investigation-dialog').open)return;
    el('investigation-note').textContent=data.note;
    if(!data.records.length)add(el('investigation-list'),'p','本对话还没有已结束的调查。问答和上传材料分析不会生成设备调查。','experience-note');
    for(const record of data.records){
      const row=add(el('investigation-list'),'button','','record-item');row.type='button';
      add(row,'strong',record.question);add(row,'small',(taskStates[record.status]||record.status)+' · '+recordDate(record.started_at)+' · '+record.reads+'项读取');
      row.onclick=()=>showInvestigation(record.id);
    }
    if(data.has_more)add(el('investigation-list'),'p','显示最近20次已结束调查；更早审计仍保留在服务器。','experience-note');
  }catch(error){if(revision===recordRevision&&epoch===accountEpoch)el('investigation-note').textContent=error.message;}
}
function resetWorkspaceUI(){
  recordRevision++;workspaceChat='';workspaceSignature='';followLatest=true;
  el('history-search').value='';el('investigation-list').replaceChildren();el('investigation-detail').replaceChildren();el('investigation-note').textContent='';
}
el('investigation-open').onclick=()=>{el('investigation-dialog').showModal();loadInvestigationRecords();};
el('investigation-refresh').onclick=loadInvestigationRecords;
el('investigation-dialog').addEventListener('close',()=>{recordRevision++;});
el('history-search').addEventListener('input',sidebar);
el('jump-latest').onclick=jumpLatest;
el('conversation-scroll').addEventListener('scroll',()=>{const pane=el('conversation-scroll');followLatest=pane.scrollHeight-pane.scrollTop-pane.clientHeight<100;syncScrollButton();},{passive:true});
window.addEventListener('resize',syncScrollButton);
