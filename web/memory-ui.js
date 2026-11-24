// 记忆、自学习经验与设备只读授权管理（与 Agent v2 同步）。
let memoryKey='',memoryViewRevision=0,memoryRecord=null,memoryDraft=null,memoryRows=[],rollingRecord=null;
const agentMemoryKindLabels={
  preference:'用户偏好',
  correction:'纠错记录',
  troubleshooting_lesson:'排障经验',
  topology_fact:'拓扑规律',
};
const memoryOpen=add(document.querySelector('.topbar-actions'),'button','记忆与学习','toolbar-button');
memoryOpen.type='button';memoryOpen.id='memory-open';

function resetTaskMemory(){
  memoryViewRevision++;memoryKey='';memoryRecord=null;memoryDraft=null;memoryRows=[];rollingRecord=null;
  if(el('use-task-memory'))el('use-task-memory').checked=false;
  if(el('memory-consent'))el('memory-consent').hidden=true;
}

function syncTaskMemory(){
  const c=current(),key=accountEpoch+'/'+(c?.id||'');
  if(key!==memoryKey){resetTaskMemory();memoryKey=key;if(el('memory-dialog')?.open)el('memory-dialog').close();}
  memoryOpen.disabled=accountLoading||accountExpired;
  if(el('memory-consent'))el('memory-consent').hidden=true;
  if(el('use-task-memory')){el('use-task-memory').checked=false;el('use-task-memory').disabled=true;}
}

function selectedTaskMemory(){return {};}

function renderAgentMemoryPanel(agentMemory){
  const devBox=el('agent-granted-devices'),sumEl=el('agent-conv-summary'),listBox=el('agent-memory-list'),noteEl=el('agent-memory-note');
  if(!devBox||!sumEl||!listBox)return;
  devBox.replaceChildren();listBox.replaceChildren();
  const granted=agentMemory?.granted_devices||[];
  if(!granted.length){
    add(devBox,'p','暂未授权任何设备。首次只读排障时会向你询问一次。','record-notice');
  }else{
    const row=add(devBox,'div','','task-actions');
    for(const item of granted){
      const btn=add(row,'button',item.label+' · 撤销授权','secondary-button');
      btn.type='button';
      btn.onclick=()=>mutateAgentMemory({action:'revoke_device',device:item.id});
    }
    if(granted.length>1){
      const allBtn=add(row,'button','全部撤销','secondary-button');
      allBtn.type='button';
      allBtn.onclick=()=>mutateAgentMemory({action:'revoke_all_devices'});
    }
  }
  sumEl.textContent=agentMemory?.summary||'当前对话尚未触发滚动摘要（最近对话原文会直接保持完整上下文）。';
  const memories=agentMemory?.memories||[];
  const sharedMemories=agentMemory?.shared_memories||[];
  if(noteEl)noteEl.textContent=memories.length
    ?'个人记忆 '+memories.length+' 条'+(sharedMemories.length?' · 实验室共享经验 '+sharedMemories.length+' 条':'')+'，新对话会自动按需召回。'
    :'暂无个人长期记忆。排障后点“定位准确/纠正”或“总结本对话”可自动沉淀，也可以在下方手动添加。';
  for(const item of memories){
    const card=add(listBox,'section','','memory-source');
    const head=add(card,'div','','task-actions');
    const titleWrap=add(head,'div','','memory-title-wrap');
    add(titleWrap,'strong','['+(agentMemoryKindLabels[item.kind]||item.kind)+'] '+item.title);
    if(item.shared)add(titleWrap,'span','实验室共享','memory-badge-shared');
    if(item.confidence>1)add(titleWrap,'span','已验证 '+item.confidence+' 次','memory-badge-meta');
    if(item.hit_count>0)add(titleWrap,'span','命中 '+item.hit_count+' 次','memory-badge-meta');
    const btns=add(head,'div','','task-actions');
    if(item.kind!=='preference'){
      const shareBtn=add(btns,'button',item.shared?'取消共享':'共享给全实验室','secondary-button');
      shareBtn.type='button';
      shareBtn.onclick=()=>mutateAgentMemory({action:item.shared?'unshare_memory':'promote_shared',id:item.id});
    }
    const del=add(btns,'button','删除','secondary-button');
    del.type='button';
    del.onclick=()=>mutateAgentMemory({action:'delete_memory',id:item.id});
    add(card,'p',item.content);
    const meta=[];
    if(item.scope)meta.push('适用范围：'+item.scope);
    if(item.tags)meta.push('关键词：'+item.tags);
    if(meta.length)add(card,'small',meta.join(' · '));
  }
  const sharedBox=el('agent-shared-memory-list');
  if(sharedBox){
    sharedBox.replaceChildren();
    if(!sharedMemories.length){
      add(sharedBox,'p','暂无全实验室共享经验。你可以把高价值的个人排障经验点击“共享给全实验室”，让全组新对话都能直接受益。','record-notice');
    }else{
      for(const item of sharedMemories){
        const card=add(sharedBox,'section','','memory-source');
        const head=add(card,'div','','task-actions');
        const titleWrap=add(head,'div','','memory-title-wrap');
        add(titleWrap,'strong','['+(agentMemoryKindLabels[item.kind]||item.kind)+'] '+item.title);
        if(item.confidence>1)add(titleWrap,'span','已验证 '+item.confidence+' 次','memory-badge-meta');
        if(item.hit_count>0)add(titleWrap,'span','命中 '+item.hit_count+' 次','memory-badge-meta');
        if(item.is_mine){
          const unshare=add(head,'button','取消共享','secondary-button');
          unshare.type='button';
          unshare.onclick=()=>mutateAgentMemory({action:'unshare_memory',id:item.id});
        }
        add(card,'p',item.content);
        if(item.scope)add(card,'small','适用范围：'+item.scope);
      }
    }
  }
}

async function mutateAgentMemory(payload){
  const c=current(),conv=c?c.id:'default';
  const noteEl=el('agent-memory-note');
  try{
    const data=await api('/api/conversations/'+encodeURIComponent(conv)+'/memory',payload);
    if(data.agent_memory)renderAgentMemoryPanel(data.agent_memory);
    return data;
  }catch(error){
    if(noteEl)noteEl.textContent=error.message;
    return false;
  }
}

async function loadTaskMemory(){
  const c=current(),conv=c?c.id:'default',epoch=accountEpoch,view=++memoryViewRevision;
  const noteEl=el('agent-memory-note');
  if(noteEl)noteEl.textContent='正在加载记忆与设备授权…';
  try{
    const data=await api('/api/conversations/'+encodeURIComponent(conv)+'/memory');
    if(epoch!==accountEpoch||view!==memoryViewRevision||!el('memory-dialog').open)return;
    renderAgentMemoryPanel(data.agent_memory||{granted_devices:[],summary:'',memories:[]});
  }catch(error){
    if(epoch===accountEpoch&&view===memoryViewRevision&&noteEl)noteEl.textContent=error.message;
  }
}

memoryOpen.onclick=()=>{el('memory-dialog').showModal();loadTaskMemory();};
if(el('memory-refresh'))el('memory-refresh').onclick=loadTaskMemory;
if(el('agent-consolidate-btn')){
  el('agent-consolidate-btn').onclick=async()=>{
    const btn=el('agent-consolidate-btn'),noteEl=el('agent-memory-note');
    btn.disabled=true;
    if(noteEl)noteEl.textContent='正在复盘本对话并归纳可复用经验…';
    try{
      const res=await mutateAgentMemory({action:'consolidate_conversation'});
      if(res&&noteEl){
        const items=Array.isArray(res.consolidated)?res.consolidated:[];
        noteEl.textContent=items.length
          ?'已归纳沉淀 '+items.length+' 条经验：'+items.map(i=>i.title).join('、')
          :'本对话暂无需要新增沉淀的通用经验（已有记忆已保持最新）。';
      }
    }finally{btn.disabled=false;}
  };
}
if(el('agent-memory-add-form')){
  el('agent-memory-add-form').onsubmit=async event=>{
    event.preventDefault();
    const title=el('agent-mem-title').value.trim(),content=el('agent-mem-content').value.trim();
    if(!title||!content)return;
    const btn=el('agent-mem-submit');btn.disabled=true;
    try{
      const saved=await mutateAgentMemory({action:'add_memory',kind:el('agent-mem-kind').value,title,content,scope:'手动记录'});
      if(saved)el('agent-memory-add-form').reset();
    }finally{btn.disabled=false;}
  };
}
if(el('memory-dialog'))el('memory-dialog').addEventListener('close',()=>{memoryViewRevision++;});
syncTaskMemory();
