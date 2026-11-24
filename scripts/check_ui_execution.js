/* Actual UI functions: execution records, safe text and stable expansion on polling. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
function all(n){return [n,...n.children.flatMap(all)];}
function node(tag,text=''){return {tagName:tag.toUpperCase(),textContent:text,children:[],dataset:{},open:false,
  replaceChildren(){this.children=[];},querySelectorAll(){return all(this).filter(n=>n.tagName==='DETAILS'&&n.dataset.executionKey);}};}
const sandbox={deviceName:d=>d,checkLabels:{services:'服务状态',interfaces:'接口/VIP'},add(parent,tag,text='',cls=''){
  const child=node(tag,text);child.className=cls;parent.children.push(child);return child;}};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('// 可读执行过程：'),source.indexOf('// 参考资料面向阅读')),sandbox);
const root=node('div'),step={id:'OBS-1',device:'local-backup',check:'services',status:'succeeded',freshness:'current',
  summary:'Croc停止，不能说明根因。',command:'systemctl show croc.service',output:'<img src=x onerror=alert(1)>',purpose:'查看服务',duration_ms:12,collected_at:'2026-10-23T01:00:00Z'};
const data={id:'t',status:'running',execution_steps:[step],active_check:{device:'local-backup',check:'interfaces',command:'ip -br addr',purpose:'核对VIP'}};
sandbox.renderExecutionFlow(root,data);const panel=root.children[0];assert.ok(panel.open);assert.equal(panel.dataset.executionTask,'t');
assert.ok(all(panel).some(n=>n.tagName==='PRE'&&n.textContent===step.output));assert.equal(all(panel).filter(n=>n.tagName==='IMG').length,0);
assert.ok(all(panel).some(n=>n.textContent.includes('等待这一步的结果')));
const details=all(panel).find(n=>n.dataset.executionKey?.startsWith('OBS-1'));details.open=true;
sandbox.refreshExecutionFlow(panel,{...data,execution_steps:[step,{...step,id:'OBS-2',check:'interfaces'}],active_check:null});
assert.ok(all(panel).find(n=>n.dataset.executionKey?.startsWith('OBS-1')).open,'轮询保留已展开的命令/结果');
const before=panel.children;sandbox.refreshExecutionFlow(panel,{...data,execution_steps:[step,{...step,id:'OBS-2',check:'interfaces'}],active_check:null});
assert.equal(panel.children,before,'数据未变不重建流程');
const answer='FINAL_ANSWER_SENTINEL',thinking='HIDDEN_REASONING_SENTINEL';
const qa=node('div');sandbox.renderExecutionFlow(qa,{status:'answered',answer,reasoning:thinking,events:[{action:'search',ids:['KB-1'],count:1}]});
assert.ok(all(qa).some(n=>n.textContent.includes('这次没有操作设备')));assert.ok(all(qa).some(n=>n.textContent==='先了解这个问题'));
assert.ok(!all(qa).some(n=>n.textContent.includes(answer)||n.textContent.includes(thinking)));
const old=node('div');sandbox.renderExecutionFlow(old,{status:'completed',execution_steps:[{...step,freshness:'historical'}]});
assert.ok(all(old).some(n=>n.textContent.includes('以前的检查')));
const failed=node('div');sandbox.renderExecutionFlow(failed,{status:'incomplete',execution_steps:[{...step,status:'failed',summary:'检查超时'}]});
assert.ok(all(failed).some(n=>n.textContent.includes('这一步没查完')));assert.ok(all(failed).some(n=>n.textContent==='这次检查已停止'));
const humanDetails=all(panel).find(n=>n.dataset.executionKey?.startsWith('OBS-1')&&!n.dataset.executionKey.endsWith('-raw'));
assert.ok(humanDetails.children.some(n=>n.textContent==='这一步做了什么？'));
const raw=all(humanDetails).find(n=>n.dataset.executionKey?.endsWith('-raw'));
assert.equal(raw.open,false,'命令默认收在第二层，打开步骤先看到中文说明');
assert.ok(all(raw).some(n=>n.tagName==='PRE'&&n.textContent===step.command));
assert.ok(all(panel).some(n=>n.textContent.includes('有没有运行')));
assert.ok(panel.children.some(n=>all(n).some(c=>c.tagName==='LI'&&c.children.some(t=>t.tagName==='SMALL'&&t.textContent.includes('检查时间：')&&t.textContent.includes('只代表检查时的情况')))),'采集时间在步骤正文直接可见，不需要展开原始命令');
console.log(JSON.stringify({ui_execution_checks:11,passed:11,model_api_calls:0,device_reads:0,visual_acceptance:false}));
