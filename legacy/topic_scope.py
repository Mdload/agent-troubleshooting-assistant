"""追问与检索共用设备范围，原始问题仍单独保留作历史。"""
import re


def topic_groups(question):
    q = question.lower()
    groups = []
    if '局端' in q: groups.append('local')
    if '远端' in q: groups.append('remote')
    if 'router' in q or '路由器' in q: groups.append('router')
    return groups


def query_group(question):
    groups = topic_groups(question)
    return groups[0] if len(groups) == 1 else 'unknown'


def retarget_topic(text, group):
    # 单一设备话题转向另一端；多端关系问题保留原范围，不改地址或批准。
    previous = query_group(text)
    if group == 'unknown' or previous in ('unknown', group): return text
    label = {'local': '局端', 'remote': '远端', 'router': 'Router'}[group]
    return re.sub({'local': '局端', 'remote': '远端', 'router': r'router|路由器'}[previous],
                  label, text, flags=re.I)


def single_role(text):
    text=text.replace('主节点','主机').replace('备用节点','备机').replace('备用机','备机')
    roles={match.group() for match in re.finditer(r'主机|备机',text)}
    return next(iter(roles)) if len(roles)==1 else None


def target_changed(anchor,target):
    before,after=query_group(anchor),query_group(target)
    if before!='unknown' and after!='unknown' and before!=after:return True
    old,new=single_role(anchor),single_role(target)
    return bool(old and new and old!=new)


def refocus_topic(text,target):
    """单一节点追问同时更新端与角色；比较问题保留原关系。"""
    if query_group(text)=='unknown':return text
    focused=retarget_topic(text,query_group(target))
    role=single_role(target)
    if role and single_role(text):
        focused=re.sub(r'(局端|远端)(的)?(?:主机|主节点|备机|备用机|备用节点)',
                       lambda m:m[1]+(m[2] or '')+role,focused)
    return focused
