"""实验室通用基础知识的目录和有限范围；不记录现场配置或运行状态。"""
import re
from learning_intent import public_learning_topic

CHECKED_DATE = '2026-10-12'
CHECKED_DATES = {'KB-129':'2026-10-21', 'KB-130':'2026-10-21'}
# ID固定到小主题，正文独立放在Markdown，后续补内容不重编号。
SECTIONS = [
    ('KB-101', 'linux.md', '文件与路径', ('文件路径', '绝对路径', '相对路径', '工作目录', '目录', 'pwd', 'ls')),
    ('KB-102', 'linux.md', '用户与权限', ('文件权限', '权限不足', 'permission denied', 'sudo', 'chmod', '用户组')),
    ('KB-103', 'linux.md', '进程与资源', ('进程', 'cpu', '内存', 'free', 'ps', 'top')),
    ('KB-104', 'linux.md', '磁盘与文件大小', ('磁盘', '磁盘满', 'no space left', 'df', 'du', '文件大小')),
    ('KB-105', 'network.md', 'IP与路由', ('子网', '网关', '默认路由', 'ip route', '路由表')),
    ('KB-106', 'network.md', 'DNS与域名', ('dns', '域名解析', '域名', 'getent', 'nslookup')),
    ('KB-107', 'network.md', 'TCP与UDP及端口', ('tcp', 'udp', '端口', 'listen', 'estab', 'ss')),
    ('KB-108', 'network.md', 'NAT与回程', ('nat', '回程', '私网', '公网', '地址转换')),
    ('KB-109', 'remote.md', 'SSH与身份验证', ('ssh', 'publickey', '主机密钥', 'host key', '远程登录')),
    ('KB-110', 'remote.md', 'SCP与SFTP', ('scp', 'sftp', '文件传输', '复制文件', '上传', '下载文件')),
    ('KB-111', 'remote.md', 'SSH端口转发', ('ssh隧道', 'ssh 隧道', '端口转发', '本机转发', '本地转发')),
    ('KB-112', 'remote.md', '代理与HTTPS', ('代理', 'https', 'tls', '证书', 'model_connection_failed')),
    ('KB-113', 'services.md', 'systemd服务状态', ('systemd', 'systemctl', 'active', 'inactive', 'oneshot', '服务状态')),
    ('KB-114', 'services.md', '启动与开机自启', ('开机自启', '启动顺序', 'enabled', 'disabled', 'rc.local', '启动入口')),
    ('KB-115', 'services.md', '日志与时间', ('日志', 'journalctl', '时间戳', '时区', 'utc')),
    ('KB-116', 'services.md', 'HTTP与nginx及Node', ('http', 'nginx', 'node', '反向代理', '502', '404', '401')),
    ('KB-117', 'database.md', '数据库与表', ('数据库', '数据表', '表与字段', '字段', 'mysql', '达梦', '数据库模式', 'schema')),
    ('KB-118', 'database.md', 'SELECT与条件查询', ('select', 'where', 'limit', 'order by', 'sql', '查询语句', '条件查询')),
    ('KB-119', 'database.md', '数据库连接问题', ('数据库连接', '连接数据库', 'access denied', '数据库连不上', 'mysql连接')),
    ('KB-120', 'database.md', '备份与导出', ('mysqldump', '数据库备份', '数据库导出', 'sql文件', 'sql 文件', '数据恢复')),
    ('KB-121', 'testing.md', 'ping与时延丢包', ('ping', 'rtt', '时延', '延迟', '丢包')),
    ('KB-122', 'testing.md', 'iperf与吞吐', ('iperf', '吞吐', '带宽测试', 'mbps', 'mb/s')),
    ('KB-123', 'testing.md', '抓包与过滤', ('tcpdump', 'wireshark', '抓包', 'pcap', '报文')),
    ('KB-124', 'testing.md', '实验记录与数据整理', ('csv', '实验记录', '实验数据', '数据整理', '缺失值', '绘图', '时间对齐')),
    ('KB-125', 'development.md', 'Python脚本与报错', ('python脚本', 'python 脚本', '运行脚本', 'traceback', 'modulenotfounderror', 'importerror')),
    ('KB-126', 'development.md', '虚拟环境与依赖', ('venv', 'pip', '虚拟环境', '依赖包', '解释器')),
    ('KB-127', 'development.md', 'JSON与接口请求', ('json', 'api接口', 'api 接口', '接口请求', '请求响应', 'jsondecodeerror')),
    ('KB-128', 'development.md', 'Git与版本管理', ('git', '暂存', 'commit', '版本管理', '工作区')),
    ('KB-129', 'reliability.md', 'Keepalived配置阅读与切换条件', ('keepalived配置阅读',)),
    ('KB-130', 'reliability.md', '工具超时与重试补偿边界', ('工具超时', '幂等', '结果未知', '补偿')),
]

BY_ID = {item[0]: item for item in SECTIONS}
ASPECTS = {ident: ['mechanism', 'checklist', 'examples'] for ident in BY_ID}
for ident in ('KB-101', 'KB-104'):
    ASPECTS[ident].append('paths')
for ident in ('KB-107', 'KB-109', 'KB-116'):
    ASPECTS[ident].append('ports')


def matches(term, question):
    if re.fullmatch(r'[a-z0-9_. /-]+', term):
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(term) + r'(?![a-z0-9_])', question))
    return term in question


def is_wiring_query(question):
    """识别实际接线清单；一般交换机/网线概念仍可走通用学习。"""
    return bool(re.search(r'(每根|每条|精确|实际|具体).*(网线|光纤|物理端口)|'
                          r'(网线|光纤).*(插在哪|插哪|对应哪个|接在哪)|'
                          r'布线表|接线表|完整接线|物理接线',question.lower()))


def topic_ids(question):
    q = question.lower()
    public=public_learning_topic(question)
    if public: return [public]
    result = [ident for ident, _, _, terms in SECTIONS if any(matches(t, q) for t in terms)]
    if any(t in q for t in ('网线','光纤','物理端口','交换机')) and not any(matches(t,q) for t in
            ('tcp','udp','ss','listen','estab')):
        result = [i for i in result if i != 'KB-107']
    # 更具体的问法不额外要求整个上位主题，避免“SSH隧道”被拆成两份需求。
    if 'KB-111' in result and not any(t in q for t in ('身份', '密钥', 'publickey', '登录', '默认端口')):
        result = [i for i in result if i != 'KB-109']
    if any(i in result for i in ('KB-118', 'KB-119', 'KB-120')) and not any(t in q for t in
            ('表是什么', '字段', 'schema', '数据库模式', '数据库是什么', '达梦是什么')):
        result = [i for i in result if i != 'KB-117']
    if any(i in result for i in ('KB-109', 'KB-111', 'KB-116')) and not any(matches(t, q) for t in
            ('tcp', 'udp', 'ss', 'listen', 'estab')):
        result = [i for i in result if i != 'KB-107']
    return result


def is_basics_query(question):
    q = question.lower()
    if public_learning_topic(question): return True
    if is_wiring_query(q) or any(t in q for t in ('网线','光纤','物理端口','交换机端口')):
        return False  # 物理接线/接口不是TCP/UDP端口教程的直接覆盖范围。
    # 专有拓扑和实验室具体参数继续走原有范围判断，不能用通用教程代答。
    if any(t in q for t in ('局端', '远端', 'router', '路由器', 'croc', 'keepalived',
                            '主备', '主机', '备机', '拓扑', '固定测试', '固定主机',
                            '维护模式', '正常ha', 'vrrp', 'watchdog', 'tun16', 'ppp',
                            'vip', '业务', '140', '251', '各台设备', '日志时间',
                            'unknown', '四台', '换卡', 'snat', 'masquerade')) or re.search(r'(?<![a-z0-9_])ha(?![a-z0-9_])', q):
        return False
    if any(t in q for t in ('连接池', '事务', '死锁', '索引优化', '许可证', '源码', '算法实现')):
        return False
    private = any(t in q for t in ('实验室', '我们', '本项目', '这台', '这套', '这边'))
    if private and any(t in q for t in ('容量', '占用', '使用率', '型号', '负载')) and not re.search(
            r'怎么|如何|怎样|一般|通用|概念|原理|区别', q):
        return False
    if private and any(t in q for t in ('监听', '端口', '配置', '数据库名', '用户名', '账号',
                                       '密码', '口令', '安装在哪', '程序路径', '服务名',
                                       '哪个服务器', '哪台', '版本', '业务关系', '对应关系', '地址')):
        return False
    return bool(topic_ids(question))


def basic_requirements(question):
    q = question.lower()
    aspects = []
    if any(t in q for t in ('端口', '监听')):
        aspects.append('ports')
    if any(t in q for t in ('源码', '算法实现', '加密算法实现')):
        aspects.append('implementation')
    if any(t in q for t in ('实测', '测得', '测出来')):
        aspects.append('measurement')
    if re.search(r'怎么|如何|怎样|命令|步骤|操作|用法|使用|运行|查询|导出', q):
        # 入门示例不等于完整操作手册，避免把浅层资料标成完整步骤依据。
        aspects.append('procedure' if '完整' in q and any(t in q for t in ('操作', '步骤', '手册')) else 'examples')
    elif any(t in q for t in ('报错', '失败', '排查', '检查', '拒绝', '不通', '连不上')):
        aspects.append('checklist')
    if not aspects:
        aspects = ['mechanism'] if public_learning_topic(question) or any(t in q for t in
            ('是什么', '什么是', '原理', '区别', '用途', '作用', '含义', '解释', '为什么', '基础', '入门')) else ['unassessed']
    return [{'topic': 'basic_' + ident, 'aspect': aspect, 'mode': 'normal_ha', 'groups': []}
            for ident in topic_ids(question) for aspect in list(dict.fromkeys(aspects))]


def coverage(record):
    ident = record['id']
    if ident not in BY_ID:
        return None
    return {'supports': {'basic_' + ident: ASPECTS[ident]}, 'topic_modes': {},
            'gap_topics': [], 'scope': 'general_principle', 'current_state_supported': False}
