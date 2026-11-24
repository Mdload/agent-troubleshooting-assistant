// 仅在服务端返回本次维护任务的具体修复方案时展示入口。
const repairRequests=new Set();
const repairLoaded=new Set();
const repairWindowCache=new Map(),repairWindowRequests=new Set();
function renderRepairPermission(parent,task){
  if(!task||!['pending','approved'].includes(task.status)||!account||accountExpired)return;
  if(!['remote-primary','remote-backup'].every(device=>['services','interfaces'].every(check=>task.scope?.[device]?.includes(check))))return;
  const key=accountEpoch+':'+task.id,box=add(parent,'div','','task-card'),window=repairWindowCache.get(key);
  const labels={pending:'修复权限等待管理员批准',active:'本次固定动作已获限时授权',revoked:'本次修复权限已撤销',expired:'本次修复权限已过期'};
  add(box,'small',window?(labels[window.state]||window.state):'默认只读。需要时可提前申请本次受限修复权限。');
  if(!window){
    const button=add(box,'button','申请修复权限','secondary-button');button.type='button';button.disabled=repairWindowRequests.has(key);
    button.onclick=async()=>{
      if(repairWindowRequests.has(key))return;const epoch=accountEpoch;repairWindowRequests.add(key);button.disabled=true;
      try{const value=await api('/api/repair-windows',{task:task.id});if(epoch!==accountEpoch)return;repairWindowCache.set(key,value);render();}
      catch(error){if(epoch===accountEpoch)el('status').textContent=error.message;}
      finally{repairWindowRequests.delete(key);if(epoch===accountEpoch)button.disabled=false;}
    };
  }
}
function renderRepair(parent,message){
  const task=taskOf(message),plan=message.repairPlan||task?.result?.repair_plan||message.result?.repair_plan;
  renderRepairPermission(parent,task);
  const assessment=task?.result?.ha_service_assessment||message.result?.ha_service_assessment;
  if(assessment){const finding=add(parent,'div','','task-card');add(finding,'small','本轮服务状态核对');add(finding,'p',assessment.summary);}
  if(!plan)return;
  const loadKey=accountEpoch+':'+plan.id;
  if(!repairLoaded.has(loadKey)){
    repairLoaded.add(loadKey);const identity=accountEpoch;
    void api('/api/repairs/'+plan.id).then(current=>{if(identity===accountEpoch&&current.state!==plan.state){message.repairPlan=current;render();}}).catch(()=>{});
  }
  const box=add(parent,'div','','task-card');add(box,'strong','恢复备机热备服务');
  add(box,'p',plan.plan.reason);
  const states={pending:'待确认：只启动一次指定服务，随后自动复查。',approved:'已确认，可以开始恢复。',running:'正在恢复和复查…',checking:'只读核查中，不会重新执行。',reviewed:'只读核查已结束。',cancel_requested:'已请求停止后续步骤；已发出的操作可能完成。',cancelled:'本次恢复已取消。',completed:'恢复检查已结束。',refused:'本次操作未下发。',unknown:'操作结果尚未确认，可只读核查，不能重复执行。'};
  add(box,'p',states[plan.state]||plan.state);
  if(plan.result){
    const descriptions={service_restored:'热备服务已启动，VIP仍在主机，备机业务服务保持停止。',service_restored_confirmed:'设备执行记录与当前状态一致，恢复核查通过。',state_verified_execution_unconfirmed:'当前服务与VIP状态符合预期；无法确认是否由本次操作恢复。',no_action_needed:'当前已符合服务检查条件，没有再次启动。'};
    add(box,'p',descriptions[plan.result.status]||'本次未确认恢复完成。');
    if(plan.result.business_verified!==true)add(box,'small','尚未验证端到端业务。');
  }
  const actions=add(box,'div','','task-actions');
  async function perform(action,body={}){
    if(repairRequests.has(plan.id))return;
    const identity=accountEpoch;repairRequests.add(plan.id);render();
    try{
      let value=await api('/api/repairs/'+plan.id+'/'+action,body);
      if(identity!==accountEpoch)return;message.repairPlan=value;
      if(action==='decision'&&body.approved===true){render();value=await api('/api/repairs/'+plan.id+'/execute',{});if(identity!==accountEpoch)return;message.repairPlan=value;}
      save();render();
    }catch(error){if(identity===accountEpoch)el('status').textContent='恢复未完成：'+error.message;}
    finally{repairRequests.delete(plan.id);if(identity===accountEpoch)render();}
  }
  if(['pending','approved'].includes(plan.state)){
    const button=add(actions,'button',plan.state==='pending'?'同意并恢复':'开始恢复','primary-button');button.type='button';
    button.disabled=repairRequests.has(plan.id)||Date.now()/1000>=plan.expires;
    button.onclick=()=>perform(plan.state==='pending'?'decision':'execute',plan.state==='pending'?{approved:true}:{});
    const cancel=add(actions,'button','取消','secondary-button');cancel.type='button';cancel.disabled=repairRequests.has(plan.id);cancel.onclick=()=>perform('cancel');
  }
  if(['unknown','reviewed','refused'].includes(plan.state)||plan.state==='completed'&&plan.result?.status==='verification_incomplete'){
    const inspect=add(actions,'button','只读核查结果','secondary-button');inspect.type='button';inspect.title='核对设备执行记录及主备服务、VIP，不会再次下发启动命令。';inspect.disabled=repairRequests.has(plan.id);inspect.onclick=()=>perform('reconcile');
  }
}

async function loadRepairPermissions(){
  const epoch=accountEpoch;el('repair-permissions-note').textContent='';
  try{
    const data=await api('/api/repair-windows');if(epoch!==accountEpoch)return;
    const list=el('repair-permissions-list');list.replaceChildren();
    for(const item of data.windows){repairWindowCache.set(epoch+':'+item.task,item);renderPermissionRow(list,item,false);}
    if(!data.windows.length)add(list,'p','暂无申请。请在远端主备排障开始前，从排障卡片申请。');
    const auth=await api('/api/admin/status');if(epoch!==accountEpoch)return;
    el('repair-admin-form').hidden=auth.authenticated;
    if(auth.authenticated){const queue=await api('/api/admin/repair-windows');if(epoch!==accountEpoch)return;const box=el('repair-admin-list');box.replaceChildren();for(const item of queue.windows)renderPermissionRow(box,item,true);}
    else el('repair-admin-list').replaceChildren();
    render();
  }catch(error){if(epoch===accountEpoch)el('repair-permissions-note').textContent=error.message;}
}
function renderPermissionRow(parent,item,admin){
  const box=add(parent,'div','','task-card');add(box,'strong','远端备机 · 启动 Keepalived 一次');
  add(box,'p',item.question);const labels={pending:'待批准',active:'已批准',revoked:'已撤销',expired:'已过期'};
  add(box,'small',(labels[item.state]||item.state)+' · '+new Date(item.expires*1000).toLocaleTimeString()+' 到期');
  const actions=add(box,'div','','task-actions');
  async function apply(action,body={}){
    const epoch=accountEpoch;const key='window:'+item.id;if(repairWindowRequests.has(key))return;repairWindowRequests.add(key);
    try{await api('/api/'+(admin?'admin/':'')+'repair-windows/'+item.id+'/'+action,body);if(epoch===accountEpoch)await loadRepairPermissions();}
    catch(error){if(epoch===accountEpoch)el('repair-permissions-note').textContent=error.message;}
    finally{repairWindowRequests.delete(key);}
  }
  if(admin&&item.state==='pending'){
    const duration=add(actions,'select','');duration.setAttribute('aria-label','维护时限');
    for(const minutes of [5,10,15]){const option=add(duration,'option',minutes+' 分钟');option.value=String(minutes);if(minutes===10)option.selected=true;}
    duration.value='10';const approve=add(actions,'button','批准一次','primary-button');approve.type='button';approve.onclick=()=>apply('decision',{approved:true,duration_minutes:Number(duration.value)});
  }
  if(['pending','active'].includes(item.state)){const revoke=add(actions,'button',admin?'撤销 / 拒绝':'撤回申请','secondary-button');revoke.type='button';revoke.onclick=()=>apply('revoke');}
}
el('repair-permissions-open').onclick=()=>{el('repair-admin-password').value='';if(!el('repair-permissions-dialog').open)el('repair-permissions-dialog').showModal();void loadRepairPermissions();};
el('repair-permissions-refresh').onclick=()=>void loadRepairPermissions();
el('repair-admin-form').onsubmit=async event=>{
  event.preventDefault();const epoch=accountEpoch;
  try{await api('/api/admin/login',{password:el('repair-admin-password').value});if(epoch===accountEpoch)await loadRepairPermissions();}
  catch(error){if(epoch===accountEpoch)el('repair-permissions-note').textContent=error.message;}
  finally{el('repair-admin-password').value='';}
};
el('repair-permissions-dialog').addEventListener('close',()=>{el('repair-admin-password').value='';});
