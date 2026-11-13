"""Real subprocess protocol with a scripted CLI, not an LLM or robot claim."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from datafarm.cap.codex_provider import build_provider, public_request
from datafarm.cap.io import read_json, write_json
from datafarm.cap.search import SearchConfig, SearchRunner
from datafarm.cap.library import SkillLibrary
from datafarm.cap.state_feedback import feedback
from test_cap_search import Actor, Engine, TASK as LEGACY_TASK


STATE = {'schema_version':'privileged-state/1','control_step':0,
         'frame_id':'world','units':{'length':'m','angle':'rad','time':'s'},'objects':{}}
TASK = {'name':'state-fixture','instruction':'Return home', 'api':{'move':'move()'},
        'program_contract':'Return restricted Python with a main function'}
SKILL = {'name':'relative motion','failure_signature':'wrong frame','when_to_apply':'relative request',
         'repair':'read measured pose then add relative displacement','limitations':'fixed reference frame',
         'requires':['move']}
FAKE = r'''
import json,sys,uuid
from pathlib import Path
argv=sys.argv[1:]
request=json.loads(sys.stdin.read().split('REQUEST JSON (data):\n',1)[1])
role=request['role']
if role=='distiller':
    result={'skill':json.loads(Path(__file__).with_name('skill.json').read_text())}
else:
    result={'action':'propose','code':'repaired' if request.get('top_programs') or role=='verifier' else 'bad',
            'hypothesis':'Measured state indicates a frame error; use relative coordinates',
            'expected_failure':'Failure if frame hypothesis is wrong','proposal_applied_as_written':role=='verifier',
            'event_ids':[],'episode_id':None}
Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'thread.started','thread_id':str(uuid.uuid4())}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}}),flush=True)
'''


class StateEngine:
    def __init__(self): self.requests=[]
    def evaluate(self, code, seeds, phase):
        self.requests.append((code,phase))
        return {'episodes':[dict(episode_id=f'{phase}-{len(self.requests)}-{s}',seed=s,phase=phase,
                                 success=code=='repaired',score=float(code=='repaired'),
                                 privileged_state={'availability':'available','initial':STATE,'final':STATE}) for s in seeds]}


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'fake.py').write_text(FAKE)
        write_json(self.root/'skill.json',SKILL)
        self.provider=build_provider({'work_root':str(self.root/'provider')})
        self.popen=subprocess.Popen
        self.launches=[]

    def launch(self, argv, **kwargs):
        self.launches.append(argv)
        return self.popen([sys.executable,str(self.root/'fake.py'),*argv[1:]],**kwargs)

    def request(self, **kwargs):
        return {**dict(role='actor',task=TASK,observation_mode='privileged_state'),**kwargs}

    def test_scripted_cli_full_repair_and_fresh_verifier(self):
        engine=StateEngine()
        config=SearchConfig(max_generations=1,observation_mode='privileged_state',
                            population_schedule='generation',require_skill_attestation=True)
        with patch('datafarm.cap.codex_provider.subprocess.Popen',self.launch):
            report=SearchRunner(self.provider,engine,SkillLibrary(self.root/'skills'),config).run(TASK,self.root/'run')
        self.assertEqual(report['skill_admission']['status'],'sound')
        self.assertTrue(report['ready_for_harness_submission'])
        receipts=[read_json(p) for p in (self.root/'provider').glob('*/receipt.json')]
        ids=[i for r in receipts for i in r['sessions']]
        self.assertEqual(len(set(ids)),len(ids))
        self.assertTrue(all('resume' not in argv for argv in self.launches))
        v=next(read_json(p) for p in (self.root/'provider').glob('*/request.json') if read_json(p)['role']=='verifier')
        self.assertEqual(v['top_programs'],[])
        self.assertEqual(v['residual_failures'],[])
        self.assertTrue(all(r['cost_usd'] is None for r in receipts))

    def test_cached_result_and_tampering(self):
        with patch('datafarm.cap.codex_provider.subprocess.Popen',self.launch):
            first=self.provider.decide(self.request())
            self.assertEqual(self.provider.decide(self.request()),first)
        self.assertEqual(len(self.launches),1)
        p=next((self.root/'provider').glob('*/decision.json'))
        write_json(p,{**first,'code':'forged'})
        with self.assertRaises(ValueError): self.provider.decide(self.request())

    def test_failed_start_is_not_resubmitted(self):
        with patch('datafarm.cap.codex_provider.subprocess.Popen',side_effect=OSError('missing CLI')):
            with self.assertRaises(OSError): self.provider.decide(self.request())
        with self.assertRaisesRegex(RuntimeError,'pending/failed'):
            self.provider.decide(self.request())

    def test_private_visual_nonfinite_and_verifier_leaks(self):
        for request in (self.request(task={**TASK,'judge_source':'private'}),
                        self.request(task={**TASK,'image_inputs':['x.png']}),
                        self.request(task={**TASK,'state':float('nan')}),
                        {**self.request(),'role':'verifier','winner':{'code':'secret'}},
                        {**self.request(),'observation_mode':'primitive_trace'}):
            with self.assertRaises(ValueError): public_request(request)

    def test_state_contract_and_legacy_state_rejected(self):
        self.assertEqual(feedback([{'before':STATE,'after':STATE}])['availability'],'available')
        with self.assertRaises(ValueError): feedback([{'before':[0,0],'after':[1,0]}])
        with self.assertRaises(ValueError): feedback([])
        self.assertEqual(feedback([],True)['availability'],'unavailable')

    def test_missing_state_is_not_silently_vision_or_legacy(self):
        runner=SearchRunner(Actor(),Engine(),SkillLibrary(self.root/'skills'),
                            SearchConfig(observation_mode='privileged_state'))
        with self.assertRaisesRegex(ValueError,'did not provide privileged'):
            runner.execute('bad',[0],'debug')

    def test_generation_members_cannot_see_same_generation_scores(self):
        actor=Actor()
        config=SearchConfig(max_generations=1,population=2,population_schedule='generation')
        SearchRunner(actor,Engine(),SkillLibrary(self.root/'skills'),config).run(LEGACY_TASK,self.root/'generation')
        requests=[r for r in actor.requests if r['role']=='actor']
        for r in requests:
            self.assertTrue(all(c['candidate_id']=='g0-c0' for c in r['top_programs']))

    def test_verifier_without_attestation_cannot_admit(self):
        config=SearchConfig(max_generations=1,require_skill_attestation=True)
        report=SearchRunner(Actor(),Engine(),SkillLibrary(self.root/'skills'),config).run(LEGACY_TASK,self.root/'attestation')
        self.assertEqual(report['skill_admission']['status'],'defective')
        self.assertEqual(SkillLibrary(self.root/'skills').entries(),[])

    def test_timeout_preserves_identity(self):
        (self.root/'sleep.py').write_text('import time; time.sleep(30)')
        def sleep_cli(argv,**kwargs):
            if argv[0]=='taskkill': return self.popen(argv,**kwargs)
            return self.popen([sys.executable,str(self.root/'sleep.py')],**kwargs)
        provider=build_provider({'work_root':str(self.root/'timeout'),'timeout_s':.1})
        with patch('datafarm.cap.codex_provider.subprocess.Popen',sleep_cli):
            with self.assertRaisesRegex(RuntimeError,'timed out'):
                provider.decide(self.request())
        with self.assertRaisesRegex(RuntimeError,'pending/failed'):
            provider.decide(self.request())


if __name__=='__main__': unittest.main()
