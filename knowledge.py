"""按章节读取拓扑原文，保留日期、模式和来源；不访问设备。"""
import re

# ID绑定章节标题，新增其他章节不会改变已有引用编号。
SECTIONS = [
    ('mechanisms.md', '三层分工', 'KB-001', [], []),
    ('mechanisms.md', '外层与内层', 'KB-002', [], []),
    ('mechanisms.md', 'Router', 'KB-003', ['normal_ha'], ['router']),
    ('mechanisms.md', '远端', 'KB-004', ['normal_ha'], ['remote']),
    ('mechanisms.md', '局端', 'KB-005', ['normal_ha'], ['local']),
    ('mechanisms.md', '固定主机测试模式', 'KB-006', ['fixed_primary_test'], []),
    ('mechanisms.md', '对排障助手的要求', 'KB-007', [], []),
    ('devices-and-programs.md', '设备与程序', 'KB-008', [], []),
    ('devices-and-programs.md', '关键程序路径', 'KB-009', [], []),
    ('devices-and-programs.md', '角色与网络细节', 'KB-010', [], []),
    ('sources-and-gaps.md', '已识别的冲突', 'KB-011', [], []),
    ('sources-and-gaps.md', '未确认与后续取证', 'KB-012', [], []),
    ('local-ha-rules.md', '局端触发切换的规则', 'KB-013', ['normal_ha'], ['local']),
    ('local-ha-rules.md', '局端健康判据和故障计数', 'KB-014', ['normal_ha'], ['local']),
    ('local-ha-rules.md', '局端watchdog恢复与切换的区别', 'KB-015', ['normal_ha'], ['local']),
    ('diagnostic-guide.md', '局端备机停止与异常的区分', 'KB-016', ['normal_ha'], ['local']),
    ('diagnostic-guide.md', '排障路径与工具覆盖的边界', 'KB-017', [], []),
    ('diagnostic-guide.md', '配置阈值与实测恢复时间', 'KB-018', ['normal_ha'], ['local']),
]

def load_knowledge(root):
    records, sources = [], []
    for filename, heading, ident, modes, groups in SECTIONS:
        path = root / 'docs' / 'topology' / filename
        text = path.read_text(encoding='utf-8-sig')
        # 到下一标题为止，避免把其他模式的章节混入当前证据。
        pattern = r'^#{1,3} ' + re.escape(heading) + r'\s*\n(.*?)(?=^#{1,3} |\Z)'
        match = re.search(pattern, text, re.M | re.S)
        if not match or not match.group(1).strip():
            raise ValueError('Missing knowledge section: ' + ident)
        source_id = 'DOC-' + ident
        # 整理时间与现场证据时间不同；不以文档更新刷新旧现场证据。
        document_date = '2026-10-02'
        snapshot_date = '2026-10-02'
        records.append(dict(
            id=ident, title=heading, kind='topology', evidence_kind='anonymous_example', evidence_date=snapshot_date,
            documentation_date=document_date,
            operating_modes=modes, device_groups=groups,
            symptom='', historical_finding=match.group(1).strip(),
            historical_outcome='', proposed_read_only_checks=[],
            verification_limits='匿名示例资料的编写日期；无真实现场采集，不等于端到端验收。',
            sources=[source_id], repair_enabled=False))
        sources.append(dict(id=source_id, title=heading,
                            path='docs/topology/' + filename,
                            coverage=heading, note='匿名示例资料；编写日期：'+document_date+'；没有现场采集或真实状态'))
    return records, sources
