"""完整设备×检查矩阵；缺样本在模型调用前报错，不伪装成设备故障。"""
import copy
from device_tools import CHECKS

# 冻结题库当时采集的三类状态。新工具不能反向要求旧样本编造日志。
BASE_DEVICES = ('local-primary', 'local-backup', 'remote-primary', 'remote-backup')
BASE_CHECKS = ('services', 'interfaces', 'routes')


def validate_cases(cases):
    ids=set()
    for case in cases:
        if case['id'] in ids: raise ValueError('Duplicate case')
        ids.add(case['id'])
        matrix=case['matrix']
        if set(matrix)!=set(BASE_DEVICES): raise ValueError('Incomplete device matrix')
        for device in BASE_DEVICES:
            supplied = set(matrix[device])
            if not set(BASE_CHECKS) <= supplied or not supplied <= set(CHECKS):
                raise ValueError('Incomplete check matrix')
            for item in matrix[device].values():
                if (set(item)-{'ok','output','error'} or type(item.get('ok')) is not bool or
                        not isinstance(item.get('output'),str)):
                    raise ValueError('Invalid synthetic observation')
                if item['ok'] and item.get('error'): raise ValueError('Success contains error')
                if not item['ok'] and (item['output'] or not isinstance(item.get('error'),str) or not item['error']):
                    raise ValueError('Invalid failure observation')


def make_probe(case):
    validate_cases([case])
    def probe(device,check):
        # 不连接任何设备；未登记请求直接拒绝，不构造假成功。
        if device not in BASE_DEVICES or check not in CHECKS: raise ValueError('Unsupported synthetic request')
        if check not in case['matrix'][device]:
            raise ValueError('Synthetic check not provided')
        item=copy.deepcopy(case['matrix'][device][check])
        item.update(collected_at='SIMULATION',clock_source='synthetic')
        item['output']='[合成测试快照，不是真实设备状态]\n'+item['output']
        return item
    return probe
