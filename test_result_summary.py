"""指标口径：未知不补零，回答不算答对，重复ID不混入统计。"""
import json
from pathlib import Path
import tempfile
import unittest
from evaluation.result_summary import summarize


class ResultSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.save('run.json',{'status':'stopped','cases':[{'id':'A01'},{'id':'A02'}],
                              'unexecuted_cases':['A03']})
        self.save('A01.json',{'id':'A01','result':{'status':'answered'},'elapsed_seconds':10,
            'automatic_checks':{'scope':True},'adapter_calls':[{'outcome':'response_received',
                'usage':25,'transport':{'transport_attempted':True}}]})
        self.save('A02.json',{'id':'A02','result':{'status':'incomplete','reason':'model_circuit_open'},
            'elapsed_seconds':0.01,'automatic_checks':{'scope':True},'adapter_calls':[
                {'outcome':'model_timeout','usage':None,'transport':{'transport_attempted':True}},
                {'outcome':'model_circuit_open','usage':0,'transport':{'transport_attempted':False}}]})

    def tearDown(self):self.temp.cleanup()

    def save(self,name,value):
        (self.root/name).write_text(json.dumps(value),encoding='utf-8')

    def test_completed_status_never_generates_accuracy_and_unknown_usage_is_separate(self):
        report=summarize(self.root)
        self.assertEqual(report['case_statuses'],{'answered':1,'incomplete':1})
        self.assertIsNone(report['semantic_accuracy'])
        self.assertEqual(report['usage']['known_total_tokens'],25)
        self.assertEqual(report['usage']['unknown_usage_calls'],1)
        self.assertEqual(report['transport'],{'sent_known':2,'not_sent_known':1,'unknown':0})
        self.assertEqual(report['pending_cases'],['A03'])

    def test_legacy_missing_metrics_and_missing_records_do_not_turn_into_zero(self):
        row=json.loads((self.root/'A01.json').read_text());row.pop('adapter_calls');self.save('A01.json',row)
        row=json.loads((self.root/'A02.json').read_text());row['adapter_calls'][0].pop('transport');self.save('A02.json',row)
        report=summarize(self.root)
        self.assertEqual(report['cases_missing_call_records'],1)
        self.assertEqual(report['transport']['unknown'],1)
        self.assertEqual(report['usage']['unknown_usage_calls'],1)

    def test_false_contract_and_missing_latency_are_reported_without_fabricating_values(self):
        row=json.loads((self.root/'A01.json').read_text());row.pop('elapsed_seconds');row['automatic_checks']['scope']=False;self.save('A01.json',row)
        report=summarize(self.root)
        self.assertEqual(report['contract_failed_cases'],['A01'])
        self.assertEqual(report['cases_missing_timing'],['A01'])
        self.assertEqual(report['latency_all_attempted']['count'],1)
        self.assertEqual(report['latency_all_attempted']['p95_seconds'],0.01)

    def test_duplicate_and_path_like_ids_are_rejected(self):
        for ids in (['A01','A01'],['../A01']):
            self.save('run.json',{'status':'stopped','cases':[{'id':i} for i in ids],'unexecuted_cases':[]})
            with self.assertRaises(ValueError):summarize(self.root)
