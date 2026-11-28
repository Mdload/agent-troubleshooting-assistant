"""本对话一次开启的滚动用户背景；来源绑定的摘录式摘要，不调用额外模型。"""
import hashlib
import json
import re
from server.chat_history import HistoryError
from devices.diagnostic_logs import SENSITIVE

TTL=7*86400
NOTE='本对话用户原话的摘录式整理，仅作背景；不是核实事实、知识证据或设备权限。片段可能省略上下文，较新目标优先，不相关原话不用于回答。'
POLICY='inferera_rolling_user_context_v1'
TOPICS={
    'python':('python','字典','列表','元组','迭代器','装饰器'),
    'database':('mysql','sql','数据库','达梦'),
    'network':('linux','tcp','udp','ssh','路由','网络','ppp','网卡'),
    'topology':('局端','远端','keepalived','vrrp','croc','主备','拓扑','watchdog'),
    'agent':('agent','rag','llm','大模型','mcp','fastapi','gradio'),
}


class RollingError(ValueError):pass


def source_hash(messages):
    # 包含上下文资格；仅改context=False也必须使在途快照失效。
    rows=[{'role':m['role'],'content':m['content'],'context':m.get('context',True)} for m in messages]
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def topics(text):
    q=text.lower()
    return [key for key,terms in TOPICS.items() if any(
        (bool(re.search(r'(?<![a-z0-9_])'+re.escape(term)+r'(?![a-z0-9_])',q))
         if term.isascii() else term in q) for term in terms)]


def followup(text):
    return bool(re.match(r'^(那|那么|它|这个|这样|继续|接着|下一步|为什么|怎么做|还有)',text.strip()))


def summarize(messages):
    """只保存可回到原文的目标/约束/问题；显式改目标会替换旧目标。"""
    entries=[];active_topics=['other']
    for index,message in enumerate(messages):
        # context是旧轮次进入近期窗口的资格，不等于用户撤回明确表达的目标。
        # 本对话明确开启后，旧格式/失败轮次也只摘录用户意图，不恢复问答事实。
        if message['role']!='user':continue
        text=message['content'];found=topics(text)
        if found:active_topics=found
        elif (re.search(r'换个话题|不说这个|先聊别的',text) or
              not followup(text) and re.search(r'是什么|哪里|哪国|几点|多少|怎样|如何|什么|介绍|解释|[？?]|吗$',text)
              and not re.search(r'目标|约束|不要|必须|改为|改成',text)):
            active_topics=['other']
        for match in re.finditer(r'[^\n。！？!?]+[。！？!?]?',text):
            raw=match.group();quote=raw.strip()
            if not quote or SENSITIVE.search(quote):continue
            kind=('goal' if re.search(r'目标|我想|我要|我希望|计划|准备|改学|改为|改成',quote) else
                  'constraint' if re.search(r'不要|不用|必须|只能|只用|限制|预算|请用|不再',quote) else
                  'question' if re.search(r'待解决|没解决|还没解决|问题是',quote) else None)
            if kind is None:continue
            keys=topics(quote) or list(active_topics)
            if kind=='constraint' and re.search(r'回答|中文|简短|输出格式',quote):keys=['global']
            reset_goal=bool(re.search(r'目标.{0,12}(改|换)|(?:改为|改成|改学)|不再学',quote))
            reset_constraint=bool(re.search(r'取消.{0,8}(限制|约束)|之前.{0,8}不再限制',quote))
            if kind=='goal':
                entries=[e for e in entries if not(e['kind']=='goal' and
                    (reset_goal or set(keys).intersection(e['topics'])))]
            if reset_constraint:entries=[e for e in entries if e['kind']!='constraint']
            start=match.start()+len(raw)-len(raw.lstrip());end=start+min(len(quote),240)
            quote=text[start:end]
            entries=[e for e in entries if e['quote']!=quote]
            entries.append({'kind':kind,'message_index':index,'start':start,'end':end,
                'quote':quote,'truncated':len(raw.strip())>240,'topics':keys,
                'original_turn_context':message.get('context',True)})
            goals=[e for e in entries if e['kind']=='goal']
            others=[e for e in entries if e['kind']!='goal'][-max(0,24-len(goals)):]
            entries=sorted(goals+others,key=lambda e:(e['message_index'],e['start']))
    return {'entries':entries,'active_topics':active_topics}


def relevant_entries(summary,question):
    keys=topics(question)
    if not keys and followup(question):keys=summary['active_topics']
    return [e for e in summary['entries'] if 'global' in e['topics'] or bool(set(keys).intersection(e['topics']))]


def select_entries(summary,messages,question):
    cutoff=max(0,len(messages)-8)
    eligible=[e for e in relevant_entries(summary,question) if e['message_index']<cutoff]
    # 最近的相关原话优先，输出按来源顺序，最多六条，不把整段历史重新外发。
    recent=sorted(eligible,key=lambda e:e['message_index'],reverse=True)
    goals=[e for e in recent if e['kind']=='goal'][:3]
    selected=goals+[e for e in recent if e not in goals][:6-len(goals)]
    return sorted(selected,key=lambda e:(e['message_index'],e['start']))


class RollingContext:
    def __init__(self,history):
        self.history=history
        with history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rolling_contexts ('
                'owner TEXT,conversation TEXT,revision INTEGER,enabled INTEGER,expires REAL,'
                'source_revision INTEGER,source_hash TEXT,summary TEXT,updated REAL,'
                'PRIMARY KEY(owner,conversation))')

    def record(self,db,owner,conversation):
        return db.execute('SELECT * FROM rolling_contexts WHERE owner=? AND conversation=?',(owner,conversation)).fetchone()

    def view(self,row):
        state='off' if row is None or not row['enabled'] else 'expired' if self.history.clock()>=row['expires'] else 'active'
        summary=json.loads(row['summary']) if row and state=='active' else {'entries':[]}
        return {'state':state,'enabled':state=='active','revision':row['revision'] if row else 0,
            'expires':row['expires'] if row else None,'entries':summary['entries'],
            'note':NOTE,'data_use':POLICY,'format':'extractive_user_quotes'}

    def refresh(self,db,owner,conversation,source):
        row=self.record(db,owner,conversation)
        if row is None or not row['enabled'] or self.history.clock()>=row['expires']:return row
        if row['source_revision']==source['revision']:return row
        messages=json.loads(source['messages'])
        summary=summarize(messages)
        # revision只表示开关/用途批准的代次；自动更新摘要不让关闭按钮反复冲突。
        db.execute('UPDATE rolling_contexts SET source_revision=?,source_hash=?,summary=?,updated=? '
                   'WHERE owner=? AND conversation=?',(source['revision'],source_hash(messages),
                   json.dumps(summary,ensure_ascii=False),self.history.clock(),owner,conversation))
        return self.record(db,owner,conversation)

    def get(self,owner,conversation):
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE');source=self.history.row(db,owner,conversation)
            return self.view(self.refresh(db,owner,conversation,source))

    def configure(self,owner,conversation,data):
        if set(data)!={'enabled','expected_revision'} or type(data['enabled']) is not bool or type(data['expected_revision']) is not int or data['expected_revision']<0:
            raise ValueError('Invalid rolling context setting')
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE');source=self.history.row(db,owner,conversation)
            old=self.record(db,owner,conversation)
            revision=old['revision'] if old else 0
            if revision!=data['expected_revision']:raise RollingError('rolling_context_changed')
            now=self.history.clock();messages=json.loads(source['messages'])
            summary=summarize(messages) if data['enabled'] else {'entries':[],'active_topics':[]}
            db.execute('INSERT OR REPLACE INTO rolling_contexts VALUES (?,?,?,?,?,?,?,?,?)',
                (owner,conversation,revision+1,int(data['enabled']),now+TTL if data['enabled'] else now,
                 source['revision'],source_hash(messages) if data['enabled'] else '',
                 json.dumps(summary,ensure_ascii=False),now))
            return self.view(self.record(db,owner,conversation))

    def for_request(self,owner,conversation,question):
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:source=self.history.row(db,owner,conversation)
            except HistoryError:return None
            row=self.refresh(db,owner,conversation,source)
            if self.view(row)['state']!='active':return None
            messages=json.loads(source['messages']);summary=json.loads(row['summary'])
            entries=select_entries(summary,messages,question)
            if not entries:return None
            # 每条仍须逐字对应当前用户原文；异常缓存不能变成新的事实。
            for entry in entries:
                m=messages[entry['message_index']]
                if m['role']!='user' or m['content'][entry['start']:entry['end']]!=entry['quote']:
                    raise RollingError('rolling_context_changed')
            return {'context':{'kind':'rolling_user_excerpts','note':NOTE,'entries':entries,
                'revision':row['revision'],'source_history_revision':source['revision'],'expires':row['expires'],
                'source_messages':len(messages),'recent_window_excluded':8,'grants_device_access':False},
                'binding':{'revision':row['revision'],'source_revision':source['revision'],
                           'source_hash':row['source_hash'],'question':question,
                           'state':relevant_entries(summary,question)}}

    def assert_active(self,owner,conversation,binding):
        with self.history.connect() as db:
            source=self.history.row(db,owner,conversation);row=self.record(db,owner,conversation)
            if self.view(row)['state']!='active' or row['revision']!=binding['revision']:
                raise RollingError('rolling_context_not_active')
            messages=json.loads(source['messages'])
            if source['revision']==binding['source_revision']:
                changed=source_hash(messages)!=binding['source_hash']
            else:
                # 同一目标下追加普通消息不撤销正在回答的有效原话；改目标或改原文则停止。
                changed=relevant_entries(summarize(messages),binding['question'])!=binding['state']
            if changed:raise RollingError('rolling_context_not_active')
