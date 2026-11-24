/* Actual page function: evidence links stay within their answer and render text safely. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
function node(tag,text='',parent=null){return {tagName:tag.toUpperCase(),textContent:text,parentElement:parent,children:[],open:false,
  setAttribute(name,value){this[name]=value;},scrollIntoView(){this.scrolled=true;},focus(){this.focused=true;}};}
const sandbox={deviceName:d=>d,checkLabels:{logs:'日志'},add(parent,tag,text='',cls=''){
  const child=node(tag,text,parent);child.className=cls;parent.children.push(child);return child;}};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('function observationLabel('),source.indexOf('// 参考资料面向阅读')),sandbox);
function all(n){return [n,...n.children.flatMap(all)];}
const box=node('div'),run=node('details','',box),target=node('details','',run),foreign=node('details');
const report={schema_version:1,note:'仅整理采集字段',facts:[{device:'local-backup',text:'<img src=x onerror=alert(1)>',refs:['OBS-1','OBS-999'],collected_at:'2026-10-18T13:00:00Z'}],
  assessments:[],unknowns:['业务未核实'],investigation:{next_checks:[],run_ended:true,stop_explanation:'本轮已停止',continuation_note:'须新批准'}};
sandbox.renderDiagnosticReport(box,report,[{id:'OBS-1'}],new Map([['OBS-1',target],['OBS-999',foreign]]));
const buttons=all(box).filter(n=>n.tagName==='BUTTON');
assert.equal(buttons.length,1);buttons[0].onclick();assert.ok(target.open&&run.open&&target.scrolled&&target.focused);
assert.equal(foreign.open,false);assert.ok(all(box).some(n=>n.textContent===report.facts[0].text));
assert.equal(all(box).filter(n=>n.tagName==='IMG').length,0);
const other=node('div');sandbox.renderDiagnosticReport(other,{schema_version:2},[],new Map());assert.equal(other.children.length,0);
const empty=node('div');sandbox.renderDiagnosticReport(empty,{schema_version:1,facts:[],unknowns:[]},[],new Map());
assert.ok(all(empty).some(n=>n.textContent.includes('尚无可解析')));
console.log(JSON.stringify({ui_evidence_checks:4,passed:4,model_api_calls:0,live_device_reads:0,visual_acceptance:false}));
