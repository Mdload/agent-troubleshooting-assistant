/* Real page renderer: readable labels, scoped links, literal code and copy fidelity. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[2]||path.join(__dirname,'..');
const page=fs.readFileSync(path.join(root,'web/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(page);
class Node{
 constructor(tag='',text='',type=1){this.nodeType=type;this.tagName=tag.toUpperCase();this.value=text;this.children=[];this.parentElement=null;this.open=false;}
 get childNodes(){return this.children;}
 get textContent(){return this.nodeType===3?this.value:this.value+this.children.map(n=>n.textContent).join('');}
 set textContent(value){if(this.nodeType===3){this.value=value;return;}this.value='';this.children=[];if(value)this.append(new Node('',value,3));}
 append(...items){for(const item of items){if(item.nodeType===11){this.append(...item.children);continue;}item.parentElement=this;this.children.push(item);}}
 replaceWith(fragment){const p=this.parentElement,i=p.children.indexOf(this),nodes=fragment.nodeType===11?fragment.children:[fragment];for(const n of nodes)n.parentElement=p;p.children.splice(i,1,...nodes);}
 setAttribute(name,value){this[name]=value;}
 scrollIntoView(){this.scrolled=true;}
 focus(){this.focused=true;}
}
const document={createElement:tag=>new Node(tag),createTextNode:text=>new Node('',text,3),createDocumentFragment:()=>new Node('', '',11)};
const sandbox={document,URL,deviceName:d=>({'local-backup':'局端备机','remote-primary':'远端主机'}[d]||d)};
vm.createContext(sandbox);
vm.runInContext(page.slice(page.indexOf('function add('),page.indexOf('function tokenUsageNote(')),sandbox);
vm.runInContext(page.slice(page.indexOf('function observationLabel('),page.indexOf('// 卡片仅整理')),sandbox);
function all(n){return [n,...n.children.flatMap(all)];}
const checks=[];
function checked(name,fn){fn();checks.push(name);}
const data={evidence:[{id:'OBS-1',device:'local-backup',check:'services',ok:true,freshness:'current',collected_at:'2026-10-23T03:00:00Z'},
  {id:'OBS-2',device:'remote-primary',check:'interfaces',ok:true,freshness:'historical'}],
  refs:['OBS-1','OBS-2','KB-013'],sources:[{id:'KB-013',title:'主备切换的判断条件'}]};
const original='如果这台没有接管VIP，Croc停止可能正常（**OBS-1**、**OBS-2**）。另一台尚未检查，不能据此确认业务正常。[KB-013]';
const expected='如果这台没有接管VIP，Croc停止可能正常。另一台尚未检查，不能据此确认业务正常。';
const before=JSON.stringify(data);
checked('new_and_restored_answers_share_plain_explanation',()=>{
  for(const context of [data,null,{}, {...data,source_display:'expanded'}]){
    const box=new Node('div');sandbox.renderAnswer(box,original,context);
    assert.equal(box.textContent,expected);assert.equal(sandbox.answerCopyText(original,context),expected);
    assert.equal(all(box).filter(n=>n.tagName==='BUTTON').length,0);
  }
});
checked('inline_code_style_does_not_leak_reference_only_tokens',()=>{
  const box=new Node('div');sandbox.renderAnswer(box,'`OBS-1`没有证明业务正常。',data);
  assert.equal(box.textContent,'局端备机的程序检查没有证明业务正常。');
  assert.equal(all(box).filter(n=>n.tagName==='CODE').length,0);
});
checked('bare_identifiers_used_as_subjects_become_readable_checks',()=>{
  const text=sandbox.answerCopyText('**OBS-1**显示程序未运行；OBS-2没有确认业务。',data);
  assert.equal(text,'局端备机的程序检查显示程序未运行；远端主机的接口与业务地址检查没有确认业务。');
});
checked('missing_records_never_leave_ids_or_create_links',()=>{
  const box=new Node('div');sandbox.renderAnswer(box,'还没有确认原因。[OBS-999]',data);
  assert.equal(box.textContent,'还没有确认原因。');assert.equal(all(box).filter(n=>n.tagName==='BUTTON').length,0);
  assert.equal(sandbox.answerCopyText('OBS-999显示原因未确认。',null),'原回答提到的检查记录显示原因未确认。');
});
checked('reference_only_markdown_lists_do_not_become_an_answer',()=>{
  for(const text of ['**OBS-1**、**OBS-2**','- [OBS-1]','**[OBS-1]**','【OBS-1】'])
    assert.equal(sandbox.answerCopyText(text,data),'这条回答没有提供具体说明。');
});
checked('all_internal_reference_families_are_hidden',()=>{
  const text='说明[KB-003][HA-001]（MAT-1）【WEB-2】 **[EXP-'+'a'.repeat(32)+']** [DOC-KB-001]';
  assert.equal(sandbox.answerCopyText(text,null),'说明');
});
checked('literal_code_and_commands_are_preserved_without_html_execution',()=>{
  const text='示例中的 `tag = \"OBS-1\"` 是代码。\n\n```text\nOBS-2 <img src=x onerror=alert(1)>\n\n\nEND\n```';
  const box=new Node('div');sandbox.renderAnswer(box,text,data);
  const code=all(box).filter(n=>n.tagName==='CODE').map(n=>n.textContent).join('\n');
  assert.ok(code.includes('OBS-1'));assert.ok(code.includes('OBS-2 <img'));
  assert.equal(all(box).filter(n=>n.tagName==='IMG').length,0);assert.equal(sandbox.answerCopyText(text,data),text);
});
checked('real_links_and_filenames_are_not_internal_citations',()=>{
  const text='https://example.org/OBS-1/manual 和 /tmp/OBS-1.log 不是引用编号。';
  assert.equal(sandbox.answerCopyText(text,data),text);
  const link=sandbox.answerCopyText('[OBS-1](https://example.org/OBS-1/manual)',data);
  assert.ok(!link.startsWith('[OBS-'));assert.ok(link.endsWith('(https://example.org/OBS-1/manual)'));

});
checked('markdown_links_render_as_https_anchors_without_changing_copy',()=>{
  const text='参考[Keepalived 文档](https://example.org/guide?q=1#setup)。';
  const box=new Node('div');sandbox.renderAnswer(box,text,data);
  const links=all(box).filter(n=>n.tagName==='A');
  assert.equal(links.length,1);
  assert.equal(links[0].textContent,'Keepalived 文档');
  assert.equal(links[0].href,'https://example.org/guide?q=1#setup');
  assert.equal(links[0].target,'_blank');
  assert.equal(links[0].rel,'noopener noreferrer');
  assert.equal(sandbox.answerCopyText(text,data),text);
  const citation=new Node('div');
  sandbox.renderAnswer(citation,'[OBS-1](https://example.org/OBS-1/manual)',data);
  const anchor=all(citation).find(n=>n.tagName==='A');
  assert.equal(anchor.href,'https://example.org/OBS-1/manual');
  assert.ok(!anchor.textContent.includes('OBS-1'));
  const standard=new Node('div');
  const standardText='[C++ 文档](HTTPS://example.org/wiki/Foo_(bar))';
  sandbox.renderAnswer(standard,standardText,data);
  assert.equal(sandbox.answerCopyText(standardText,data),standardText);
  assert.equal(all(standard).find(n=>n.tagName==='A').href,'https://example.org/wiki/Foo_(bar)');
});
checked('unsafe_markdown_destinations_do_not_create_anchors',()=>{
  for(const destination of ['javascript:alert(1)','data:text/html,unsafe','http://example.org/',
                            'https://user:pass@example.org/','https://user@example.org/']){
    const box=new Node('div');
    sandbox.renderAnswer(box,'[示例]('+destination+')',data);
    assert.equal(all(box).filter(n=>n.tagName==='A').length,0,destination);
  }
});
checked('raw_records_and_detail_navigation_keep_their_original_binding',()=>{
  assert.equal(JSON.stringify(data),before);
  const run=new Node('details'),target=new Node('details'),foreign=new Node('details');run.append(target);
  const box=new Node('div'),button=sandbox.citationButton(box,data.evidence[0],new Map([['OBS-1',target]]));
  assert.ok(!button.textContent.includes('OBS-'));button.onclick();
  assert.ok(target.open&&run.open&&target.scrolled&&target.focused);assert.equal(foreign.open,false);
  const missing=sandbox.citationButton(box,data.evidence[1],new Map([['OBS-1',target]]));assert.equal(missing.onclick(),false);
});
checked('mixed_language_typography_keeps_ordinary_program_names',()=>{
  const mixed='检查Croc_sar与Keepalived2.2，等待30秒。',box=new Node('div');sandbox.renderAnswer(box,mixed,null);
  assert.equal(box.textContent,mixed);
  const latin=all(box).filter(n=>/\banswer-latin\b/.test(n.className||''));
  assert.deepEqual(latin.map(n=>n.textContent),['Croc_sar','Keepalived2.2','30']);
  sandbox.formatAnswerTypography(box);assert.equal(all(box).filter(n=>/\banswer-latin\b/.test(n.className||'')).length,3);
});
console.log(JSON.stringify({ui_citation_checks:checks.length,passed:checks.length,checks,model_api_calls:0,live_device_reads:0,visual_acceptance:false}));
