"""CPU protocol fixtures; these do not claim robot or LLM task performance."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from datafarm.cap.io import read_json, write_json
from datafarm.cap.library import SkillLibrary
from datafarm.cap.search import SearchConfig, SearchRunner, checked_result

TASK={'name':'route-fixture','instruction':'Go around a blocked approach and return home.',
      'api':{'move':'move(x,y)','observe':'observe()'},'program_contract':'A task-adapter-defined Python expert'}
SKILL={'name':'approach from the open side','failure_signature':'direct approach blocked',
       'when_to_apply':'planner rejects direct approach','repair':'Use an open side before approaching',
       'limitations':'Only where the alternate side is reachable','requires':['move']}


class Engine:
    def __init__(self):self.executions=[];self.records={}
    def evaluate(self,code,seeds,phase):
        rows=[]
        for seed in seeds:
            eid=f'e{len(self.executions)}';self.executions.append((code,seed,phase))
            ok=code.startswith('repaired')
            self.records[eid]=[{'event_id':0,'api':'move','before':[0,0],
                               'after':[1,1] if ok else [0,0],'result':{'ok':ok,'reason':'' if ok else 'blocked'}}]
            rows.append({'episode_id':eid,'seed':seed,'phase':phase,'success':ok,'score':float(ok),
                         'trace':[{'event_id':0,'api':'move','ok':ok}]})
        return {'success_rate':1,'score':1,'episodes':rows}  # Intentionally false aggregate.
    def inspect(self,eid,ids):return [r for r in self.records[eid] if r['event_id'] in ids]


class Actor:
    def __init__(self,verifier_passes=True):self.requests=[];self.verifier_passes=verifier_passes
    def decide(self,r):
        self.requests.append(json.loads(json.dumps(r)))
        if r['role']=='distiller':return {'skill':SKILL}
        if r['role']=='verifier':
            return {'action':'propose','code':'repaired independent' if self.verifier_passes else 'return True',
                    'hypothesis':'derive independently from guidance'}
        if not r['top_programs']:
            return {'action':'propose','code':'repaired from skill' if r['skills'] else 'bad',
                    'hypothesis':'initial candidate'}
        if not r['inspected_evidence']:
            failed=next(c for c in r['top_programs'] if c['debug']['success_rate']<1)
            return {'action':'inspect','episode_id':failed['debug']['episodes'][0]['episode_id'],'event_ids':[0]}
        assert r['inspected_evidence'][0]['events'][0]['result']['reason']=='blocked'
        return {'action':'propose','code':'repaired '+str(len(r['other_hypotheses_this_generation'])),
                'hypothesis':'measured obstruction requires an alternate side'}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.library=SkillLibrary(self.root/'skills')
        self.engine=Engine();self.actor=Actor()
        self.config=SearchConfig(debug_seeds=(0,1),validation_seeds=(10,),verification_seeds=(30,),max_generations=1)

    def test_repair_trace_search_and_fresh_skill_verification(self):
        result=SearchRunner(self.actor,self.engine,self.library,self.config).run(TASK,self.root/'run')
        self.assertTrue(result['ready_for_harness_submission'])
        self.assertEqual(result['candidates_evaluated'],3)
        self.assertEqual(result['skill_admission']['status'],'sound')
        self.assertEqual(len(self.library.entries()),1)
        v=next(r for r in self.actor.requests if r['role']=='verifier')
        self.assertEqual(v['top_programs'],[]);self.assertEqual(v['residual_failures'],[])
        self.assertEqual(v['skills'],[{'guidance':SKILL}])
        self.assertTrue(all(e['seed'] in (0,1) for r in self.actor.requests
                            for c in r.get('top_programs',[]) for e in c['debug']['episodes']))
        self.assertEqual({s for _,s,p in self.engine.executions if p=='verification'},{30})
        self.assertNotIn('evaluation',{p for _,_,p in self.engine.executions})

    def test_failed_verifier_does_not_admit_skill(self):
        result=SearchRunner(Actor(False),self.engine,self.library,self.config).run(TASK,self.root/'run')
        self.assertEqual(result['skill_admission']['status'],'defective')
        self.assertEqual(self.library.entries(),[])

    def test_seed_overlap_and_bool_rejected(self):
        for seeds in ((0,),(True,)):
            with self.assertRaises(ValueError):replace(self.config,verification_seeds=seeds).validate()

    def test_budget_termination_keeps_executed_candidate(self):
        config=replace(self.config,max_model_calls=1)
        result=SearchRunner(self.actor,self.engine,self.library,config).run(TASK,self.root/'run')
        self.assertEqual(result['provider_decisions'],1)
        self.assertTrue(result['search_budget_exhausted'])
        self.assertFalse(result['ready_for_harness_submission'])

    def test_model_cannot_inspect_another_run(self):
        class Intruder:
            def decide(self,r):return {'action':'inspect','episode_id':'hidden-holdout','event_ids':[0]}
        with self.assertRaisesRegex(ValueError,'this search history'):
            SearchRunner(Intruder(),self.engine,self.library,self.config).run(TASK,self.root/'run')

    def test_result_aggregate_and_fake_return_do_not_create_success(self):
        result=checked_result(self.engine.evaluate('return True',(0,),'debug'),(0,),'debug')
        self.assertEqual(result['success_rate'],0)

    def test_duplicate_program_not_reexecuted(self):
        class Duplicate:
            def decide(self,r):return {'action':'propose','code':'bad','hypothesis':'same source'}
        result=SearchRunner(Duplicate(),self.engine,self.library,self.config).run(TASK,self.root/'run')
        self.assertEqual(result['candidates_evaluated'],1)
        self.assertEqual(sum(p=='debug' for _,_,p in self.engine.executions),2)

    def test_changed_skill_is_rejected_and_incompatible_api_filtered(self):
        SearchRunner(self.actor,self.engine,self.library,self.config).run(TASK,self.root/'run')
        self.assertEqual(self.library.retrieve({**TASK,'api':{'other':'()'}}),[])
        f=next((self.root/'skills').glob('*.json'));entry=read_json(f)
        entry['payload']['guidance']['repair']='forged';write_json(f,entry)
        with self.assertRaisesRegex(ValueError,'integrity'):self.library.entries()

    def test_corrupt_score_and_missing_seed_fail_closed(self):
        result=self.engine.evaluate('bad',(0,),'debug');result['episodes'][0]['score']=float('nan')
        with self.assertRaises(ValueError):checked_result(result,(0,),'debug')
        with self.assertRaises(ValueError):checked_result({'episodes':[]},(0,),'debug')

    def test_run_directory_never_overwritten(self):
        (self.root/'run').mkdir()
        with self.assertRaises(FileExistsError):
            SearchRunner(self.actor,self.engine,self.library,self.config).run(TASK,self.root/'run')
