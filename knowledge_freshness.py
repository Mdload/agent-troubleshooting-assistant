"""Keep archived observations available for history without feeding them as today's status."""
import copy
import datetime
import re


def historical_request(question, today=None):
    """Explicit requests for a past snapshot; rejecting old data does not request it."""
    if not isinstance(question, str):
        return False
    patterns = (r'(?P<year>20\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})',
                r'(?:(?P<year>20\d{2})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?')
    past = r'(?:历史(?:状态|情况|记录|快照)|旧(?:状态|记录|快照)|当时|那天|之前的(?:状态|情况))'
    cue = '(?:'+past+r'|(?:20\d{2}[-/年])?\d{1,2}[-/月]\d{1,2}[日号]?)'
    if re.search(r'(?:不要再?|不用|别再?|不再|不采用|不使用|不参考).{0,18}'+cue, question):
        return False
    if re.search(past, question):
        return True
    today = today or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    for pattern in patterns:
        for match in re.finditer(pattern, question):
            parts = match.groupdict()
            try:
                date = datetime.date(int(parts['year'] or today.year), int(parts['month']), int(parts['day']))
            except ValueError:
                continue
            if date < today:
                return True
    return False


def source_for_model(source, question):
    """Project only known mixed archive sections; leave originals, dates and rules intact."""
    result = copy.deepcopy(source)
    if historical_request(question):
        return result
    ident = result.get('id')
    if ident not in ('KB-002', 'KB-005', 'KB-008', 'KB-009', 'KB-010', 'KB-011', 'KB-012'):
        return result
    text = result.get('historical_finding', '')
    if not isinstance(text, str):
        return result
    original = text
    if ident == 'KB-008':
        lines = []
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if line.lstrip().startswith('|') and len(cells) == 4:
                # The fourth column is a one-time observation, never an inventory fact.
                cells = cells[:3]
                cells = [cell.replace('同上；Croc服务安装但未运行', '同上；Croc')
                         .replace('常驻拨号/管理及同套HA；Croc安装未运行', '拨号/管理及同套HA；Croc')
                         .replace('主要程序与入口', '登记的程序与用途（是否仍安装、运行需现查）') for cell in cells]
                line = '| '+' | '.join(cells)+' |'
            lines.append(line)
        text = '以下只说明登记的设备和程序用途，不提供设备当前安装或运行情况。\n'+ '\n'.join(lines)
    elif ident == 'KB-002':
        text = text.replace('观察到TCP9965与UDP9966配置/监听', '登记端口TCP9965与UDP9966（当前是否监听需检查）')
        text = text.replace('现场在6个源地址看到TCP9965 ESTAB，局端也观察到对应连接。', '')
    elif ident == 'KB-005':
        text = re.sub(r'(?m)^- 两局端的health/role/watchdog脚本哈希在本轮一致；两远端health/check_croc_links哈希一致。未比较所有文件。\n?', '', text)
        text = text.replace('局端备机仍保留部分linkdown内层路由', '待机设备也可能保留部分linkdown内层路由')
    elif ident == 'KB-009':
        text = re.sub(r'(?m)^- MySQL/nginx/node进程已看到[^\n]*\n?', '', text)
    elif ident == 'KB-010':
        text = re.sub(r'(?m)^- Router路由：[^\n]*\n?', '', text)
    elif ident == 'KB-011':
        text = re.sub(r'(?m)^- ppp5历史故障案例[^\n]*\n?', '', text)
    elif ident == 'KB-012':
        text = re.sub(r'(?m)^- 本轮发现140 nginx\.service[^\n]*\n?', '', text)
    if text != original:
        result['historical_finding'] = text
        result['runtime_snapshot_excluded'] = True
    return result
