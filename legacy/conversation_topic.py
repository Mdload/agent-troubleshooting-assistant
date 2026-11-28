"""只接续简短设备补充；话题记忆不是现场证据，也不携带设备批准。"""
import re
from legacy.question_routing import FAULT
from legacy.topic_scope import refocus_topic, target_changed
from devices.device_registry import DEVICES


def validate_conversation_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):
        raise ValueError('Invalid conversation id')
    return value


def join_diagnostic_question(previous, question):
    """给本次补充留足空间，沿用调查目标和用户已经给出的信息。"""
    prefix = '\n补充：'
    room = 1000 - len(question) - len(prefix)
    return previous[:room] + prefix + question if room > 0 else question


def diagnostic_clarification(question):
    """模型未能生成有效调查时，追问一项会改变检查路径的信息。"""
    if re.search(r'通吗|通不通|互通|连通|链路|传输|不通|连不上|ping', question, re.I):
        if '业务' not in question and not re.search(r'管理|登录|ssh|隧道|croc', question, re.I):
            return '你想确认设备能否互相访问，还是两端的业务数据能否传过去？'
        return '你看到的是哪一端、哪种业务或连接出了问题？'
    if re.search(r'备机|主机|接管|切换|VIP', question, re.I):
        return '你想核对局端还是远端的主备状态？'
    return '你希望核对哪台设备、哪个服务或哪条路径？'


def target_reply(question):
    """只识别设备名称/角色的短回答，不把任意新问题拼到旧话题上。"""
    text=question.strip().lower().replace('备用节点','备机').replace('主节点','主机').replace('备用机','备机')
    text=re.sub(r'[\s，,。？?！!]+$','',text)
    correction=re.fullmatch(r'不是(局端|远端)?(主机|备机)[，,]?是(局端|远端)?(主机|备机)',text)
    if correction:
        return (correction.group(3) or correction.group(1) or '')+correction.group(4)
    text=re.sub(r'^(那么|那|我说的是|指的是|就是|是)','',text)
    text=re.sub(r'(呢|吧)$','',text)
    if re.fullmatch(r'(局端|远端)(的)?(主机和备机|主机与备机|主备|两台设备|两台|主机|备机)?',text):
        return text.replace('的','')
    if text in ('主机','备机','主备','主机和备机','两台','两台设备'):
        return text
    if re.fullmatch(r'(router|路由器)[12]?',text): return text
    return None


def resolve_topic(question,topic):
    """返回用户原问题和本次关注目标；目标提示只由宿主生成。"""
    reply=target_reply(question)
    current_followup=bool(re.fullmatch(r'(那|那么)?(它|这个)?(现在|当前)(的)?(状态)?(呢|怎么样|如何)?[？?。]*',question.strip()))
    if reply is None and not current_followup: return None
    relevant=bool(topic and (topic['intent']=='clarifying' or topic['domain'] in ('lab','topology','libraries') or
                  topic['domain']=='unknown' and any(t in topic['question'] for t in FAULT)))
    if not relevant:
        if reply is None: return None
        return {'question':question,'anchor':question,'target_hint':reply,'needs_question':True}
    previous_target=topic.get('target_hint') or topic['question']
    if current_followup:
        target=topic.get('target_hint') or topic['question']
    else:
        target=reply
        # 只补出唯一明确的端；多端话题中的“备机”仍然需要说明是哪一端。
        if not any(t in reply for t in ('局端','远端','router','路由器')):
            sides={side for side in ('局端','远端') if side in previous_target}
            if len(sides)==1: target=next(iter(sides))+reply
        elif reply in ('局端','远端') and previous_target in ('主机','备机','主备','两台','两台设备','主机和备机'):
            target=reply+previous_target
    anchor=topic['question']
    needs_question=topic['intent']=='unknown' and target_reply(anchor) is not None and not current_followup
    # 地址可能是管理身份、VIP或访问目标；切换节点时不猜改写关系。
    hostnames='|'.join(re.escape(item['hostname']) for item in DEVICES.values())
    bound_address=bool(re.search(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',anchor) or
        re.search(r'(?<![A-Za-z0-9_.-])(?:'+hostnames+r')(?![A-Za-z0-9_.-])',anchor,re.I))
    if bound_address and target_changed(anchor,target):
        return {'question':question,'anchor':anchor,'target_hint':target,'needs_question':True,
            'clarify_answer':'原问题含具体地址或主机名，切换设备后不能直接沿用。请明确新设备及要查的问题；当前尚未读取设备。'}
    focused = refocus_topic(anchor,target)
    return {'question':focused[:800]+'\n追问：'+question+'\n本次关注：'+target[:100],
            'anchor':anchor,'target_hint':target,'needs_question':needs_question,
            'inherited_live':topic['intent']=='live' or current_followup}
