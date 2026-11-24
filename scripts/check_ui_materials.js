/* 执行材料页面函数：长输入不丢失、迟到响应隔离、同意重置；0真实模型/设备。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/material-ui.js'),'utf8');
new vm.Script(source);
function environment(api=async()=>({materials:[]})){
  const elements=new Map();
  const sandbox={accountEpoch:1,accountLoading:false,accountExpired:false,busy:false,modeLoading:false,
    chat:{id:'one'},TextEncoder,Uint8Array,btoa:text=>Buffer.from(text,'binary').toString('base64'),
    current(){return sandbox.chat;},newChat(){sandbox.chat={id:'created'};},api,save(){},resizeQuestion(){},
    render(){sandbox.renderMaterials();},add(parent,tag,text){const child={textContent:text};parent.children.push(child);return child;},
    el:id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,hidden:false,disabled:false,textContent:'',
      open:false,dataset:{},files:[],children:[],replaceChildren(){this.children=[];},showModal(){this.open=true;},close(){this.open=false;}});
      return elements.get(id);}};
  vm.createContext(sandbox);vm.runInContext(source,sandbox);return sandbox;
}
async function main(){
  const raw='长文本😀\r\n'.repeat(600)+'END';
  const long=environment();long.el('question').value=raw;long.updateInputMeter();
  assert.equal(long.inputLength('😀'),1);assert.equal(long.el('convert-long').hidden,false);
  await long.openMaterialEditor(true);
  assert.equal(long.el('material-paste').value,raw);assert.equal(long.el('question').value,raw);
  long.el('material-editor').close();assert.equal(long.el('question').value,raw);

  let sent;const converted=environment(async(url,data)=>{sent=data;return {id:'m1',name:'p.txt',lines:600,bytes:9};});
  converted.el('question').value=raw;await converted.openMaterialEditor(true);
  converted.el('material-question').value='分析异常';await converted.saveMaterial();
  assert.equal(Buffer.from(sent.data_base64,'base64').toString('utf8'),raw);
  assert.equal(converted.el('question').value,'分析异常');assert.equal(converted.chat.material.id,'m1');
  assert.equal(converted.el('allow-materials').checked,false);

  const oversized=environment(()=>{throw Error('should not upload');});
  oversized.el('question').value=raw;await oversized.openMaterialEditor(true);
  oversized.el('material-question').value='分析';oversized.el('material-paste').value='字'.repeat(100000);
  await oversized.saveMaterial();assert.equal(oversized.el('question').value,raw);
  assert.equal(oversized.el('material-editor').open,true);assert.match(oversized.el('material-note').textContent,/原输入仍保留/);

  const refused=environment(async()=>{throw Error('服务器拒绝');});
  refused.el('question').value=raw;await refused.openMaterialEditor(true);refused.el('material-question').value='分析';
  await refused.saveMaterial();assert.equal(refused.el('question').value,raw);assert.equal(refused.el('material-paste').value,raw);

  const consent=environment();consent.chat.material={id:'m1',name:'p.txt'};consent.renderMaterials();
  consent.el('allow-materials').checked=true;consent.renderMaterials();assert.equal(consent.el('allow-materials').checked,true);
  consent.chat={id:'two',material:{id:'m1'}};consent.renderMaterials();assert.equal(consent.el('allow-materials').checked,false);
  consent.el('allow-materials').checked=true;consent.accountEpoch++;consent.renderMaterials();assert.equal(consent.el('allow-materials').checked,false);

  const resolvers=[];const late=environment(()=>new Promise(resolve=>resolvers.push(resolve)));
  const oldView=late.showMaterial('old');late.resetMaterialsUI();late.el('material-original').close();
  const newView=late.showMaterial('new');
  resolvers[0]({text:'private old identity',name:'old',sha256:'old',lines:1});await oldView;
  assert.equal(late.el('material-original-body').textContent,'');
  resolvers[1]({text:'new line',name:'new',sha256:'new',lines:1});await newView;
  assert.equal(late.el('material-original-body').textContent,'1 | new line');

  let finish;const saving=environment(()=>new Promise(resolve=>finish=resolve));
  saving.el('question').value=raw;await saving.openMaterialEditor(true);saving.el('material-question').value='old';
  const pending=saving.saveMaterial();saving.accountEpoch++;saving.chat={id:'new'};saving.el('question').value='new draft';
  finish({id:'old',name:'old.txt'});await pending;assert.equal(saving.chat.material,undefined);assert.equal(saving.el('question').value,'new draft');

  assert.equal(long.originalLines('a\r\nb\rc\u0085d\u2028e\u2029'),'1 | a\n2 | b\n3 | c\n4 | d\n5 | e');
  console.log(JSON.stringify({ui_material_checks:8,passed:8,visual_acceptance:false,model_api_calls:0,device_reads:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
