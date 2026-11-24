/* 分段UTF8、首字早于完成、失败截断和取消；不连接真实API。 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/selection-stream.js'),'utf8');
function setup(chunks){let index=0,cancelled=false,options;const context={window:{},TextDecoder,Uint8Array,csrf:'test',fetch:async(url,args)=>{options=args;assert.equal(url,'/api/selection/explain');return {ok:true,body:{getReader:()=>({async read(){return index<chunks.length?{done:false,value:chunks[index++]}:{done:true};},async cancel(){cancelled=true;}})}}}};
 vm.createContext(context);vm.runInContext(source,context);return {run:context.window.readSelectionExplanation,cancelled:()=>cancelled,options:()=>options};}
const event=(kind,value)=>'event: '+kind+'\ndata: '+JSON.stringify(value)+'\n\n';
(async()=>{
 const bytes=new TextEncoder().encode(event('delta',{text:'中文'})+event('delta',{text:'解释'})+event('done',{answer:'中文解释'}));
 const chunks=Array.from(bytes,b=>new Uint8Array([b])),c=setup(chunks),seen=[];
 const answer=await c.run('公开示例',new AbortController().signal,text=>seen.push(text));
 assert.equal(answer,'中文解释');assert.equal(seen[0],'中文');assert.equal(c.cancelled(),true);assert.equal(JSON.parse(c.options().body).text,'公开示例');
 const incomplete=setup([new TextEncoder().encode(event('delta',{text:'半句'}))]);await assert.rejects(()=>incomplete.run('示例',new AbortController().signal,()=>{}),/中断/);
 const error=setup([new TextEncoder().encode(event('error',{error:'模型繁忙'}))]);await assert.rejects(()=>error.run('示例',new AbortController().signal,()=>{}),/模型繁忙/);
 const aborted=new AbortController();aborted.abort();const stale=setup(chunks);await assert.rejects(()=>stale.run('示例',aborted.signal,()=>{throw Error('late paint');}),/取消/);
 console.log(JSON.stringify({stream_checks:4,passed:4,model_api_calls:0,device_reads:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
