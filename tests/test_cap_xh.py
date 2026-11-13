"""Author-client protocol tests; a scripted broker is not a physical robot run."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datafarm.cap.io import read_json,source_digest,write_json
from datafarm.cap.xh import XHEngine


class XHTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ('requests','responses','results'):(self.root/'broker'/name).mkdir(parents=True)
        self.engine=XHEngine(self.root,'Expert','primitive.jsonl',timeout_s=.03,poll_s=.001)
        self.attempts=0

    def service(self,_):
        for f in (self.root/'broker/requests').glob('*.json'):
            request=read_json(f);rid=request['request_id'];args=request['args']
            response=self.root/'broker/responses'/(rid+'.json')
            if response.exists():continue
            self.attempts+=1;aid=f'A-{self.attempts:04d}'
            write_json(response,{'request_id':rid,'status':'accepted','attempt_id':aid})
            if request['kind']=='submit':continue
            directory=self.root/'rollouts'/aid;directory.mkdir(parents=True)
            trace=[{'event':'primitive','event_id':0,'api':'move','before':[0,0],
                    'after':[1,0],'result':{'ok':True}}]
            (directory/'primitive.jsonl').write_text('\n'.join(json.dumps(r) for r in trace))
            code=(self.root/'workspace'/args['file']).read_text()
            write_json(self.root/'broker/results'/(aid+'.json'),{
                'attempt_id':aid,'seed':args['seed'],'expert_sha256':source_digest(code),
                'outcome_class':'judged','counted':True,'success':True,'run_dir':str(directory)})

    def test_engine_uses_broker_and_reuses_existing_attempt(self):
        code='class Expert: pass\n'
        with patch('datafarm.cap.xh.time.sleep',self.service):
            first=self.engine.evaluate(code,[0],'debug')
            self.assertEqual(first['episodes'][0]['episode_id'],'A-0001')
            again=self.engine.evaluate(code,[0],'debug')
            self.assertEqual(first,again);self.assertEqual(self.attempts,1)
            self.assertEqual(self.engine.inspect('A-0001',[0])[0]['before'],[0,0])
            self.engine.submit(code)
        self.assertEqual(self.attempts,2)

    def test_timeout_persists_one_identity_and_can_resume(self):
        with self.assertRaises(TimeoutError):self.engine.evaluate('class Expert: pass',[0],'debug')
        self.assertEqual(len(list((self.root/'broker/requests').glob('*.json'))),1)
        with patch('datafarm.cap.xh.time.sleep',self.service):
            self.engine.evaluate('class Expert: pass',[0],'debug')
        self.assertEqual(self.attempts,1)

    def test_broker_rejection_is_not_bypassed(self):
        def reject(_):
            for f in (self.root/'broker/requests').glob('*.json'):
                r=read_json(f);write_json(self.root/'broker/responses'/f.name,
                                         {'request_id':r['request_id'],'status':'rejected','reason':'not a development seed'})
        with patch('datafarm.cap.xh.time.sleep',reject):
            with self.assertRaisesRegex(ValueError,'development seed'):
                self.engine.evaluate('class Expert: pass',[999],'debug')

    def test_trace_path_escape_and_unknown_events_refused(self):
        with self.assertRaises(ValueError):XHEngine(self.root,'Expert','../harness/private.json')
        with patch('datafarm.cap.xh.time.sleep',self.service):self.engine.evaluate('class Expert: pass',[0],'debug')
        with self.assertRaises(ValueError):self.engine.inspect('A-0001',[99])

    def test_changed_source_and_mismatched_outcome_refused(self):
        code='class Expert: pass'
        with patch('datafarm.cap.xh.time.sleep',self.service):self.engine.evaluate(code,[0],'debug')
        result=self.root/'broker/results/A-0001.json';raw=read_json(result);raw['expert_sha256']='0'*64;write_json(result,raw)
        with self.assertRaisesRegex(ValueError,'bound'):self.engine.evaluate(code,[0],'debug')
        source=next((self.root/'workspace').glob('cap-*.py'));source.write_text('modified')
        with self.assertRaisesRegex(ValueError,'modified'):self.engine.evaluate(code,[0],'debug')
