"""每次规划记录耗时、大小、阶段；不把正文或凭据放入指标。"""
import json
import time
from chat_model import ModelFailure

STAGES={'building_request','awaiting_headers','reading_body','decoding','complete'}
CODES={'model_timeout','model_output_truncated','model_invalid_json','model_invalid_response',
       'model_connection_failed','model_http_error','model_rate_limited','evaluation_budget_reached'}
NUMBERS=('request_bytes','context_bytes','prompt_bytes','response_bytes','max_output_tokens',
         'wait_headers_ms','read_body_ms','elapsed_ms','http_status','timeout_seconds')


def traced_plan(planner, context, trace, audit, task, step):
    began=time.monotonic()
    row={'step':step,'phase':('correction' if context.get('correction_feedback') else
                            'after_observations' if context.get('observations') else 'initial'),
         'context_bytes':len(json.dumps(context,ensure_ascii=False).encode('utf-8')),
         'source_count':len(context.get('sources',[])),
         'observation_count':len(context.get('observations',[])),
         'outcome':'planner_error'}
    try:
        action,usage=planner(context)
        row['outcome']='response_received'
        row['action']=action.get('action') if isinstance(action,dict) and action.get('action') in ('read','read_overview','search','answer') else 'invalid'
        return action,usage
    except ModelFailure as error:
        row['outcome']=error.code if error.code in CODES else 'model_error'
        raise
    finally:
        row['elapsed_ms']=round((time.monotonic()-began)*1000,3)
        # 测试规划器可能没有metrics；只接受固定类别及非负数字。
        metrics=getattr(planner,'last_metrics',None)
        if type(metrics) is dict and row['outcome']!='evaluation_budget_reached':
            safe={k:metrics[k] for k in NUMBERS if type(metrics.get(k)) in (int,float) and metrics[k]>=0}
            if metrics.get('stage') in STAGES: safe['stage']=metrics['stage']
            row['transport']=safe
        trace.append(row)
        audit.add(task,step,'model',row)
