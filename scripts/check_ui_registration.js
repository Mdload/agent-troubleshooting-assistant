/* 执行真实注册页与账号设置脚本；只用临时DOM/API替身。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../deploy/public_login.html'),'utf8');
const source=html.match(/<script>([\s\S]*?)<\/script>/)[1];new vm.Script(source);
function node(){return {value:'',disabled:false,hidden:false,required:false,textContent:'',attributes:{},listeners:{},setAttribute(k,v){this.attributes[k]=v;},addEventListener(k,v){this.listeners[k]=v;},reportValidity(){return true;}};}
function page(configured=true){
 const elements=new Map(),calls=[],navigation=[];
 const box={document:{getElementById(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);}},
  location:{replace:url=>navigation.push(url)},fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/registration')?{configured,revision:1}:{authenticated:true}};}};
 vm.createContext(box);vm.runInContext(source,box);return {box,calls,navigation,get:id=>box.document.getElementById(id)};
}
async function main(){
 const registration=page();await registration.box.chooseMode('register');
 registration.get('username').value='中';registration.get('password').value='1';registration.get('lab-password').value='实验室口令';
 await registration.get('login').listeners.submit({preventDefault(){}});
 const request=registration.calls.find(x=>x.url==='/api/account/register');
 assert.deepEqual(JSON.parse(request.options.body),{username:'中',password:'1',lab_password:'实验室口令',allow_storage:true});
 assert.deepEqual(registration.navigation,['/']);assert.equal(registration.get('lab-password').value,'');
 const login=page();login.get('username').value='中';login.get('password').value='1';login.get('lab-password').value='must-not-be-sent';
 await login.get('login').listeners.submit({preventDefault(){}});
 assert.deepEqual(Object.keys(JSON.parse(login.calls[0].options.body)).sort(),['password','username']);
 const unconfigured=page(false);await unconfigured.box.chooseMode('register');assert.equal(unconfigured.get('submit').disabled,true);
 await unconfigured.box.chooseMode('login');assert.equal(unconfigured.get('submit').disabled,false);
 for(const id of ['username','password','lab-password']){
  const input=html.match(new RegExp('<input[^>]*id="'+id+'"[^>]*>'))[0];assert.doesNotMatch(input,/minlength|maxlength|pattern/);
 }
 const accountSource=fs.readFileSync(path.join(__dirname,'../web/account-ui.js'),'utf8');
 const elements=new Map(),calls=[];const sandbox={account:{id:'existing'},accountEpoch:1,el:id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);},api:async(url,data)=>{calls.push({url,data});return {configured:true,revision:data?2:1};}};
 vm.createContext(sandbox);vm.runInContext(accountSource,sandbox);await sandbox.loadLabRegistration();
 sandbox.el('lab-password').value='new-code';sandbox.el('lab-password-confirm').value='new-code';
 await sandbox.el('lab-password-form').onsubmit({preventDefault(){}});
 assert.equal(calls[1].data.expected_revision,1);assert.equal(calls[1].data.password,'new-code');assert.equal(sandbox.el('lab-password').value,'');
 assert.match(sandbox.el('lab-password-note').textContent,/已保存/);
 console.log(JSON.stringify({registration_ui_checks:4,passed:4,model_calls:0,device_calls:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
