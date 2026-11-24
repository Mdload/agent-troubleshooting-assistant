// 流式解释：正文到达即显示，完成标记后才允许接续对话。
window.readSelectionExplanation=async function(text,signal,onPartial){
  const response=await fetch('/api/selection/explain',{method:'POST',headers:{'Content-Type':'application/json','X-ATA-Token':csrf},
    signal,body:JSON.stringify({text,allow_context:true})});
  if(!response.ok){const error=await response.json();throw Error(error.error||'解释请求未完成，请稍后重试。');}
  if(!response.body)throw Error('未取得解释，请重试。');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',answer='',done=null,lastPaint=0;
  try{
    while(true){
      const next=await reader.read();if(signal.aborted)throw Error('解释已取消');
      buffer+=decoder.decode(next.value||new Uint8Array(),{stream:!next.done}).replace(/\r\n/g,'\n');
      if(buffer.length>100000)throw Error('解释响应格式异常');
      let boundary;
      while((boundary=buffer.indexOf('\n\n'))>=0){
        const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
        const kind=/^event: (.+)$/m.exec(block)?.[1],raw=/^data: (.+)$/m.exec(block)?.[1];if(!kind||!raw)continue;
        const value=JSON.parse(raw);
        if(kind==='error')throw Error(value.error||'解释未完成，请重试。');
        if(kind==='delta'&&typeof value.text==='string'){
          answer+=value.text;if(answer.length>20000)throw Error('解释过长，请重试。');
          if(!lastPaint||Date.now()-lastPaint>=60){onPartial(answer);lastPaint=Date.now();}
        }
        if(kind==='done')done=value;
      }
      if(next.done)break;
    }
    if(!done||typeof done.answer!=='string'||!done.answer||done.answer!==answer)throw Error('解释中断，请点击重试。');
    return done.answer;
  }finally{await reader.cancel().catch(()=>{});}
};
