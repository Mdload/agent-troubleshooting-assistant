"""有限断言守卫共用的句段处理；不替代语义评审。"""
import re


def prose(answer):
    # 示例代码里的文字不作为助手对现场的断言，保留代码之外的正文。
    return re.sub(r'```.*?```|`[^`\n]*`','',answer,flags=re.S).replace('**','')


def qualified(text,match,uncertain=False):
    """只看当前断言前的限定；后附未知项或转折前的否定不能洗掉肯定断言。"""
    start=max(text.rfind(mark,0,match.start()) for mark in ('。','；',';','\n'))+1
    prefix=re.split(r'但是|然而|不过|但',text[start:match.start()])[-1]
    # “两台可能同时持有VIP”的限定在主语之后；仍只读到断言末尾。
    scope=re.split(r'但是|然而|不过|但',text[start:match.end()])[-1] if uncertain else prefix
    negatives=('不能','无法','不足','尚未','并不','不等于','不代表','不应','不要',
               '没有证据','未能','不是','如果','假设','示例','例如','有待核实')
    if any(word in scope for word in negatives):return True
    if prefix.endswith('不') or re.search(r'(?:是否|能否|可否)[^，,。；\n]{0,12}$',prefix):return True
    if uncertain and any(word in scope for word in ('可能','疑似','推测')):return True
    return bool(re.match(r'(?:了)?(?:吗|呢|了没有|[？?])',text[match.end():]))
