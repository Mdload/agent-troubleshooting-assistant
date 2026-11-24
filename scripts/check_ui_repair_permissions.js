/* 网页权限申请、管理员批准与核查动作分开；无网络或设备。 */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/repair-ui.js'),'utf8');
function fixture(){
 const requests=[],nodes=[],elements=new Map();
 function node(tag,text,cls){const n={tag,text,cls,value:'',children:[],setAttribute(){},addEventListener(){},replaceChildren(){this.children=[];}};nodes.push(n);return n;}
 const sandbox={accountEpoch:1,account:{id:'alice'},accountExpired:false,render(){},save(){},taskOf:m=>m.task,
  el:id=>{if(!elements.has(id))elements.set(id,node('div',''));return elements.get(id);},
  add(parent,tag,text,cls){const n=node(tag,text,cls);if(parent.children)parent.children.push(n);return n;},
  api:async(url,body)=>{requests.push({url,body});return {id:'w',task:'task',state:'pending'};}};
 vm.createContext(sandbox);vm.runInContext(source,sandbox);return {sandbox,nodes,requests};
}
(async()=>{
 const pending={id:'task',status:'pending',scope:{'remote-primary':['services','interfaces'],'remote-backup':['services','interfaces']}};
 const request=fixture();request.sandbox.renderRepairPermission({},pending);
 await request.nodes.find(n=>n.text==='申请修复权限').onclick();assert.deepEqual(JSON.parse(JSON.stringify(request.requests[0])),{url:'/api/repair-windows',body:{task:'task'}});
 const insufficient=fixture();insufficient.sandbox.renderRepairPermission({}, {...pending,scope:{'remote-backup':['services']}});assert.equal(insufficient.nodes.some(n=>n.text==='申请修复权限'),false);
 const review=fixture();const plan={id:'a'.repeat(32),state:'unknown',plan:{reason:'fixed action'},result:{status:'unknown'}};
 review.sandbox.api=async(url,body)=>{review.requests.push({url,body});return {...plan,state:'reviewed',result:{status:'state_verified_execution_unconfirmed'}};};
 review.sandbox.renderRepair({}, {result:{repair_plan:plan}});
 await review.nodes.find(n=>n.text==='只读核查结果').onclick();assert.equal(review.requests.filter(r=>r.body!==undefined)[0].url,'/api/repairs/'+plan.id+'/reconcile');assert.equal(review.requests.some(r=>r.url.endsWith('/execute')),false);
 const admin=fixture();admin.sandbox.renderPermissionRow({}, {id:'w',question:'fixture',state:'pending',expires:Date.now()/1000+300},true);
 admin.sandbox.loadRepairPermissions=async()=>{};
 await admin.nodes.find(n=>n.text==='批准一次').onclick();assert.equal(admin.requests[0].url,'/api/admin/repair-windows/w/decision');assert.equal(admin.requests[0].body.duration_minutes,10);
 console.log(JSON.stringify({repair_permissions_ui_checks:4,passed:4,device_writes:0,model_calls:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
