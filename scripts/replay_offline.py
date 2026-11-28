"""四场景确定性合成回放；零模型调用、零设备读取，结果拒绝覆盖。"""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evaluation.remote_replay import load_cases,replay

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.exists():parser.error('结果已存在，请使用新的输出文件')
    rows=[replay(case) for case in load_cases()]
    report={'kind':'deterministic_synthetic_replay','not_model_accuracy':True,
            'model_api_calls':0,'live_device_reads':0,'cases':rows}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'scenarios':len(rows),'engineering_assertions_passed':len(rows),
                      'model_api_calls':0,'live_device_reads':0}))

if __name__=='__main__':main()
