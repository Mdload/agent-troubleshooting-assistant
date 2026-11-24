/* 离线检查实际页面脚本：进度轮询不重绘证据，结束/失败停止，首用提示可清除。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const page = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const source = page.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
const polling = source.slice(source.indexOf('function progressText('), source.indexOf('function taskCard('));
function fixture(responses) {
  let timerId = 0;
  const timers = new Map(), calls = [], finishes = [], node = {dataset: {taskProgress: 'task'}, textContent: ''};
  const status = {textContent: ''}, message = {};
  const sandbox = {taskPollers: new Map(), deviceName: () => '局端备机', checkLabels: {services: '服务状态'},
    current: () => ({messages: [message]}), document: {querySelectorAll: () => [node]}, el: () => status, save: () => {},taskOf:m=>m.workflowTask,
    api: async (url, body) => {calls.push({url, body});const next = responses.shift();if(next instanceof Error)throw next;return next;},
    setTimeout: fn => {timers.set(++timerId, fn);return timerId;}, clearTimeout: id => timers.delete(id),
    updateTask: (message, task) => {finishes.push(task);sandbox.stopWatchingTask(task.id);}};
  vm.createContext(sandbox);vm.runInContext(polling, sandbox);
  return {sandbox, calls, finishes, node, status, timers, message,
    tick: async () => {const [id, fn] = timers.entries().next().value;timers.delete(id);await fn();}};
}
async function main() {
  const running = {id: 'task', status: 'running', progress: {stage: 'reading', device: 'local-backup', check: 'services', elapsed_seconds: 2, reads: 1}};
  const first = fixture([running, {id: 'task', status: 'cancelled'}]);
  first.sandbox.watchTask({}, 'task');first.sandbox.watchTask({}, 'task');
  assert.equal(first.timers.size, 1);
  await first.tick();assert.match(first.node.textContent, /服务状态.*2.0 秒/);assert.equal(first.finishes.length, 0);
  await first.tick();assert.equal(first.finishes[0].status, 'cancelled');assert.equal(first.timers.size, 0);
  const staged={...running,execution_limits:{version:'bounded_stages_v1',max_reads:8,max_stages:2,reads_per_stage:4},
    investigation_stages:[{number:1,reads:4,status:'completed'},{number:2,reads:1,status:'running'}]};
  assert.match(first.sandbox.progressText(staged),/第2轮检查（共2轮）/);
  const scopeText=[],scopeBox={};
  const scopeSandbox={deviceName:id=>id,checkLabels:{},add:(parent,tag,text)=>{scopeText.push(text||'');return {};}};
  vm.createContext(scopeSandbox);
  vm.runInContext(source.slice(source.indexOf('function taskScope('),source.indexOf('function updateTask(')),scopeSandbox);
  scopeSandbox.taskScope(scopeBox,staged);
  assert.ok(scopeText.some(text=>/总计最多 8 项/.test(text)));
  scopeText.length=0;scopeSandbox.taskScope(scopeBox,{});
  assert.ok(scopeText.some(text=>/最多读取\s*4\s*项/.test(text)));
  scopeText.length=0;scopeSandbox.taskScope(scopeBox,{approval_flow:'single_scope_v1',automatic_read:true});
  assert.ok(scopeText.some(text=>/检查自动开始/.test(text)));
  assert.ok(scopeText.some(text=>/结果会显示在这里/.test(text)));
  assert.ok(scopeText.every(text=>!text.includes('Inferera')));
  assert.ok(first.calls.every(call => call.url === '/api/tasks/task' && call.body === undefined));
  const failed = fixture([new Error('offline'), new Error('offline'), new Error('offline')]);
  failed.sandbox.watchTask(failed.message, 'task');await failed.tick();await failed.tick();await failed.tick();
  assert.equal(failed.timers.size, 0);assert.equal(failed.finishes.length, 0);assert.match(failed.status.textContent, /进度更新暂时中断.*刷新申请状态/);
  const inaccessible=fixture([Object.assign(new Error('not_found'),{status:404})]);
  inaccessible.sandbox.watchTask({workflowTask:{id:'task',status:'running'}},'task');await inaccessible.tick();
  assert.equal(inaccessible.finishes[0]?.status,'unavailable','会话失效的旧任务不得永久锁住输入或自动重试');
  assert.equal(inaccessible.timers.size,0);
  const notice = {textContent: '', hidden: true};
  const ready = vm.createContext({el: () => notice});
  vm.runInContext(source.slice(source.indexOf('function showReadiness('), source.indexOf("api('/api/health').then(showReadiness)")), ready);
  ready.showReadiness({model_configured: true, admin_configured: false});assert.equal(notice.hidden, true);
  ready.showReadiness({model_configured: false, admin_configured: false});assert.equal(notice.hidden, false);assert.match(notice.textContent, /模型/);
  ready.showReadiness({model_configured: true, admin_configured: true});assert.equal(notice.hidden, true);
  console.log(JSON.stringify({ui_checks: 5, passed: 5, visual_acceptance: false, model_api_calls: 0, device_reads: 0}));
}
main().catch(error => {console.error(error);process.exitCode = 1;});
