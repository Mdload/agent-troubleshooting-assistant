"""回答长度与已知来源缺口的有限检查；不冒充通用语义评分。"""
import re
from claim_text import qualified
from learning_intent import source_question, documented_path_question, croc_concept_question


def prose(text):
    # 只排除完整代码块；正文中的程序名与成立条件仍计入长度。
    text = re.sub(r'```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~', '', text, flags=re.S)
    text = re.sub(r'\[([^\]\n]+)\]\(https://[^\s)]+\)', r'\1', text)
    return text.replace('**', '').replace('`', '')


def response_policy(question):
    detail_matches = re.finditer(r'详细|展开|逐行|完整(?:脚本|代码|流程|步骤)|(?:给|写|提供).{0,8}(?:代码|脚本)', question)
    detailed = any(not re.search(r'(?:不要|不用|无需|不必|别|不需要)(?:再)?$', question[:m.start()]) for m in detail_matches)
    explicit = (re.search(r'(?:不超过|最多|控制在|限|用)\s*([1-9][0-9]{0,3})\s*字', question)
                or re.search(r'([1-9][0-9]{0,3})\s*字(?:以内|以下)', question))
    maximum = min(3000, int(explicit.group(1))) if explicit else None if detailed else 400
    beginner=bool(re.search(r'新同学|刚来|入门|没听懂|听不懂|没理解|不理解|看不懂|分不清|理解为|这样理解|打个?比方|寄快递|通俗|简单的话',question)
                  or documented_path_question(question) or croc_concept_question(question))
    plain=not detailed and not re.search(r'配置|参数|命令|脚本|实测|具体数值|源码|步骤',question)
    # 已复现的具体条件缺陷，沿用一次纠正；不是通用语义评分器。
    icmp_business=bool(re.search(r'ping|ICMP',question,re.I) and re.search(r'TCP|业务|应用',question,re.I))
    return {'question_topic':question[:1000],
            'avoid_internal_meta':plain and not bool(re.search(r'知识库|资料库|资料来源|引用来源|检索|原文|文档|角色回调|notify|你(?:们)?的实现',question)),
            'semantic_checks':['icmp_business_independence'] if icmp_business else [],
            'condition_hint':'ICMP应答成功既非TCP/应用业务正常的必要条件，也非充分条件；两类流量可被分别过滤。' if icmp_business else '',
            'style': 'detailed' if detailed and not explicit else 'concise',
            'source_display': 'expanded' if source_question(question) or re.search(r'来源|从哪|原文', question) else 'on_request',
            'audience':'beginner' if beginner else 'default', 'prefer_plain_explanation':plain,
            'target_prose_chars': '150-250' if maximum == 400 else maximum,
            'max_prose_chars': maximum,
            'counting': '正文含程序名和标点；排除空白、Markdown标记、链接URL、来源编号与完整代码块',
            'citations': '关键事实就近标注本轮来源；refs覆盖正文实际使用的来源。编号有效不等于语义成立。',
            'scope': '先答所问，再给必要解释；保留改变结论的条件，不以截断正文缩短回答。'}


def remote_rules(sources, group):
    records = [s for s in sources if s.get('kind') == 'topology'
               and s.get('device_groups') == ['remote']
               and s.get('operating_modes') == ['normal_ha']]
    result = []
    for source in records:
        text = source.get('historical_finding', '')
        if 'candidate检查管理/业务前提' in text and '至少一条合格接受' in text:
            result.append({'id': 'remote_candidate_source', 'refs': [source['id']],
                'statement': '说明远端candidate/候选检查条件时就近引用本章；至少一条候选链路合格不是端到端业务验收。不得从概述补出完整调用时序。'})
        if 'remote-ha-routes.service' in text and '管理拨号与SSH保持' in text:
            result.append({'id': 'remote_program_roles', 'refs': [source['id']],
                'statement': 'remote-ha-routes管理业务路由，Croc承担传输；拨号、路由、信号采集、管理隧道等常驻程序不能统称拨号程序。部分主备资料相同不能证明全部启动方式、配置与时序完全相同。'})
    # 泛指局端candidate时不借用远端的机制。
    return result if group == 'remote' else []


def reference_link_rules(sources, policy):
    if policy.get('source_display')!='expanded': return []
    urls={s['id']:s['reference_urls'] for s in sources if s.get('reference_urls')}
    return [{'id':'requested_reference_link','reference_urls':urls,
        'statement':'用户要可打开的出处。至少给一个本轮reference_urls中的Markdown链接，refs列出对应来源；说明它支持哪一点及适用范围。不要只报书名、文档名或编号，不补造链接。'}] if urls else []



def plain_language_answer(answer):
    """Translate a few internal framing terms; preserve code, citations and every factual qualifier."""
    rules=[
        (r'按资料里的正常主备模式','按平时一台工作、一台待机的安排'),
        (r'(?:注[：:]\s*)?这是文档记录的正常热备分工[，,]当前实际运行状态本轮未核实[。]?','这是平时的分工，现在哪台正在工作还没有检查。'),
        (r'这轮资料里没有给出具体服务名称[，,]也没查到它的配置[，,]所以端口暂时不能确认[，,]我不猜[。]?','还不知道具体是哪个服务，也还没查看它的配置，所以暂时不能确定端口。'),
        (r'按照正常双机热备的规则','如果仍按平时一主一备的安排运行'),
        (r'符合待机时的预期','看起来像是在待机'),
        (r'本次可查范围内','这次获准检查的范围内'),
        (r'处于停止（inactive）','没有运行'),
        (r'还没验证端到端业务和当前运行模式','还没试过实际业务，也没核对现在的主备设置'),
        (r'(?:以上|上述)(?:是|为)(?:文档|资料)(?:记录|描述|记载)的(?:分工方式|机制|分工|规则)','这是通常的工作方式'),
        (r'按已有正常HA资料比较','如果仍按平时一台工作、一台待机的设置'),
        (r'角色回调(?:的预期行为)?','接管或退出业务时自动执行的动作'),
        (r'正常HA假设','平时一台工作、一台待机的前提'),
        (r'符合(?:正常HA(?:中|的))?(?:非VIP持有节点的)?局部预期','这一项表现可能正常'),
        (r'当前配置、运行模式和角色转换阶段尚未核实','还没查看现在的设置，以及两台设备是否正在切换'),
    ]
    parts=re.split(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)',answer)
    for index,text in enumerate(parts):
        if text.startswith(('`','~~~')):continue
        for pattern,replacement in rules:text=re.sub(pattern,replacement,text)
        parts[index]=text
    return ''.join(parts)

def answer_issues(answer, refs, policy, rules):
    text = prose(answer)
    counted = re.sub(r'\[(?:(?:KB|HA|OBS|MAT|WEB)-\d+|EXP-[a-f0-9]{32})\]|\s|(?m:^[#>*+-]+\s*)', '', text)
    issues = []
    maximum = policy['max_prose_chars']
    if maximum is not None and len(counted) > maximum:
        issues.append({'id': 'answer_too_long', 'statement':
            '正文为%d字符，上限%d；英文字母、数字、标点也逐字符计算。请重写到%d字符以内，'
            '只留所问的核心结论、成立条件和必要来源，省略未问的配置细节；勿直接截断或删除否定词。'
            % (len(counted), maximum, min(250, max(1, maximum*3//5)))})
    # 行内代码中的程序标识仍可作主语，代码里的整句中文示例不作断言。
    claims = re.sub(r'```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~', '', answer, flags=re.S)
    claims = re.sub(r'`([^`\n]*)`', lambda m: m.group(1) if re.fullmatch(r'[/A-Za-z0-9_.:-]+', m.group(1)) else '', claims).replace('**', '')
    if 'icmp_business_independence' in policy.get('semantic_checks',[]):
        for match in re.finditer(r'必要(?:非充分|但不充分|不充分)?条件',claims):
            start=max(claims.rfind(mark,0,match.start()) for mark in ('。','；',';','\n'))+1
            prefix=claims[start:match.start()]
            if (re.search(r'ping\s*通|ICMP[^。；\n]{0,12}(?:通|可达|成功|应答)',prefix,re.I)
                    and re.search(r'TCP|业务|应用',prefix,re.I) and not qualified(claims,match)):
                issues.append({'id':'icmp_business_independence','statement':policy['condition_hint']+
                    '本答把ICMP连通说成必要条件，请更正该断言，保留具体协议、端口与应用响应的区别。'})
                break
    if re.search(r'元组|tuple',policy.get('question_topic',''),re.I):
        positive=re.search(r'元组[^。；\n]{0,25}不可变[^。；\n]{0,20}(?:可以|能|可)[^。；\n]{0,10}字典[^。；\n]{0,3}键',claims)
        if positive and not re.search(r'可哈希|hashable',claims,re.I):
            issues.append({'id':'tuple_key_condition','statement':
                '不能从元组不可变直接推出它能作字典键：所有成员也必须可哈希。'
                '请保留这个条件，或删掉用户没问的字典键话题，只解释可否修改元素绑定。'})
    for rule in rules:
        if rule['id']=='requested_reference_link':
            links=set(re.findall(r'\[[^\]\n]+\]\((https://[^\s)]+)\)',answer))
            if not any(ident in refs and links.intersection(urls)
                       for ident,urls in rule['reference_urls'].items()):
                issues.append(dict(rule))
        elif rule['id'] == 'remote_candidate_source':
            # 只检查已经登记的具体来源缺口；一般说明、引用错误说法和代码例子不作机制断言。
            for paragraph in re.split(r'\n\s*\n', claims):
                if not re.search(r'candidate|候选检查|check_croc_links', paragraph, re.I):
                    continue
                if not re.search(r'检查|探测|合格|源IPv4|至少一条|路由|接管', paragraph):
                    continue
                if not any(r in refs and '['+r+']' in paragraph for r in rule['refs']):
                    issues.append(dict(rule))
                    break
        elif rule['id'] == 'remote_program_roles':
            patterns = [
                r'(?:remote-ha-routes(?:\.service)?|Croc(?:_smr)?)(?:\s*是|\s*就是|\s*负责|\s*用于|\s*承担)[^。；\n]{0,12}拨号',
                r'拨号(?:程序|进程|服务)[：:][^。；\n]{0,140}(?:manage_MAR|pingFor|dynamicroute|signal|envset)',
                r'(?:两台|主备|主机和备机|备机和主机)[^。；\n]{0,12}(?:机制|配置|启动方式|流程)[^。；\n]{0,5}(?:完全相同|完全一致|一模一样)',
            ]
            if any(not qualified(claims, match) for pattern in patterns
                   for match in re.finditer(pattern, claims, re.I)):
                issues.append(dict(rule))
    return issues
