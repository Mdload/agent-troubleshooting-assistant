/* 真实页面函数：知识引用按需显示，代码/现场OBS不被删，来源仅列实际使用项。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const page=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const source=page.match(/<script>([\s\S]*?)<\/script>/)[1];new vm.Script(source);
function node(tag,text=''){return {tagName:tag.toUpperCase(),nodeType:1,textContent:text,childNodes:[],open:false};}
function add(parent,tag,text='',className=''){const item=node(tag,text);item.className=className;parent.childNodes.push(item);return item;}
const env={Map,Set,Array,Number,URL,add,libraryLabels:{croc_local:'Croc业务局端'},
  markdown(parent,text){parent.childNodes.push({tagName:'P',nodeType:1,childNodes:[{nodeType:3,textContent:text}]},
    {tagName:'CODE',nodeType:1,childNodes:[{nodeType:3,textContent:'[KB-013]'}]});},
  observationMap:()=>new Map(),formatAnswerTypography(){},tokenUsageNote:()=>'',renderDiagnosticReport(){},renderExecutionFlow(){}};
vm.createContext(env);
for(const name of ['readableReference','answerCopyText','renderAnswer','evidence']){
  const fn=source.match(new RegExp('^function '+name+'\\([\\s\\S]*?^\\}', 'm'))[0];
  vm.runInContext(fn,env);
}
const raw='条件成立时才降权。[KB-013] 当前观测[OBS-1]';
const hidden=node('div');env.renderAnswer(hidden,raw,{refs:['KB-013'],source_display:'on_request'});
assert.equal(hidden.childNodes[0].childNodes[0].textContent,'条件成立时才降权。 当前观测');
assert.equal(hidden.childNodes[1].childNodes[0].textContent,'[KB-013]');
const explicit=node('div');env.renderAnswer(explicit,raw,{refs:['KB-013'],source_display:'expanded'});
assert.equal(explicit.childNodes[0].childNodes[0].textContent,'条件成立时才降权。 当前观测');
const originalOnly=node('div');env.renderAnswer(originalOnly,raw,{refs:['KB-014']});
assert.equal(originalOnly.childNodes[0].childNodes[0].textContent,'条件成立时才降权。 当前观测');
function nodes(root){return [root,...root.childNodes.flatMap(n=>n.childNodes?nodes(n):[n])];}
const data={status:'answered',refs:['KB-013'],source_display:'on_request',sources:[
  {id:'KB-013',title:'局端切换',kind:'topology',module_id:'croc_local',historical_finding:'整合知识内容',provenance_notes:['来源：原档案']},
  {id:'KB-014',title:'未使用资料',historical_finding:'未引用的检索结果'}]};
const box=node('div');env.evidence(box,data);
const panel=nodes(box).find(n=>n.className==='source-panel');assert.equal(panel.open,false);
assert.equal(nodes(box).filter(n=>n.className==='source-item').length,1);
assert.ok(nodes(box).some(n=>n.textContent==='来源：原档案'));
assert.ok(!nodes(box).some(n=>n.textContent==='未使用资料'));
const expanded=node('div');env.evidence(expanded,{...data,source_display:'expanded'});
assert.equal(nodes(expanded).find(n=>n.className==='source-panel').open,true);
const webData={status:'answered',refs:['WEB-1'],source_display:'on_request',public_lookup:{status:'succeeded'},sources:[
  {id:'WEB-1',kind:'public_web',title:'官方发布',url:'https://pypi.org/project/fastapi/0.100.0/',fetched_at:'2026-10-21T00:00:00Z',published_at:null,verification_limits:'公开资料，不代表现场'},
  {id:'WEB-2',kind:'public_web',title:'未引用网页',url:'https://unused.example/'}]};
const webBox=node('div');env.evidence(webBox,webData);
assert.equal(nodes(webBox).find(n=>n.className==='source-panel').open,false);
assert.equal(nodes(webBox).filter(n=>n.className==='source-item').length,1);
assert.ok(nodes(webBox).some(n=>n.textContent.includes('发布 未标明')));
assert.equal(nodes(webBox).find(n=>n.tagName==='A').href,'https://pypi.org/project/fastapi/0.100.0/');
assert.equal(nodes(webBox).find(n=>n.tagName==='A').rel,'noopener noreferrer');
const unsafe=node('div');env.evidence(unsafe,{...webData,sources:[{...webData.sources[0],url:'javascript:alert(1)'}]});
assert.equal(nodes(unsafe).filter(n=>n.tagName==='A').length,0);
const hiddenWeb=node('div');env.renderAnswer(hiddenWeb,'元数据[WEB-1]，未知[WEB-999]。',webData);
assert.equal(hiddenWeb.childNodes[0].childNodes[0].textContent,'元数据，未知。');
console.log(JSON.stringify({ui_knowledge_checks:16,passed:16,model_calls:0,device_reads:0}));
