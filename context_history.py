"""保存完整历史，单独构造有节选标注的有限模型视图。"""
MAX_MESSAGE=3000
MAX_INPUT_MESSAGE=16000
MAX_TOTAL=16000
EXCERPT_NOTE='\n[历史消息已节选，完整原文保留在对话中]'

def prepare_history(value):
    if not isinstance(value,list) or len(value)>8:raise ValueError('Invalid history')
    result=[];excerpted=0;total=0
    for message in value:
        if (not isinstance(message,dict) or set(message)!={'role','content'} or
            message['role'] not in ('user','assistant') or not isinstance(message['content'],str) or
            len(message['content'])>MAX_INPUT_MESSAGE):raise ValueError('Invalid history')
        text=message['content'];total+=len(text)
        if total>MAX_TOTAL:raise ValueError('History too large')
        if len(text)>MAX_MESSAGE:
            text=text[:MAX_MESSAGE-len(EXCERPT_NOTE)]+EXCERPT_NOTE;excerpted+=1
        result.append({'role':message['role'],'content':text})
    return result,excerpted
