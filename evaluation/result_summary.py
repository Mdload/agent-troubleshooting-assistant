"""只读汇总固定评测结果；完成率/契约检查不冒充语义准确率。"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def latency(values):
    values=sorted(values)
    return {'count':len(values),'method':'nearest_rank',
            'p50_seconds':values[math.ceil(len(values)*0.5)-1] if values else None,
            'p95_seconds':values[math.ceil(len(values)*0.95)-1] if values else None}


def summarize(directory):
    directory=Path(directory)
    raw=(directory/'run.json').read_bytes()
    run=json.loads(raw)
    ids=[item['id'] for item in run['cases']]
    pending=run['unexecuted_cases']
    if (len(ids)!=len(set(ids)) or len(pending)!=len(set(pending)) or set(ids)&set(pending)
            or any(not isinstance(i,str) or not re.fullmatch(r'[AF][0-9]{2}',i) for i in ids+pending)):
        raise ValueError('Invalid or duplicate case IDs')
    statuses=Counter();reasons=Counter();outcomes=Counter()
    known_tokens=unknown_usage=known_sent=known_blocked=unknown_transport=0
    invocations=missing_calls=reviewed=0
    elapsed=[];with_model=[];contract_failed=[];timing_missing=[]
    for ident in ids:
        path=directory/(ident+'.json')
        if path.is_symlink():raise ValueError('Case result must be a regular local file')
        row=json.loads(path.read_text(encoding='utf-8'))
        if row['id']!=ident:raise ValueError('Case ID mismatch')
        result=row['result'];statuses[result.get('status','unreported')]+=1
        if result.get('reason'):reasons[result['reason']]+=1
        checks=row.get('automatic_checks',{})
        if any(value is False for value in checks.values()):contract_failed.append(ident)
        if row.get('semantic_review',{}).get('verdict') is not None:reviewed+=1
        duration=row.get('elapsed_seconds')
        if valid_number(duration):elapsed.append(duration)
        else:timing_missing.append(ident)
        calls=row.get('adapter_calls')
        if not isinstance(calls,list):
            missing_calls+=1
            continue
        if calls and valid_number(duration):with_model.append(duration)
        invocations+=len(calls)
        for call in calls:
            outcomes[call.get('outcome','unreported')]+=1
            usage=call.get('usage')
            if type(usage) is int and usage>=0:known_tokens+=usage
            else:unknown_usage+=1
            sent=call.get('transport',{}).get('transport_attempted')
            if sent is True:known_sent+=1
            elif sent is False:known_blocked+=1
            else:unknown_transport+=1
    return {'kind':'engineering_evaluation_summary_not_accuracy',
        'input_run_sha256':hashlib.sha256(raw).hexdigest(),'run_status':run['status'],
        'attempted_cases':len(ids),'pending_cases':pending,'case_statuses':dict(statuses),
        'stop_reasons':dict(reasons),'model_outcomes':dict(outcomes),
        'planner_invocations_reported':invocations,'cases_missing_call_records':missing_calls,
        'transport':{'sent_known':known_sent,'not_sent_known':known_blocked,'unknown':unknown_transport},
        'usage':{'known_total_tokens':known_tokens,'unknown_usage_calls':unknown_usage,
                 'cases_missing_call_records':missing_calls},
        'latency_all_attempted':latency(elapsed),'latency_model_invoked_cases':latency(with_model),
        'cases_missing_timing':timing_missing,'contract_failed_cases':contract_failed,
        'cases_with_recorded_semantic_verdict':reviewed,'semantic_accuracy':None,
        'limitations':['Status answered does not prove correctness.',
                      'No inference of zero calls or usage from missing records.',
                      'Latency includes each case host workflow; small/mixed samples are not a service SLO.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=summarize(args.directory)
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
