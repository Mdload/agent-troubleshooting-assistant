"""审批时冻结的调查总量；阶段只划分取证，不产生新授权或重置预算。"""


def legacy_limits():
    return {'version':'single_stage_v1','max_reads':4,'reads_per_stage':4,
            'max_stages':1,'max_steps':7,'max_seconds':180}


def requested_limits(scope,staged=False):
    checks=sum(len(items) for items in scope.values())
    if not staged or checks<=4:return legacy_limits()
    total=min(12,checks)
    return {'version':'bounded_stages_v1','max_reads':total,'reads_per_stage':4,
            'max_stages':(total+3)//4,'max_steps':total+4,'max_seconds':180}


def validate_limits(limits,scope):
    if not isinstance(limits,dict) or set(limits)!=set(legacy_limits()):
        raise ValueError('Invalid investigation limits')
    if any(type(limits[key]) is not int for key in limits if key!='version'):
        raise ValueError('Invalid investigation limits')
    expected=(requested_limits(scope,True) if limits.get('version')=='bounded_stages_v1'
              else legacy_limits())
    if limits!=expected:raise ValueError('Invalid investigation limits')
    return dict(limits)


def stage_budget(limits,reads):
    remaining=max(0,limits['max_reads']-reads)
    number=min(reads//limits['reads_per_stage']+1,limits['max_stages'])
    in_stage=min(remaining,limits['reads_per_stage']-reads%limits['reads_per_stage'])
    return {'number':number,'max_stages':limits['max_stages'],
            'remaining_reads':in_stage,'parent_remaining_reads':remaining}
