"""跨节点VIP证据链：正式HTTP与确定性替身，禁止模型外网/SSH。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evaluation.ha_model import isolated_io
from tests.ha_case_test_support import case_probe,case_planner
from tests.workflow_test_support import RunningApp
from topology_model import snapshot

SCENARIOS=(
    ('primary_owner','owner_running','nonowner_stopped','single_resource_holder_observed'),
    ('backup_owner','nonowner_stopped','owner_running','single_resource_holder_observed'),
    ('duplicate_vip','owner_running','owner_running','resource_overlap_observed'),
    ('missing_vip','nonowner_stopped','nonowner_stopped','resource_missing_observed'),
    ('split_resources','partial_vip','partial_vip','resource_distribution_split'),
)


def replay():
    rows=[]
    with isolated_io():
        for name,primary,backup,expected in SCENARIOS:
            reads=[]
            def probe(device,check):
                reads.append({'device':device,'check':check})
                item=case_probe(primary if device=='local-primary' else backup)(device,check)
                if name=='split_resources' and device=='local-backup' and check=='interfaces':
                    item['output']='eth2 UP\neth3 UP 192.0.2.37/24'
                return item
            def planner(context):
                action,usage=case_planner(context)
                if action['action']=='answer':
                    action['answer']='合成回放：'+action['answer']+'\n\n'+'\n'.join(
                        pair['summary']+' '+pair['limits'] for pair in context['diagnostic_case']['pairs'])
                return action,usage
            with RunningApp(planner,probe,lookup=lambda q:[]) as client:
                code,requested=client.diagnostic_chat('请检查局端主机和备机当前VIP分布。')
                assert code==200 and requested['status']=='approval_required'
                task=requested['task']['id']
                assert client.resume(task)[0]==409 and not reads
                client.login();assert client.approve(task)[0]==200
                code,finished=client.resume(task)
                result=finished['result'];pair=result['diagnostic_case']['pairs'][0]
                assert code==200 and result['status']=='answered',name
                assert pair['finding']==expected and len(reads)==4,name
                assert len({(r['device'],r['check']) for r in reads})==4
                assert not pair['simultaneous_snapshot'] and not pair['root_cause_confirmed']
                rows.append({'scenario':name,'synthetic_reads':reads,'result':result,
                             'audit':client.service.audit.read(task)})
    return {'kind':'offline_cross_node_contract_replay','semantic_accuracy':None,
        'model_api_calls':0,'live_device_reads':0,'not_live_acceptance':True,
        'topology':snapshot(),'scenarios':rows,
        'code_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('ha_croc_case.py','topology_model.py','scripts/replay_ha_pair.py')}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('结果已存在，请使用新路径')
    report=replay()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'scenarios':len(report['scenarios']),'model_api_calls':0,'live_device_reads':0}))


if __name__=='__main__':main()
