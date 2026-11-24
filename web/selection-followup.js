// 把已完成的划词问答接到独立新对话；导入时不重复请求模型。
window.createSelectionConversation=async function(data){
  if(!(await assistantStartup)||accountLoading||accountExpired)throw Error('助手尚未就绪，请刷新后重试。');
  if(!data||!/^[-a-z0-9]{36}$/i.test(data.id)||typeof data.text!=='string'||!data.text.trim()||[...data.text].length>800||typeof data.answer!=='string'||!data.answer||data.answer.length>20000)throw Error('划词内容不完整，请重新解释后继续追问。');
  const id='selection-followup-'+data.id;
  let chat=chats.find(item=>item.id===id);
  if(!chat){
    const answerChars=Array.from(data.answer),answers=[];
    // 个人历史单条上限16000字；长解释分段保留，仍属于同一对话。
    for(let offset=0;offset<answerChars.length;offset+=16000)answers.push({role:'assistant',content:answerChars.slice(offset,offset+16000).join(''),context:true,contextRevision:0});
    chat={id,title:'划词：'+data.text.replace(/\s+/g,' ').trim().slice(0,40),updated:Date.now(),loaded:true,
      interaction:{mode:'qa',scene:null,revision:0},messages:[
        {role:'user',content:'请解释下面这段内容：\n\n'+data.text,context:true,contextRevision:0},
        ...answers
      ]};
    chats.push(chat);
  }
  selectChat(chat);save();render();closeSidebar();
  el('question').value='';el('status').textContent='已带入划词解释，继续提问即可。';
  // 用户主动接续本次在线解释，后续问题沿用在线回答。
  resizeQuestion();el('question').focus();
  return chat.id;
};
