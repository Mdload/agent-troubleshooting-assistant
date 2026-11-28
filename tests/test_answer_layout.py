"""正文多重转义可纠正一次；不自动改写代码和路径。"""
import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from devices.diagnostic_core import AuditStore
from legacy.unified_chat import run


class AnswerLayoutTests(unittest.TestCase):
    def execute(self,texts):
        with tempfile.TemporaryDirectory() as tmp:
            self.planner=Mock(side_effect=[({'action':'answer','answer':t,'refs':[],'basis':'general'},1) for t in texts])
            return run('普通问题',[],self.planner,Mock(),Mock(),AuditStore(Path(tmp)/'a.db'))
    def test_escaped_paragraphs_request_one_correction(self):
        result=self.execute(['first\\n\\nsecond\\n1. third','first\n\nsecond\n1. third'])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(result['events'][0]['reason'],'escaped_answer_layout')
        self.assertIn('Windows',self.planner.call_args[0][0]['correction_feedback']['message'])
    def test_code_literals_and_paths_are_preserved(self):
        text='```python\ntext="first\\n\\nsecond\\nthird"\n```\n路径：`C:\\new\\notes`'
        result=self.execute([text])
        self.assertEqual(result['answer'],text)
        self.assertEqual(self.planner.call_count,1)
    def test_repeated_bad_layout_does_not_get_more_corrections(self):
        text='first\\n\\nsecond\\nthird'
        result=self.execute([text,text,'unused'])
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(sum(e['action']=='correction' for e in result['events']),1)

    def test_backticks_cannot_hide_escaped_prose(self):
        bad='说明`dict`。\\n\\n第二段。\\n\\n```python\\nd = {}\\n```'
        good='说明`dict`。\n\n第二段。\n\n```python\nd = {}\n```'
        result=self.execute([bad,good])
        self.assertEqual(result['answer'],good)
        self.assertEqual(self.planner.call_count,2)
        self.assertEqual(result['events'][0]['reason'],'escaped_answer_layout')

    def test_tilde_fences_and_literal_code_remain_unchanged(self):
        text='说明`a\\n\\nb\\nc`。\n\n~~~python\nx = "a\\n\\nb\\nc"\n~~~'
        result=self.execute([text])
        self.assertEqual(result['answer'],text)
        self.assertEqual(self.planner.call_count,1)

    def test_encoded_fence_newlines_need_correction_even_without_prose(self):
        result=self.execute(['```python\\nx = 1\\nprint(x)\\n```','```python\nx = 1\nprint(x)\n```'])
        self.assertEqual(result['status'],'answered')
        self.assertEqual(self.planner.call_count,2)


if __name__=='__main__':unittest.main()
