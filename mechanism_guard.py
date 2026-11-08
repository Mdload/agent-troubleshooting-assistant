"""来源规则和局部观测的有限矛盾检查；不是通用语义裁判。"""
import re
from device_tools import DEVICES, CHECKS


def observation_constraints(observations):
    # 当前登记工具只读服务、接口/连接和主表路由；没有隧道数据路径验收。
    # 不解析原文猜状态，也不把来源未知或合成观测升级成现场事实。
    good = [item for item in observations if item.get('ok') is True
            and item.get('device') in DEVICES and item.get('check') in CHECKS]
    if not good:
        return []
    return [{'id': 'snapshot_tunnel', 'refs': [item['id'] for item in good],
             'statement': '本轮成功工具仅覆盖服务管理状态、接口标志/地址、TCP监听/连接或主表路由。'
                          'UP、ESTAB、LISTEN不能证明Croc隧道整体正常，也不能排除外层或Croc内部问题，'
                          '不能将故障位置锁定或收敛为仅内层/路由。请描述具体观测并列未核验项，'
                          '不要用泛称正常代替具体状态；未采集隧道数据路径或端到端流量。'}]


def mechanism_constraints(sources, group='unknown'):
    # 只从本轮已提供、正常HA、局端规则生成约束，不凭编号或旧聊天造依据。
    if group in ('remote', 'router'):
        return []
    records = {item.get('id'): item for item in sources
               if item.get('kind') == 'topology'
               and item.get('device_groups') == ['local']
               and item.get('operating_modes') == ['normal_ha']}
    rules = []
    local = records.get('KB-013', {}).get('historical_finding', '')
    health = records.get('KB-014', {}).get('historical_finding', '')
    watchdog = records.get('KB-015', {}).get('historical_finding', '')
    if 'notify角色脚本只启停自身节点的Croc' in local:
        rules.append({'id': 'local_notify', 'refs': ['KB-013'],
                      'statement': '每台局端的notify角色脚本通过本机systemctl启停自身Croc；不远程控制对端。'})
    if '健康不是所有接管路径的必要条件' in local:
        rules.append({'id': 'local_election', 'refs': ['KB-013'],
                      'statement': '健康备机100高于降权主机90只是特定比较。仅降权不等于退出选举；健康不是所有接管路径的必要条件，仍需实际角色/候选/优先级条件。'})
    if '健康检查失败仅表示该角色的某项检查未通过' in health:
        rules.append({'id': 'local_business', 'refs': ['KB-014'],
                      'statement': 'owner要求Croc运行，无VIP备机要求Croc停止；两套判据不同。健康失败不能推出接管后业务必断；完整健康检查是合取，任一项失败只表示未全部通过，不等于任何业务都不通。需具体失败项和实际目标路径证据。'})
    if '跟踪项是否导致FAULT或降权取决于实际权重配置' in local:
        rules.append({'id': 'local_interface', 'refs': ['KB-013'],
                      'statement': 'eth2的VRRP通告路径与eth3的track_interface分开；接口跟踪故障可能FAULT或降权，依实际配置，不能一律说只是降低优先级。'})
    if '故障标记允许恢复' in watchdog and '15秒' in watchdog and '300秒' in watchdog:
        rules.append({'id': 'local_watchdog', 'refs': ['KB-015'],
                      'statement': '解释watchdog触发条件时保留故障标记允许恢复、持VIP/服务active、Recv-Q>0且两远端不可达、300秒冷却、15秒后重新确认；这些配置不保证VIP一直不变或端到端恢复。'})
    return rules


PATTERNS = {
    # 只覆盖已经复现的整体正常和定位收敛说法；未命中不代表语义通过。
    'snapshot_tunnel': [
        r'(?:Croc\s*)?隧道(?:\s*(?:两端|整体|建立|运行|状态|连接|数据路径|转发|均|都|已|已经|恢复)){0,4}\s*(?:正常|健康|无故障|没有问题|没问题)',
        r'(?:问题|故障|原因|可能位置)[^。；\n]{0,16}(?:收敛|限定|锁定)[^。；\n]{0,12}(?:内层|路由)',
        r'(?:可以|能够|足以|已经|已)(?:完全|基本)?排除(?:外层|Croc)[^。；\n]{0,12}(?:故障|问题|原因)',
    ],
    'local_notify': [
        r'notify[^。；\n]{0,35}(?:对端|另一台|另一节点|远程)[^。；\n]{0,20}Croc[^。；\n]{0,8}(?:启停|启动|停止|重启)',
        r'notify[^。；\n]{0,35}(?:启停|启动|停止|重启)[^。；\n]{0,12}(?:对端|另一台|另一节点)[^。；\n]{0,10}Croc',
    ],
    'local_election': [
        r'(?:备机|备用机|备用节点)[^。；\n]{0,8}(?:只有|必须)[^。；\n]{0,8}健康[^。；\n]{0,20}(?:才|方能)[^。；\n]{0,20}(?:接管|接替|资格|优先级)',
        r'健康(?:检查通过)?是[^。；\n]{0,8}(?:备机|备用机)[^。；\n]{0,8}接管的必要条件',
    ],
    'local_business': [
        r'(?:两台|双机|两边)[^。；\n]{0,12}(?:不健康|健康(?:检查)?失败|健康检查都失败)[^。；\n]{0,150}业务[^。；\n]{0,10}(?:照样不通|一定不通|必然不通|必然中断|一定中断|肯定不通|肯定中断|都不可用)',
        r'(?:两台|双机|两边)[^。；\n]{0,12}(?:不健康|健康(?:检查)?失败|健康检查都失败)[^。；\n]{0,80}(?:一定|必然|肯定)[^。；\n]{0,5}业务[^。；\n]{0,5}(?:不通|中断|不可用)',
        r'(?:任一|任何一)(?:项|个检查项|个门禁|环节)[^。；\n]{0,8}(?:失败|不满足|未通过)[^。；\n]{0,30}业务[^。；\n]{0,8}(?:不通|中断|不可用|无法恢复)',
    ],
    'local_interface': [
        r'(?:track_interface|接口跟踪)[^。；\n]{0,20}(?:故障|失败)[^。；\n]{0,8}(?:只|仅|一律)[^。；\n]{0,8}(?:降权|降低优先级)',
    ],
}


def contradicted_constraints(answer, constraints):
    # 只识别肯定的特定矛盾；否定错误说法和提问不会触发。
    # 未命中不表示答案正确，引用有效也不表示每项因果关系成立。
    text = re.sub(r'```.*?```', '', answer, flags=re.S)
    snapshot_text = re.sub(r'`[^`\n]*`', '', text).replace('**', '')
    text = text.replace('**', '').replace('`', '')
    problems = []
    for rule in constraints:
        rule_text = snapshot_text if rule['id'] == 'snapshot_tunnel' else text
        found = False
        for pattern in PATTERNS.get(rule['id'], []):
            for match in re.finditer(pattern, rule_text, flags=re.I):
                fragment = match.group()
                # 针对某条已限定业务路径的影响，不能当作“全部业务必断”拦截。
                if rule['id'] == 'local_business' and re.search(r'相关业务|对应业务|该业务|本次业务|这条业务|指定目标|关键路径', fragment):
                    continue
                start = max(rule_text.rfind(mark, 0, match.start()) for mark in ('。', '；', '\n', '，', ',')) + 1
                prefix = rule_text[start:match.start()][-24:]
                if rule['id'] == 'snapshot_tunnel':
                    sentence_start = max(rule_text.rfind(mark, 0, match.start()) for mark in ('。', '；', '\n')) + 1
                    sentence_prefix = rule_text[sentence_start:match.start()]
                    if re.search(r'如果|假设|若|当.*时', sentence_prefix):
                        continue
                    if re.search(r'无法(?:确认|证明|认定)|不能(?:确认|证明|认定|保证|据此宣布)|未(?:验证|核实|确认)|不等于|不代表|不意味着|不是|不知道|错误示例|代码示例', prefix):
                        continue
                if re.search(r'不能说|不可说|不应说|不要说|不能写|不可写|并非|错误说法|错误示例', prefix):
                    continue
                if re.search(r'不是|并非|不负责|不控制|不远程|不能|不一定|不意味着|不代表', fragment):
                    continue
                if re.match(r'[吗呢？?]', rule_text[match.end():match.end()+4]):
                    continue
                problems.append(dict(rule))
                found = True
                break
            if found:
                break
    return problems
