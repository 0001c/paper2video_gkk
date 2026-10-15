"""第三章实验传输替身冒烟，不读密钥、不调用外部模型。"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts/experiments'))
import nlp_ch3_compare as experiment


@unittest.skipUnless((experiment.ROOT/'manifest.json').exists(),'需要本地已冻结实验输入')
class ExperimentSmoke(unittest.TestCase):
    def test_frozen_inputs_and_both_generators_without_network(self):
        experiment.check()
        with tempfile.TemporaryDirectory(dir=REPO/'jobs/_validation',prefix='ch3-smoke-') as directory:
            root=Path(directory)
            for name in ('plan.json','manifest.json','baseline_course.py','common_slides.pdf'):
                shutil.copyfile(experiment.ROOT/name,root/name)
            shutil.copytree(experiment.ROOT/'common_slides',root/'common_slides')
            requests=[]
            def fake(label,messages,**kwargs):
                prompt=messages[0]['content'][0]['text']
                requests.append((label,prompt))
                minutes=float(re.findall(r'本页目标约\s*(\d+(?:\.\d+)?)',prompt)[-1])
                text='z=20。'+'字'*max(1,round(minutes*220)-8)+'。'
                return json.dumps({'pages':[{'sentences':[{'speech_text':text,'cursor_prompt':'no'}]}]}),{'total_tokens':1}
            with patch.object(experiment,'ROOT',root),patch.object(experiment,'OUT',root/'display'),\
                 patch.object(experiment,'check'),patch.object(experiment.transport,'api',side_effect=fake):
                experiment.generate()
                before=len(requests)
                experiment.generate()
                self.assertEqual(len(requests),before,'恢复不得重复请求已完成页')
            for sample in experiment.ORDER:
                self.assertEqual(len(experiment.course.split_pages((root/sample/'subtitle_w_cursor.txt').read_text('utf-8'))),8)
            old=next(p for label,p in requests if label.startswith('generate-A1-p2-'))
            new=next(p for label,p in requests if label.startswith('generate-B1-p2-'))
            self.assertNotIn('前面实际已讲内容',old)
            self.assertIn('前面实际已讲内容',new)
            self.assertIn('后面要承接的问题',new)


if __name__=='__main__':
    unittest.main(verbosity=2)
