"""通用学习意图与真实现场请求分开；不建立设备读取权限。"""
import re


def source_question(text):
    return bool(re.search(r'出处|公开(?:来源|资料|文档|链接)|参考(?:资料|文献)|依据(?:是|什么|哪)|从哪.*(?:得到|知道|查到)|(?:能|可以).{0,5}打开.{0,5}(?:资料|文档|链接)', text))


def direct_inspection_request(text):
    """识别请求实际检查的说法；只识别意图，不授予执行权限。"""
    return bool(re.search(
        r'(?:请|帮我|帮忙|麻烦|替我).{0,5}'
        r'(?:查|检查|检测|排查|排障|诊断|读取|核对|看看|看下|看一下)|'
        r'^(?:检查|检测|排查|排障|诊断|查看|核对|看看|看下|看一下)',text.lower()))


def forbids_inspection(text):
    return bool(re.search(r'(?:不要|不需要|不用|无需|不必|别)(?:再|去|实际)?\s*(?:查|检查|读取|连接|排查|访问)|'
                          r'不查现场|只讲原理|只解释|仅解释', text))


def live_request(text):
    q = text.lower()
    return bool(direct_inspection_request(q)
                or re.search(r'(局端|远端|主机|备机|设备).{0,8}(现在|当前|刚刚|此刻)',q)
                or re.search(r'(现在|当前|刚刚|此刻).{0,8}(局端|远端|主机|备机|设备|vip|服务)',q))


def answer_discussion(text):
    """谈论上一回答的措辞不等于报告现场；显式现场请求仍优先。"""
    if live_request(text): return False
    return bool(re.search(r'你(?:刚才|之前|前面|上次|原来)?(?:说|讲|写|提到|举)|(?:上[一个次]|刚才那).{0,8}(?:回答|例子|示例|那句|计算)|'
                          r'(?:这个|那个|刚才).{0,8}(?:说法|算例|表述)|我.{0,5}(?:理解|复述)', text))


def review_previous_answer(question, history):
    """纠错时显式聚焦上一原答；原答仍是待审查数据，不是证据。"""
    if (not re.search(r'写反|算错|说错|不对|矛盾|错误|举错|笔误|你确定|不成立',question)
            or not re.search(r'刚才|你|上次|前面|上一|这句|那句|这个例子',question)):
        return None
    return next((m['content'] for m in reversed(history) if m.get('role')=='assistant'),None)


def general_agent_question(text):
    q = text.lower()
    if live_request(q):
        return False
    # 引用上一回答的路径，并不表示用户要查询那个程序。
    q = re.sub(r'(?:而不是|不是|不要|不用|别).{0,12}agent\.py(?:.{0,6}路径)?', '', q)
    if re.search(r'(?<![a-z0-9_.-])[a-z][a-z0-9_-]*\.(?:py|sh|service|conf)(?![a-z0-9_.-])',q):
        return False
    if any(word in q for word in ('局端','远端','router','路由器','croc','keepalived','ppp','tun16')):
        return False
    return bool(re.search(r'(?<![a-z0-9_])(?:agent|llm|react)(?![a-z0-9_])|大模型|模型|工具(?:调用|结果)|反馈循环',q)
                or source_question(q) and re.search(r'\bif\b|语义判断',q))


def general_ha_question(text):
    q = text.lower()
    if live_request(q) or any(t in q for t in ('局端','远端','router','路由器','croc','tun16','配置值','配置参数','优先级','接口','地址','本项目')):
        return False
    schema=bool(re.search(r'(?<![a-z0-9_])(?:state\s+(?:master|backup)|vrrp_(?:instance|script)|track_script)(?![a-z0-9_])',q))
    if schema: return True
    if not ('keepalived' in q or '主备' in q or '主机' in q and '备机' in q
            or '备机' in q and re.search(r'一般|通用|如果|假如|假设',q) and re.search(r'接管|切换|倒换',q)
            or read_method_question(q) and re.search(r'接管|切换|倒换',q)):
        return False
    return bool(re.search(r'一般原理|通用原理|示例|例子|有什么区别|什么区别|停(?:掉|止)?.{0,20}keepalived.{0,10}后|keepalived.{0,8}(?:停掉|停止).{0,6}后',q)
                or re.search(r'如果|假如|假设',q) and re.search(r'接管|切换|倒换|选举',q)
                or read_method_question(q))


def probe_explanation(text):
    if not re.search(r'(?<![a-z0-9_])(?:ping|icmp)(?![a-z0-9_])', text, re.I):
        return False
    clauses = re.split(r'[，,。；;！？\n]', text)
    explanatory = re.compile(r'^(?:请|帮我)?\s*(?:解释|讲解|说明)|(?:说明|判断|验证|证明|代表|意味着)什么|有什么(?:意义|作用)')
    if any(explanatory.search(clause.strip()) for clause in clauses):
        return not any(
            clause.strip() and not explanatory.search(clause.strip()) and
            (direct_inspection_request(clause.strip()) or
             re.search(r'(?:先|再|实际|现在|请|帮我|试着|从).{0,24}(?:查|测|ping)', clause, re.I))
            for clause in clauses)
    return False


def read_method_question(text):
    if probe_explanation(text):
        return True
    return (not live_request(text) and bool(re.search(
        r'(?:查|看|读取|核对)(?:哪(?:个|些)|什么)|(?:查看|检查|排查|读取)(?:方法|步骤)|'
        r'(?:怎么|如何).{0,4}(?:验证|检查|判断)|(?:验证|检查|排查|核对).{0,25}(?:怎么做|如何做|步骤)|'
        r'从哪.{0,10}(?:看|查|开始)|学会看|怎么看|如何看|离线学',text)))


def public_learning_topic(text):
    """仅解释性问题使用已整理的公开知识，不把专有配置或状态当教程。"""
    if live_request(text): return None
    if '\n追问：' in text:
        anchor,tail=text.split('\n追问：',1)
        if not any(t in tail.lower() for t in ('局端','远端','router','croc','tun16','ppp','本项目','实验室')):
            inherited=public_learning_topic(anchor)
            if inherited: return inherited
    if general_ha_question(text): return 'KB-129'
    q=text.lower()
    if (not any(t in q for t in ('局端','远端','router','croc','tun16','ppp','本项目','实验室'))
            and re.search(r'超时|重试|幂等|结果未知|补偿|撤销前面|工具.{0,10}(?:报错|错误|异常|失败)',q)
            and re.search(r'工具|请求|接口|重启|服务|补偿|幂等|结果未知|agent',q)):
        return 'KB-130'
    return None


def public_followup_question(question, anchor):
    topic=public_learning_topic(anchor)
    if not topic or public_learning_topic(anchor+'\n追问：'+question)!=topic: return False
    return bool(source_question(question) or re.search(r'^(那|那么|这个|这些|刚才|它|如果)',question)
                or topic=='KB-129' and re.search(r'fall|rise|weight|priority|notify|track_|抢占|优先级|写反|高于|低于|刚才.{0,15}(?:例子|那句|计算)',question,re.I)
                or topic=='KB-130' and re.search(r'超时|重试|幂等|结果未知|补偿|撤销|请求ID',question,re.I))


def documented_path_question(text):
    """识别项目语境下的功能路径问法，不推定物理接线或视频系统配置。"""
    endpoints='现场电脑' in text and '实验室电脑' in text and bool(re.search(r'发送|收到|传|到',text))
    return endpoints or (bool(re.search(r'实验室|局端|远端|本项目|这套',text))
            and bool(re.search(r'视频|数据|业务|流量|文件',text))
            and bool(re.search(r'传回|传到|送回|传输|经过|一路|走到',text))
            and bool(re.search(r'路线|路径|哪些设备|怎么走|一路上|分工|各自|谁接手|哪一步',text)))


def croc_concept_question(text):
    q=text.lower().rsplit('\n追问：',1)[-1]
    return ('croc' in q and not any(t in q for t in
            ('router','路由器','路径','配置','端口','优先级','健康','启动','停止','切换','服务名','脚本','vip','正常ha','主备','备机','主机','历史','案例'))
            and bool(re.search(r'软件|(?:一条|接的)线|做了?什么|干什么|是什么|怎么理解|到底.{0,8}做',q)))
