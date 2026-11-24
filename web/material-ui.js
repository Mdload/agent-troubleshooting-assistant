// 每次只分析一份材料；账号历史不自动恢复材料、外发同意或设备权限。
const materialMaxBytes=256*1024;
let materialScope='',materialSaving=false,materialViewRevision=0;
function inputLength(text){return Array.from(text).length;}
function bytesBase64(bytes){
  let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(text);
}
function updateInputMeter(){
  const count=inputLength(el('question').value),long=count>1000;
  el('input-meter').textContent=count>800?(long?'已输入'+count+'字符；原输入完整保留，请转为材料后用短问题分析。':count+' / 1000字符'):'';
  el('convert-long').hidden=!long;el('convert-long').disabled=busy||modeLoading||accountLoading||accountExpired;
}
function renderMaterials(){
  const chat=current(),material=chat?.material,scope=accountEpoch+'/'+(chat?.id||'')+'/'+(material?.id||'');
  if(scope!==materialScope){materialScope=scope;el('allow-materials').checked=false;}
  el('material-open').disabled=busy||modeLoading||accountLoading||accountExpired;
  el('material-clear').hidden=!material;el('material-clear').disabled=busy||modeLoading;
  el('material-selected').hidden=!material;el('material-consent').hidden=!material;
  el('material-selected').textContent=material?'已选：'+material.name+' · '+material.lines+'行 · '+material.bytes+'字节；本次只分析所选片段，设备当前状态未核验。':'';
  updateInputMeter();
}
function resetMaterialsUI(){
  materialViewRevision++;materialScope='';el('allow-materials').checked=false;el('material-file').value='';el('material-paste').value='';
  el('material-original-body').textContent='';el('material-original-note').textContent='';el('material-existing').replaceChildren();
}
async function openMaterialEditor(fromLong=false){
  if(busy||modeLoading||accountLoading||accountExpired)return;
  if(!current())newChat();const chat=current(),epoch=accountEpoch;
  el('material-editor').dataset.long=fromLong?'yes':'';el('material-question-row').hidden=!fromLong;
  el('material-file').value='';el('material-name').value='粘贴文本.txt';
  el('material-paste').value=fromLong?el('question').value:'';el('material-note').textContent='';
  el('material-existing').replaceChildren();el('material-editor').showModal();
  if(fromLong)return; // 转长文本时必须先保存该原文，不能被旧材料选择悄悄替换。
  try{
    const data=await api('/api/conversations/'+encodeURIComponent(chat.id)+'/materials');
    if(epoch!==accountEpoch||!el('material-editor').open)return;
    if(data.materials.length)add(el('material-existing'),'h3','本对话24小时内的材料');
    for(const item of data.materials){
      const row=add(el('material-existing'),'div','','experience-card');add(row,'strong',item.name);
      add(row,'small',item.lines+'行 · '+item.bytes+'字节 · 用户上传，非现场证据');
      const choose=add(row,'button','选择这份材料','secondary-button');choose.type='button';
      choose.onclick=()=>{chat.material=item;el('material-editor').close();save();render();};
      const view=add(row,'button','查看原文','secondary-button');view.type='button';view.onclick=()=>showMaterial(item.id);
      const remove=add(row,'button','删除服务器原文','secondary-button');remove.type='button';
      remove.onclick=async()=>{
        if(!confirm('删除这份服务器原文？已有聊天摘录、审计和备份不会随之删除。'))return;
        try{await api('/api/materials/'+item.id+'/delete',{});if(chat.material?.id===item.id)delete chat.material;
          el('material-editor').close();save();render();}
        catch(error){el('material-note').textContent=error.message;}
      };
    }
  }catch(error){el('material-note').textContent=error.message;}
}
async function saveMaterial(){
  if(materialSaving)return;
  const chat=current();if(!chat)return;
  const epoch=accountEpoch,fromLong=el('material-editor').dataset.long==='yes';
  const question=el('material-question').value.trim();
  if(fromLong&&(!question||inputLength(question)>1000)){el('material-note').textContent='请用1000字符内的短问题说明要分析什么，长原文仍保留。';return;}
  materialSaving=true;el('material-save').disabled=true;
  try{
    const file=el('material-file').files[0];let bytes;
    if(file){if(file.size>materialMaxBytes)throw Error('文件超过256KB，请拆分；原选择未丢弃。');bytes=new Uint8Array(await file.arrayBuffer());}
    else bytes=new TextEncoder().encode(el('material-paste').value);
    if(!bytes.length||bytes.length>materialMaxBytes)throw Error('材料须为非空文本且不超过256KB；原输入仍保留。');
    const item=await api('/api/materials',{conversation_id:chat.id,name:el('material-name').value,data_base64:bytesBase64(bytes)});
    if(epoch!==accountEpoch)return;
    chat.material=item;if(fromLong)el('question').value=question;
    el('material-editor').close();el('material-file').value='';el('material-paste').value='';
    save();render();resizeQuestion();el('status').textContent='材料已保存在本项目服务器；确认片段外发范围后再发送问题。';
  }catch(error){if(epoch===accountEpoch)el('material-note').textContent=error.message;}
  finally{materialSaving=false;el('material-save').disabled=false;}
}
function originalLines(text){
  const lines=text.split(/\r\n|[\n\r\u0085\u2028\u2029]/);
  if(lines[lines.length-1]==='')lines.pop();return lines.map((line,index)=>(index+1)+' | '+line).join('\n');
}
async function showMaterial(id){
  const revision=++materialViewRevision;
  el('material-original-body').textContent='';el('material-original-note').textContent='正在核对材料归属…';
  if(!el('material-original').open)el('material-original').showModal();
  try{
    const item=await api('/api/materials/'+id);
    if(revision!==materialViewRevision||!el('material-original').open)return;
    el('material-original-note').textContent=item.name+' · '+item.lines+'行 · SHA256 '+item.sha256+'。用户上传，不证明当前设备状态。';
    el('material-original-body').textContent=originalLines(item.text);
  }catch(error){if(revision===materialViewRevision&&el('material-original').open)el('material-original-note').textContent=error.message;}
}
function renderMaterialEvidence(box,result){
  if(!result?.material_info)return;
  const selection=result.material_selection;
  if(selection)add(box,'p','本次提供'+selection.selected_chunks+' / '+selection.total_chunks+'个原文片段。'+(selection.selection_complete?'已覆盖本份文字材料；仍未核验现场。':'未提供全文，遗漏区域不能据此排除异常。'),'scope-note');
  const button=add(box,'button','查看上传材料原文','secondary-button experience-feedback');button.type='button';
  button.onclick=()=>showMaterial(result.material_info.id);
}
el('material-open').onclick=()=>openMaterialEditor(false);
el('convert-long').onclick=()=>openMaterialEditor(true);
el('material-save').onclick=saveMaterial;
el('material-clear').onclick=()=>{if(current())delete current().material;save();render();};
el('material-file').onchange=()=>{const file=el('material-file').files[0];if(file){el('material-name').value=file.name;el('material-paste').value='';}};
