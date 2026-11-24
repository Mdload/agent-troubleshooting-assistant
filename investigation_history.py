"""从服务器原始任务恢复只读历史；不接收浏览器伪造的OBS或恢复旧授权。"""
import copy
from approval_store import TaskError
from conversation_topic import validate_conversation_id

NOTE='历史调查记录，仅代表原采集时刻；不证明当前状态，不恢复审批或数据使用同意。'
ENDED=('completed','incomplete','rejected','cancelled','expired')


def summary(store,db,row):
    times=db.execute('SELECT min(at),max(at) FROM workflow_events WHERE task=?',(row['id'],)).fetchone()
    return {'id':row['id'],'question':row['question'],'status':row['status'],
        'conversation_id':row['conversation'],'scene':row['scene'],
        'started_at':times[0],'updated_at':times[1],'reads':row['attempts'],
        'has_result':bool(row['result']),'read_only':True}


def list_records(store,owner,conversation):
    validate_conversation_id(conversation)
    with store.connect() as db:
        rows=db.execute("SELECT * FROM workflow_tasks WHERE owner=? AND conversation=? "
            "AND status IN ('completed','incomplete','rejected','cancelled','expired') ORDER BY rowid DESC LIMIT 21",
            (owner,conversation)).fetchall()
        return {'records':[summary(store,db,row) for row in rows[:20]],'has_more':len(rows)>20,
            'read_only':True,'note':NOTE}


def get_record(store,owner,conversation,ident):
    validate_conversation_id(conversation)
    with store.connect() as db:
        row=store.row(db,ident,owner)
        if row['conversation']!=conversation:raise TaskError('not_found')
        if row['status'] not in ENDED:raise TaskError('investigation_not_ended')
        result=copy.deepcopy(store.public(row,db)['result'])
        record=summary(store,db,row)
    if isinstance(result,dict):
        # 只修改返回投影；数据库原始OBS和调用结果保持原样。
        result.pop('task',None)
        result['evidence_scope']={'kind':'historical','note':NOTE,'current_reads':0}
        for item in result.get('evidence',[]):item['freshness']='historical'
        from execution_workflow import observation_steps
        if result.get('evidence'):result['execution_steps']=observation_steps(result['evidence'])
        for step in result.get('execution_steps',[]):step['freshness']='historical'
        if isinstance(result.get('diagnostic_report'),dict):result['diagnostic_report']['note']=NOTE
    record.update(note=NOTE,result=result)
    return record
