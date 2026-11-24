/* Execute the actual transcript exporter and shared presentation function offline. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.join(__dirname,'..');
const page=fs.readFileSync(path.join(root,'web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const account=fs.readFileSync(path.join(root,'web/account-ui.js'),'utf8');
new vm.Script(page);new vm.Script(account);
const chat={id:'export-test',title:'合成对话',messages:[
  {role:'user',content:'为什么会看到OBS-1？',context:true},
  {role:'assistant',content:'如果未接管VIP，停止可能正常（**OBS-1**、**OBS-2**）。业务还没确认。',context:true},
  {role:'assistant',content:'命令示例：`printf OBS-1`。',context:false}]};
const original=JSON.stringify(chat);let bytes,downloaded=false,revoked=false;
const sandbox={current:()=>chat,deviceName:()=> '局端备机',
  Blob:class{constructor(parts){bytes=parts.join('');}},
  URL:{createObjectURL:()=> 'blob:offline',revokeObjectURL:()=>{revoked=true;}},
  document:{createElement:()=>({click(){downloaded=true;},remove(){}}),body:{append(){}}},
  setTimeout:fn=>fn()};
vm.createContext(sandbox);
vm.runInContext(page.slice(page.indexOf('function observationLabel('),page.indexOf('// 卡片仅整理')),sandbox);
vm.runInContext(account.slice(account.indexOf('function historySnapshot('),account.indexOf('function resetIdentityView(')),sandbox);
vm.runInContext(account.slice(account.indexOf('function exportCurrentHistory('),account.indexOf("el('account-open').onclick")),sandbox);
sandbox.exportCurrentHistory();
const exported=JSON.parse(bytes);
assert.ok(downloaded&&revoked);assert.equal(exported.messages[0].content,chat.messages[0].content);
assert.equal(exported.messages[1].content,'如果未接管VIP，停止可能正常。业务还没确认。');
assert.equal(exported.messages[1].content,sandbox.answerCopyText(chat.messages[1].content,null));
assert.equal(exported.messages[2].content,chat.messages[2].content);
assert.equal(JSON.stringify(chat),original);
assert.ok(sandbox.historySnapshot(chat).messages[1].content.includes('OBS-1'));
console.log(JSON.stringify({ui_answer_export_checks:4,passed:4,model_api_calls:0,device_reads:0}));
