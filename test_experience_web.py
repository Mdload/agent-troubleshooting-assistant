"""经验闭环的合成隔离测试；不审核真实样例、不调用模型或设备。"""
import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import app
import qa
from experience_store import ExperienceStore, ExperienceError
from workflow_test_support import RunningApp


QUESTION='局端备机没有VIP时Croc停止为什么合理'
REGRESSION='局端备机无VIP且Croc停止的正常HA规则是什么'


def submission():
    ref=next(r for r in qa.KNOWLEDGE if r['id']=='KB-016')
    return {'conversation_id':'test-chat','question_id':'q1','answer_id':'a1',
        'question':QUESTION,'original_answer':'ORIGINAL_PRIVATE_TEXT_NOT_RETAINED',
        'corrected_answer':'正常HA假设下，无VIP且Croc停止符合非持有者的局部预期；实际角色、业务和根因未知。',
        'reason':'合成测试：防止只按服务停止误判故障。','reference_id':'KB-016',
        'quote':ref['historical_finding'][:100],
        'conditions':{'lesson_type':'principle','operating_modes':['normal_ha'],
                      'device_groups':['local'],'software_versions':[],'evidence_date':None},'consent':True}


def approve(store,item):
    return store.review(item['id'],'approve',item['candidate_hash'],'合成夹具的人工审核动作替身。',REGRESSION,'authenticated-admin')


class ExperienceStoreTests(unittest.TestCase):
    def test_principle_does_not_inherit_reference_checklist_or_configuration_coverage(self):
        from knowledge_coverage import assess,answer_issue
        self.publish()
        query='局端备机如何检查Croc服务与VIP'
        records=self.store.records(query)
        self.assertEqual(len(records),1)
        self.assertEqual(assess(QUESTION,records)['status'],'supported')
        self.assertNotEqual(assess(query,records)['status'],'supported')
        self.assertTrue(all(aspects==['mechanism'] for aspects in records[0]['knowledge_coverage']['supports'].values()))
        sources=records+[next(r for r in qa.KNOWLEDGE if r['id']=='KB-016')]
        coverage=assess(query,sources)
        self.assertEqual(coverage['status'],'supported')
        self.assertIsNotNone(answer_issue(coverage,'evidence',[records[0]['id']],sources))
        self.assertIsNone(answer_issue(coverage,'evidence',['KB-016'],sources))

    def test_principle_cannot_publish_as_a_complete_checklist(self):
        item=self.add()
        item=self.store.review(item['id'],'approve',item['candidate_hash'],'仅测试工程审核',
            '局端备机如何检查Croc服务与VIP','authenticated-admin')
        with self.assertRaises(ExperienceError):self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        self.assertEqual(self.store.get(item['id'])['status'],'approved')
        self.assertEqual(self.store.records(QUESTION),[])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        relative=qa.SOURCE_MAP['DOC-KB-016']['path']
        self.source=self.root/relative;self.source.parent.mkdir(parents=True)
        self.source.write_bytes((qa.ROOT/relative).read_bytes())
        self.store=ExperienceStore(self.root/'experiences.db',self.root)

    def tearDown(self):self.temp.cleanup()

    def add(self,owner='owner'):return self.store.submit(owner,submission())

    def publish(self):
        item=approve(self.store,self.add())
        return self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')

    def test_only_selected_question_correction_and_hash_are_retained(self):
        item=self.add()
        self.assertEqual(item['status'],'needs_review')
        self.assertEqual(self.store.records(QUESTION),[])
        with self.store.connect() as db:
            serialized=db.execute('SELECT payload FROM experiences').fetchone()[0]
        self.assertNotIn('ORIGINAL_PRIVATE_TEXT_NOT_RETAINED',serialized)
        self.assertIn('original_answer_sha256',serialized)
        self.assertIn('inferera_answer_context',item['sharing_consent']['uses'])
        self.assertFalse(item['current_state_supported']);self.assertFalse(item['grants_permissions'])

    def test_consent_and_forged_status_cannot_bypass_review(self):
        for changes in ({'consent':False},{'status':'approved'},{'reviewer':'authenticated-admin'}):
            with self.subTest(changes=changes):
                data=submission();data.update(changes)
                with self.assertRaises(ValueError):self.store.submit('owner',data)
        self.assertEqual(self.store.list(),[])

    def test_reference_must_be_known_and_quote_inside_its_section(self):
        for changes in ({'reference_id':'../../.secrets/model.json'},
                        {'quote':'This quote is not in the verified section.'}):
            data=submission();data.update(changes)
            with self.assertRaises(ValueError):self.store.submit('owner',data)
        data=submission();data['conditions']['device_groups']=[]
        with self.assertRaises(ValueError):self.store.submit('owner',data)

    def test_deduplication_is_owner_scoped_and_cross_owner_item_is_hidden(self):
        first=self.add();self.assertEqual(self.add()['id'],first['id'])
        second=self.add('other');self.assertNotEqual(first['id'],second['id'])
        with self.assertRaises(ExperienceError):self.store.get(first['id'],'other')
        self.assertEqual(len(self.store.list('owner')),1)

    def test_approval_is_not_publication_and_publication_runs_real_retrieval_checks(self):
        item=approve(self.store,self.add())
        self.assertEqual(self.store.records(QUESTION),[])
        item=self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        self.assertTrue(item['publication']['checks']['passed'])
        self.assertFalse(item['publication']['checks']['semantic_verified'])
        records=self.store.records(QUESTION)
        self.assertEqual(len(records),1);self.assertEqual(records[0]['id'],'EXP-'+item['id'])
        self.assertEqual(records[0]['reference_id'],'KB-016')
        self.assertEqual(self.store.records('远端备机Croc停止的规则'),[])
        self.assertEqual(self.store.records('维护模式下局端备机Croc停止的规则'),[])

    def test_content_edit_invalidates_published_revision_and_stale_hash(self):
        old=self.publish();data=submission();data['corrected_answer']+='这是局部判断。'
        edited=self.store.update(old['id'],'owner',old['candidate_hash'],{k:data[k] for k in
            ('corrected_answer','reason','reference_id','quote','conditions')})
        self.assertEqual(edited['status'],'needs_review');self.assertIsNone(edited['review'])
        self.assertEqual(self.store.records(QUESTION),[])
        with self.assertRaises(ExperienceError):self.store.publish(old['id'],old['candidate_hash'],'authenticated-admin')

    def test_source_change_invalidates_review_without_serving_old_entry(self):
        item=self.publish();self.source.write_text(self.source.read_text(encoding='utf-8')+'\nchanged\n',encoding='utf-8')
        self.assertEqual(self.store.records(QUESTION),[])
        self.assertEqual(self.store.get(item['id'],'owner')['effective_status'],'review_invalid')

    def test_withdraw_and_wrong_actor_cannot_publish(self):
        item=self.add()
        with self.assertRaises(ExperienceError):approve_result=self.store.review(item['id'],'approve',item['candidate_hash'],'note',REGRESSION,'user')
        item=approve(self.store,item)
        with self.assertRaises(ExperienceError):self.store.publish(item['id'],item['candidate_hash'],'user')
        item=self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        self.store.withdraw(item['id'],item['candidate_hash'],'authenticated-admin')
        self.assertEqual(self.store.records(QUESTION),[])

    def test_parallel_publication_allows_only_one_transition(self):
        item=approve(self.store,self.add())
        def publish(_):
            try:self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin');return 'published'
            except ExperienceError:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(publish,range(2)))
        self.assertEqual(results.count('published'),1)

    def test_regression_cannot_promote_an_uncovered_topic(self):
        item=self.add()
        reviewed=self.store.review(item['id'],'approve',item['candidate_hash'],'Synthetic','我们的新服务监听什么端口','authenticated-admin')
        with self.assertRaises(ExperienceError):self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        self.assertEqual(self.store.get(reviewed['id'])['status'],'approved')

    def test_version_restricted_experience_is_not_used_when_version_is_unknown(self):
        data=submission();data['conditions']['software_versions']=['1.2.3']
        item=self.store.submit('owner',data)
        item=self.store.review(item['id'],'approve',item['candidate_hash'],'合成版本条件检查',REGRESSION+' v1.2.3','authenticated-admin')
        self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        self.assertEqual(self.store.records(QUESTION),[])
        self.assertEqual(self.store.records(QUESTION+' v9.9.9'),[])
        self.assertEqual(len(self.store.records(QUESTION+' v1.2.3')),1)
        records=self.store.records(QUESTION+' v1.2.3')
        self.assertEqual(records[0]['software_versions'],['v1.2.3'])
        hits=qa.retrieve(QUESTION+' v1.2.3',group='local',extra_records=records)
        self.assertTrue(any(h['case']['id']==records[0]['id'] for h in hits))
        self.assertEqual(self.store.records(QUESTION+'，地址192.0.2.13'),[])

    def test_forged_publication_without_matching_review_is_not_retrievable(self):
        item=self.publish()
        with self.store.connect() as db:
            changed=self.store.row(db,item['id']);changed['review']['reviewer']='forged'
            self.store.write(db,changed)
        self.assertEqual(self.store.records(QUESTION),[])

    def test_basic_knowledge_experience_uses_its_anchor_in_explicit_library_scope(self):
        ref=next(r for r in qa.KNOWLEDGE if r['id']=='KB-127')
        data=submission();data.update(question='JSON格式是什么',reference_id='KB-127',quote=ref['historical_finding'][:100])
        data['conditions'].update(operating_modes=[],device_groups=[])
        relative=qa.SOURCE_MAP['DOC-KB-127']['path']
        source=self.root/relative;source.parent.mkdir(parents=True,exist_ok=True)
        source.write_bytes((qa.ROOT/relative).read_bytes())
        item=self.store.submit('owner',data)
        item=self.store.review(item['id'],'approve',item['candidate_hash'],'合成检索检查','JSON数据格式的用途是什么','authenticated-admin')
        self.store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
        records=self.store.records('JSON格式是什么')
        hits=qa.retrieve('JSON格式是什么',library='general_basics',extra_records=records)
        self.assertTrue(any(h['case']['id']=='EXP-'+item['id'] for h in hits))
        from knowledge_coverage import assess
        self.assertEqual(assess('JSON格式是什么',records,basics=True)['status'],'supported')


class ExperienceHttpTests(unittest.TestCase):
    def test_checklist_answer_falls_back_to_reference_instead_of_principle_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);relative=qa.SOURCE_MAP['DOC-KB-016']['path']
            source=root/relative;source.parent.mkdir(parents=True)
            source.write_bytes((qa.ROOT/relative).read_bytes())
            store=ExperienceStore(root/'experiences.db',root)
            item=approve(store,store.submit('owner',submission()))
            store.publish(item['id'],item['candidate_hash'],'authenticated-admin')
            ident='EXP-'+item['id']
            def response(ref):
                return {'action':'answer','answer':'按资料核对服务和VIP，清单不能代替当前状态验收。',
                    'refs':[ref],'basis':'evidence'},1
            planner=Mock(side_effect=[response(ident),response('KB-016')]);probe=Mock()
            with RunningApp(planner,probe) as client:
                client.server.experience_store=store
                code,result=client.chat('局端备机如何检查Croc服务与VIP')
            self.assertEqual(code,200)
            self.assertEqual(result['status'],'answered',result)
            self.assertEqual(result['refs'],['KB-016'])
            self.assertEqual(planner.call_count,2)
            self.assertTrue(any(s['id']==ident for s in planner.call_args_list[0][0][0]['sources']))
            self.assertNotIn(ident,planner.call_args[0][0]['correction_feedback']['valid_refs'])
            probe.assert_not_called()

    def test_unavailable_optional_experience_store_does_not_block_original_qa_or_approval(self):
        planner=Mock(return_value=({'action':'answer','answer':'现有资料仍可用于解释，当前现场未知。',
                                   'refs':['KB-016'],'basis':'evidence'},1))
        probe=Mock()
        with RunningApp(planner,probe) as client:
            client.server.experience_store=Mock()
            client.server.experience_store.records.side_effect=sqlite3.DatabaseError('unavailable')
            result=client.chat(QUESTION)[1]
            self.assertEqual(result['status'],'answered')
            self.assertTrue(planner.call_args[0][0]['sources'])
            requested=client.diagnostic_chat('局端备机当前Croc服务与VIP')[1]
            self.assertEqual(requested['status'],'approval_required')
            probe.assert_not_called()
    def test_owner_cannot_edit_another_browser_or_use_admin_routes(self):
        with RunningApp(Mock(),Mock(),lookup=lambda q:[]) as client:
            item=client.call('/api/experiences',submission())[1]
            other=client.new_browser();client.call('/api/session',browser=other)
            data={'expected_hash':item['candidate_hash'],'changes':{k:submission()[k] for k in
                ('corrected_answer','reason','reference_id','quote','conditions')}}
            self.assertEqual(client.call('/api/experiences/'+item['id']+'/edit',data,browser=other)[0],404)
            self.assertEqual(client.call('/api/admin/experiences/'+item['id']+'/edit',data)[0],401)
            self.assertEqual(client.call('/api/experiences/'+item['id'])[1]['candidate_hash'],item['candidate_hash'])

    def test_published_experience_never_grants_device_permissions(self):
        planner=Mock(return_value=({'action':'read','device':'local-backup','check':'services'},1))
        probe=Mock()
        with RunningApp(planner,probe) as client:
            item=client.call('/api/experiences',submission())[1];client.login()
            client.call('/api/admin/experiences/'+item['id']+'/review',
                {'decision':'approve','expected_hash':item['candidate_hash'],'note':'合成权限检查','regression_question':REGRESSION,'confirmed':True})
            client.call('/api/admin/experiences/'+item['id']+'/publish',{'expected_hash':item['candidate_hash']})
            result=client.chat(QUESTION)[1]
            self.assertEqual(result['status'],'incomplete')
            self.assertEqual(result['events'][-1]['reason'],'check_not_approved')
            self.assertEqual(planner.call_args[0][0]['tool_catalog'],{})
            probe.assert_not_called();self.assertEqual(client.store.list(),[])
    def test_owner_isolation_authentication_csrf_origin_and_no_extra_authority(self):
        with RunningApp(Mock(),Mock(),lookup=lambda q:[]) as client:
            code,item=client.call('/api/experiences',submission());self.assertEqual(code,200)
            other=client.new_browser();client.call('/api/session',browser=other)
            self.assertEqual(client.call('/api/experiences/'+item['id'],browser=other)[0],404)
            self.assertEqual(client.call('/api/admin/experiences')[0],401)
            self.assertEqual(client.call('/api/experiences',submission(),token=False)[0],403)
            self.assertEqual(client.call('/api/experiences',submission(),origin='https://example.invalid')[0],403)
            self.assertEqual(client.call('/api/experiences/'+item['id']+'/publish',{'expected_hash':item['candidate_hash']})[0],404)
            forged=submission();forged['status']='published'
            self.assertEqual(client.call('/api/experiences',forged)[0],400)

    def test_admin_review_publish_retrieval_and_reference_reach_formal_qa_without_reads(self):
        seen=[]
        def planner(context):
            seen.append(context)
            record=next(s for s in context['sources'] if s['id'].startswith('EXP-'))
            return {'action':'answer','answer':'按已审核经验，在正常HA假设下作局部判断，当前角色与业务仍未知。',
                    'refs':[record['id']],'basis':'evidence'},1
        probe=Mock()
        with RunningApp(planner,probe) as client:
            code,item=client.call('/api/experiences',submission());self.assertEqual(code,200)
            self.assertEqual(client.server.get_experiences().records(QUESTION),[])
            client.login()
            code,approved=client.call('/api/admin/experiences/'+item['id']+'/review',
                {'decision':'approve','expected_hash':item['candidate_hash'],'note':'合成审核','regression_question':REGRESSION,'confirmed':True})
            self.assertEqual(code,200)
            code,published=client.call('/api/admin/experiences/'+item['id']+'/publish',{'expected_hash':approved['candidate_hash']})
            self.assertEqual(code,200,published)
            code,result=client.chat(QUESTION)
            self.assertEqual(code,200);self.assertEqual(result['status'],'answered',result)
            self.assertIn('EXP-'+item['id'],result['refs'])
            self.assertEqual(seen[0]['tool_catalog'],{})
            self.assertFalse(result['knowledge_sufficiency']['current_state_supported'])
            probe.assert_not_called();self.assertEqual(client.store.list(),[])
            self.assertTrue(published['publication']['checks']['passed'])

    def test_approval_requires_explicit_confirmation_and_expected_revision(self):
        with RunningApp(Mock(),Mock(),lookup=lambda q:[]) as client:
            item=client.call('/api/experiences',submission())[1];client.login()
            data={'decision':'approve','expected_hash':item['candidate_hash'],'note':'Synthetic','regression_question':REGRESSION,'confirmed':False}
            self.assertEqual(client.call('/api/admin/experiences/'+item['id']+'/review',data)[0],400)
            data.update(confirmed=True,expected_hash='0'*64)
            self.assertEqual(client.call('/api/admin/experiences/'+item['id']+'/review',data)[0],409)
            self.assertEqual(client.call('/api/admin/experiences/'+item['id']+'/publish',{'expected_hash':item['candidate_hash']})[0],409)


if __name__=='__main__':unittest.main()
