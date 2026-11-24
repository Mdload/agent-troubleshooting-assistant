"""从现有拓扑原文识别程序/服务/脚本名；名称不是状态或读取权限。"""
import re
from learning_intent import documented_path_question

_INDEX={}
_SUFFIXES=('.service','.timer','.sh','.py')

def record_entities(record):
    if record.get('kind')!='topology':return {}
    if 'knowledge_coverage' in record and not record['knowledge_coverage'].get('supports'):return {}
    text=str(record.get('title',''))+'\n'+str(record.get('historical_finding',''))
    result={}
    for raw in re.findall(r'(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_./-]+|[A-Za-z][A-Za-z0-9_.-]{2,})',text):
        name=raw.rstrip('.,').rsplit('/',1)[-1]
        if not name or not ('-' in name or '_' in name or name.lower().endswith(_SUFFIXES)):continue
        aliases={name.lower(),raw.rstrip('.,').lower()}
        for suffix in _SUFFIXES:
            if name.lower().endswith(suffix):
                stem=name[:-len(suffix)].lower()
                if stem not in ('agent','main','app','server','client'):
                    aliases.add(stem)
        for alias in aliases:
            if len(alias)>=4:result[alias]=name
    return result

def build_index(records):
    index={}
    for record in records:
        for alias,name in record_entities(record).items():
            item=index.setdefault(alias,{'name':name,'source_ids':[],'groups':[]})
            if record['id'] not in item['source_ids']:item['source_ids'].append(record['id'])
            groups=list(record.get('device_groups',[]))
            if not groups:
                for line in record.get('historical_finding','').splitlines():
                    label=re.match(r'\s*-\s*(局端|远端|Router)',line,re.I)
                    if label and name.lower() in line.lower():
                        group={'局端':'local','远端':'remote','router':'router'}[label[1].lower()]
                        if group not in groups:groups.append(group)
            for group in groups:
                if group not in item['groups']:item['groups'].append(group)
    return index

def configure(records):
    global _INDEX
    _INDEX=build_index(records)

def matches(question,index=None):
    index=_INDEX if index is None else index
    found=[];seen=set()
    for alias,item in sorted(index.items(),key=lambda pair:-len(pair[0])):
        if re.search(r'(?<![A-Za-z0-9_.-])'+re.escape(alias)+r'(?![A-Za-z0-9_.-])',question,re.I):
            key=item['name'].lower()
            if key not in seen:found.append(dict(item));seen.add(key)
    return found[:8]

def source_ids(question):
    return list(dict.fromkeys(ident for item in matches(question) for ident in item['source_ids']))

def group(question):
    groups={group for item in matches(question) for group in item['groups']}
    return next(iter(groups)) if len(groups)==1 else 'unknown'

def context_refs(item):
    mapping={'remote':'KB-004','local':'KB-005','router':'KB-003'}
    return [mapping[group] for group in item.get('groups',[]) if group in mapping]

def overview_requested(question):
    """明确的整体入门问题关联已有总览；不能借此认证接线、源码或现状。"""
    q=question.lower().rsplit('\n追问：',1)[-1]
    groups=[name for name in ('局端','远端','router','路由器') if name in q]
    roles=(len(groups)>=2 and any(t in q for t in ('分工','分别','各自','分不清','干什么','职责','怎么配合'))
           and not any(t in q for t in ('健康','计数','watchdog','优先级','降权','切换','恢复时间','脚本'))
           and not (re.search(r'心跳|vrrp',q) and re.search(r'心跳口|网口|接口|哪个口|什么口',q)))
    path_roles=(documented_path_question(q) and any(t in q for t in ('哪些设备','各自','分工','谁接手','哪一步'))
                and not any(t in q for t in ('完整接线','物理接线','vlan端口','源码','实测','当前状态','是否正常')))
    return (roles or path_roles or '拓扑' in q and not matches(question) and
        any(t in q for t in ('介绍','讲讲','讲解','说说','概览','全貌','整体结构','大概结构','大致结构')) and
        not any(t in q for t in ('完整接线','物理接线','vlan端口','源码','实测','运营商','当前状态','是否正常')))

def reference_terms(history):
    """只用最近展示回答中的已知名称帮助检索；不采纳其结论、命令或授权。"""
    answer=next((m['content'] for m in reversed(history) if m.get('role')=='assistant'),'')
    return [item['name'] for item in matches(answer)][:4]
