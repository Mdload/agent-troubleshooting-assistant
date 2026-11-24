"""每次规划记录耗时、大小、阶段；不把正文或凭据放入指标。"""
import json
import time
from chat_model import ModelFailure
from device_registry import DEVICES, allows_check

STAGES={'building_request','capacity_guard','awaiting_headers','reading_body','decoding','complete'}
CODES={'model_timeout','model_output_truncated','model_invalid_json','model_invalid_response',
       'model_connection_failed','model_http_error','model_rate_limited','model_circuit_open','evaluation_budget_reached','time_budget_reached'}
NUMBERS=('request_bytes','context_bytes','prompt_bytes','response_bytes','max_output_tokens',
         'wait_headers_ms','read_body_ms','elapsed_ms','http_status','timeout_seconds','retry_after_seconds',
         'prompt_tokens','completion_tokens','reasoning_tokens','response_content_bytes',
         'json_error_line','json_error_column','json_error_position','backend_attempts','transport_attempts')
LABELS={'stage':STAGES, 'failure_kind':{'socket_timeout','dns','tls','refused','reset','connection_other'},
        'rate_limit_kind':{'quota','provider_capacity','concurrency','token_rate','request_rate','unknown'},
        'prompt_kind':{'diagnostic','general_learning','lab_knowledge','material_analysis'},
        'finish_reason':{'stop','length','tool_calls','content_filter'},
        'json_failure_kind':{'response_syntax','action_syntax','duplicate_key','nonfinite_number',
                             'excessive_nesting','content_not_text','action_not_object'}}


def safe_metrics(metrics):
    if type(metrics) is not dict: return {}
    result={key:metrics[key] for key in NUMBERS
            if type(metrics.get(key)) in (int,float) and 0<=metrics[key]<float('inf')}
    result.update({key:metrics[key] for key,allowed in LABELS.items()
                   if isinstance(metrics.get(key),str) and metrics[key] in allowed})
    if type(metrics.get('transport_attempted')) is bool:
        result['transport_attempted']=metrics['transport_attempted']
    for key in ('request_json_mode','request_thinking_disabled','answer_wrapper_removed','knowledge_text_answer'):
        if type(metrics.get(key)) is bool:result[key]=metrics[key]
    return result


def proposed_parameters(action):
    device, check = action.get('device'), action.get('check')
    result = {}
    if isinstance(device,str) and device in DEVICES:
        result['device'] = device
        if allows_check(device,check):
            result['check'] = check
    scope = action.get('scope')
    if isinstance(scope,dict):
        result['scope'] = {
            key:[item for item in values if allows_check(key,item) or item == 'read_only']
            for key,values in scope.items() if key in DEVICES and isinstance(values,list)
        }
    runbook = action.get('runbook')
    if runbook in ('ha_state','routing','logs','services','interfaces','croc_fault'):
        result['runbook'] = runbook
        devices = action.get('devices')
        if isinstance(devices,list):
            result['devices'] = [item for item in devices if isinstance(item,str) and item in DEVICES]
    return result


def traced_plan(planner, context, trace, audit, task, step):
    began=time.monotonic()
    row={'step':step,'phase':('correction' if context.get('correction_feedback') else
                            'after_observations' if context.get('observations') else 'initial'),
         'context_bytes':len(json.dumps(context,ensure_ascii=False).encode('utf-8')),
         'source_count':len(context.get('sources',[])),
         'observation_count':len(context.get('observations',[])),
         'outcome':'planner_error','remaining_seconds':context.get('remaining_seconds')}
    try:
        action,usage=planner(context)
        row['outcome']='response_received'
        row['action']=action.get('action') if isinstance(action,dict) and action.get('action') in (
            'read','read_overview','search','answer','clarify','prepare_diagnostic','investigate',
            'request_scope','unavailable') else 'invalid'
        return action,usage
    except ModelFailure as error:
        row['outcome']=error.code if error.code in CODES else 'model_error'
        raise
    finally:
        row['elapsed_ms']=round((time.monotonic()-began)*1000,3)
        # 测试规划器可能没有metrics；只接受固定类别及非负数字。
        metrics=getattr(planner,'last_metrics',None)
        if type(metrics) is dict and row['outcome']!='evaluation_budget_reached':
            row['transport']=safe_metrics(metrics)
        trace.append(row)
        parameters=proposed_parameters(action) if row['outcome']=='response_received' and isinstance(action,dict) else {}
        audit.add(task,step,'model',dict(row,proposal=parameters))
